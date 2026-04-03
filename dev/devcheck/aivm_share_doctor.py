#!/usr/bin/env python3
"""
Standalone inspector for aivm shared / shared-root / virtiofs mounts.

v4 changes:
- deeper bind-mount inspection on host and guest
- exact mount-record enumeration for targets (including stacked/duplicate mounts)
- detailed findmnt output where available with util-linux compatibility fallback
- root/source/samefile checks for staged shared-root bind targets
- treat exact /proc/self/mountinfo records as authoritative for bind mount presence
- understand source strings like dataset[/subdir] and source+root combinations
- detailed reporting for orphaned staged mountpoints
"""
from __future__ import annotations

import argparse
import dataclasses as dc
import json
import os
import platform
import pwd
import re
import shutil
import stat as statmod
import subprocess
import textwrap
import tomllib
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

SHARED_ROOT_TAG = "aivm-shared-root"
GUEST_SHARED_ROOT_BASE = "/mnt/aivm-shared"
DEFAULT_TIMEOUT = 4.0


@dc.dataclass
class Attachment:
    host_path: str
    vm_name: str
    mode: str
    access: str
    guest_dst: str
    tag: str
    host_lexical_path: str = ""


@dc.dataclass
class VMConfig:
    name: str
    user: str = "agent"
    base_dir: str = "/var/lib/libvirt/aivm"


@dc.dataclass
class ConfigState:
    active_vm: str = ""
    vms: dict[str, VMConfig] = dc.field(default_factory=dict)
    attachments: list[Attachment] = dc.field(default_factory=list)


@dc.dataclass
class MountRecord:
    mount_id: int
    parent_id: int
    mount_point: str
    root: str
    fs_type: str
    source: str
    options: str
    super_options: str

    def as_dict(self) -> dict[str, Any]:
        return dc.asdict(self)


@dc.dataclass
class FindmntRecord:
    source: str = ""
    target: str = ""
    fstype: str = ""
    options: str = ""
    propagation: str = ""


@dc.dataclass
class CheckResult:
    level: str
    scope: str
    subject: str
    message: str
    data: dict[str, Any] = dc.field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return dc.asdict(self)


class Reporter:
    def __init__(self) -> None:
        self.results: list[CheckResult] = []

    def add(self, level: str, scope: str, subject: str, message: str, **data: Any) -> None:
        self.results.append(CheckResult(level=level, scope=scope, subject=subject, message=message, data=data))

    def ok(self, scope: str, subject: str, message: str, **data: Any) -> None:
        self.add("OK", scope, subject, message, **data)

    def warn(self, scope: str, subject: str, message: str, **data: Any) -> None:
        self.add("WARN", scope, subject, message, **data)

    def fail(self, scope: str, subject: str, message: str, **data: Any) -> None:
        self.add("FAIL", scope, subject, message, **data)

    def info(self, scope: str, subject: str, message: str, **data: Any) -> None:
        self.add("INFO", scope, subject, message, **data)

    def exit_code(self) -> int:
        if any(r.level == "FAIL" for r in self.results):
            return 2
        if any(r.level == "WARN" for r in self.results):
            return 1
        return 0


def run_cmd(cmd: list[str], timeout: float = DEFAULT_TIMEOUT, check: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, text=True, capture_output=True, timeout=timeout, check=check)


def which(name: str) -> bool:
    return shutil.which(name) is not None


def normpath_text(path: str | Path) -> str:
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path))))


def realpath_text(path: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(path)))


def safe_realpath(path: str | Path) -> str:
    try:
        return realpath_text(path)
    except Exception:
        return normpath_text(path)


def safe_lstat(path: str | Path) -> dict[str, Any] | None:
    try:
        st = os.lstat(path)
    except Exception:
        return None
    return {
        "dev": st.st_dev,
        "ino": st.st_ino,
        "mode": statmod.filemode(st.st_mode),
        "uid": st.st_uid,
        "gid": st.st_gid,
    }


def safe_samefile(path1: str | Path, path2: str | Path) -> bool | None:
    try:
        return os.path.samefile(path1, path2)
    except Exception:
        return None


def candidate_config_paths(explicit: Path | None) -> list[Path]:
    seen: set[str] = set()
    out: list[Path] = []

    def add(path: Path | None) -> None:
        if path is None:
            return
        key = str(path)
        if key not in seen:
            seen.add(key)
            out.append(path)

    add(explicit)
    env_path = os.environ.get("AIVM_CONFIG")
    if env_path:
        add(Path(env_path).expanduser())
    xdg = os.environ.get("XDG_CONFIG_HOME")
    if xdg:
        add(Path(xdg) / "aivm" / "config.toml")
    add(Path.home() / ".config" / "aivm" / "config.toml")

    for user_key in ["SUDO_USER", "USER", "LOGNAME"]:
        val = os.environ.get(user_key, "").strip()
        if not val:
            continue
        try:
            home = Path(pwd.getpwnam(val).pw_dir)
        except KeyError:
            continue
        add(home / ".config" / "aivm" / "config.toml")

    for root in [Path("/home"), Path("/root")]:
        if root.is_dir():
            if root.name == "root":
                add(root / ".config" / "aivm" / "config.toml")
            else:
                for child in sorted(root.iterdir()):
                    add(child / ".config" / "aivm" / "config.toml")
    return out


def load_config(path: Path | None) -> ConfigState:
    cfg = ConfigState()
    if path is None or not path.exists():
        return cfg
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    cfg.active_vm = str(data.get("active_vm", "") or "")

    base_dir = "/var/lib/libvirt/aivm"
    try:
        vms = data.get("vms", []) or []
        for item in vms:
            name = str(item.get("name", "") or "").strip()
            if not name:
                continue
            vm_block = item.get("vm", {}) or {}
            paths = item.get("paths", {}) or {}
            user = str(vm_block.get("user", "agent") or "agent")
            item_base = str(paths.get("base_dir", base_dir) or base_dir)
            cfg.vms[name] = VMConfig(name=name, user=user, base_dir=item_base)
    except Exception:
        pass

    attachments = data.get("attachments", []) or []
    for item in attachments:
        try:
            cfg.attachments.append(
                Attachment(
                    host_path=str(item.get("host_path", "") or "").strip(),
                    vm_name=str(item.get("vm_name", "") or "").strip(),
                    mode=str(item.get("mode", "shared") or "shared").strip(),
                    access=str(item.get("access", "rw") or "rw").strip(),
                    guest_dst=str(item.get("guest_dst", "") or "").strip(),
                    tag=str(item.get("tag", "") or "").strip(),
                    host_lexical_path=str(item.get("host_lexical_path", "") or "").strip(),
                )
            )
        except Exception:
            continue
    return cfg


def resolve_config_path(explicit: Path | None, explicit_vm: str | None) -> tuple[Path | None, ConfigState]:
    candidates = [p for p in candidate_config_paths(explicit) if p.exists()]
    if not candidates:
        return None, ConfigState()
    if explicit is not None and explicit.exists():
        return explicit, load_config(explicit)

    loaded: list[tuple[Path, ConfigState]] = []
    for cand in candidates:
        try:
            loaded.append((cand, load_config(cand)))
        except Exception:
            continue
    if not loaded:
        return None, ConfigState()
    if len(loaded) == 1:
        return loaded[0]

    if explicit_vm:
        for cand, cfg in loaded:
            if explicit_vm in cfg.vms or any(a.vm_name == explicit_vm for a in cfg.attachments):
                return cand, cfg
    for cand, cfg in loaded:
        if cfg.active_vm and (not explicit_vm or cfg.active_vm == explicit_vm):
            return cand, cfg
    best = max(loaded, key=lambda item: (len(item[1].attachments), len(item[1].vms)))
    return best


def read_mountinfo() -> list[MountRecord]:
    out: list[MountRecord] = []
    for line in Path("/proc/self/mountinfo").read_text(encoding="utf-8").splitlines():
        left, right = line.split(" - ", 1)
        lf = left.split()
        rf = right.split()
        out.append(
            MountRecord(
                mount_id=int(lf[0]),
                parent_id=int(lf[1]),
                root=lf[3],
                mount_point=lf[4],
                options=lf[5],
                fs_type=rf[0],
                source=rf[1] if len(rf) > 1 else "",
                super_options=rf[2] if len(rf) > 2 else "",
            )
        )
    return out


def findmnt_target(path: str) -> FindmntRecord | None:
    if not which("findmnt"):
        return None
    try:
        proc = run_cmd(["findmnt", "-n", "-T", path, "-o", "SOURCE,TARGET,FSTYPE,OPTIONS,PROPAGATION"], timeout=DEFAULT_TIMEOUT)
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    parts = proc.stdout.rstrip("\n").split(maxsplit=4)
    parts += [""] * (5 - len(parts))
    return FindmntRecord(source=parts[0], target=parts[1], fstype=parts[2], options=parts[3], propagation=parts[4])


def findmnt_target_detailed(path: str) -> dict[str, Any] | None:
    if not which("findmnt"):
        return None
    column_sets = [
        "SOURCE,SOURCES,TARGET,FSTYPE,OPTIONS,PROPAGATION,ROOT",
        "SOURCE,SOURCES,TARGET,FSTYPE,OPTIONS,PROPAGATION",
        "SOURCE,TARGET,FSTYPE,OPTIONS,PROPAGATION",
    ]
    last_err = None
    for cols in column_sets:
        try:
            proc = run_cmd(["findmnt", "-J", "-T", path, "-o", cols], timeout=max(DEFAULT_TIMEOUT, 8.0))
        except subprocess.TimeoutExpired:
            return {"error": "findmnt timed out"}
        if proc.returncode != 0 or not proc.stdout.strip():
            last_err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
            continue
        try:
            payload = json.loads(proc.stdout)
        except Exception as ex:
            last_err = f"could not parse findmnt JSON: {ex}"
            continue
        filesystems = payload.get("filesystems") or []
        if not filesystems:
            continue
        data = filesystems[0]
        data["_columns"] = cols
        return data
    return {"error": last_err} if last_err else None


def probe_stat(path: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        proc = run_cmd(["stat", "-f", path], timeout=timeout)
    except subprocess.TimeoutExpired:
        return False, "timeout"
    if proc.returncode == 0:
        return True, "ok"
    err = (proc.stderr or proc.stdout or f"exit={proc.returncode}").strip()
    return False, err


def parse_domain_filesystems(xml_text: str) -> list[dict[str, Any]]:
    root = ET.fromstring(xml_text)
    out: list[dict[str, Any]] = []
    for fs in root.findall(".//devices/filesystem"):
        driver = fs.find("driver")
        if driver is None or driver.attrib.get("type", "").strip().lower() != "virtiofs":
            continue
        source = fs.find("source")
        target = fs.find("target")
        readonly = fs.find("readonly") is not None
        out.append(
            {
                "source_dir": (source.attrib.get("dir", "") if source is not None else "").strip(),
                "target_tag": (target.attrib.get("dir", "") if target is not None else "").strip(),
                "readonly": readonly,
            }
        )
    return out


def parse_memory_backing(xml_text: str) -> tuple[bool | None, dict[str, str]]:
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None, {}
    mb = root.find(".//memoryBacking")
    if mb is None:
        return False, {}
    source = mb.find("source")
    access = mb.find("access")
    source_type = (source.attrib.get("type", "") if source is not None else "").strip()
    access_mode = (access.attrib.get("mode", "") if access is not None else "").strip()
    ok = source_type == "memfd" and access_mode == "shared"
    return ok, {"source_type": source_type, "access_mode": access_mode}


def detect_local_role() -> str:
    if which("virsh") and Path("/var/lib/libvirt").exists():
        return "host"
    try:
        proc = run_cmd(["systemd-detect-virt"], timeout=2)
        if proc.returncode == 0 and proc.stdout.strip():
            return "guest"
    except Exception:
        pass
    text = ""
    for fp in ["/sys/class/dmi/id/product_name", "/sys/class/dmi/id/sys_vendor"]:
        p = Path(fp)
        if p.exists():
            try:
                text += " " + p.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                pass
    if re.search(r"kvm|qemu|virtual|vmware|virtualbox", text, flags=re.I):
        return "guest"
    return "unknown"


def list_vm_names_from_host() -> list[str]:
    if not which("virsh"):
        return []
    try:
        proc = run_cmd(["virsh", "-c", "qemu:///system", "list", "--all", "--name"], timeout=8)
    except subprocess.TimeoutExpired:
        return []
    if proc.returncode != 0:
        return []
    return [line.strip() for line in proc.stdout.splitlines() if line.strip()]


def host_shared_root_dir(vm_cfg: VMConfig) -> str:
    return normpath_text(Path(vm_cfg.base_dir) / vm_cfg.name / "shared-root")


def attachments_for_vm(cfg: ConfigState, vm_name: str) -> list[Attachment]:
    return sorted([a for a in cfg.attachments if a.vm_name == vm_name], key=lambda a: (a.mode, a.host_path, a.guest_dst, a.tag))


def is_subpath(child: str, parent: str) -> bool:
    try:
        Path(normpath_text(child)).relative_to(Path(normpath_text(parent)))
        return True
    except Exception:
        return False


def option_has_mode(options: str, want: str) -> bool:
    parts = {p.strip() for p in options.split(",") if p.strip()}
    return want in parts


def candidate_vm_names(cfg: ConfigState, explicit_vm: str | None) -> list[str]:
    if explicit_vm:
        return [explicit_vm]
    if cfg.active_vm:
        return [cfg.active_vm]
    if cfg.vms:
        return sorted(cfg.vms)
    return list_vm_names_from_host()


def xml_for_vm(vm_name: str) -> tuple[str | None, str | None]:
    if not which("virsh"):
        return None, "virsh not found"
    try:
        proc = run_cmd(["virsh", "-c", "qemu:///system", "dumpxml", vm_name], timeout=10)
    except subprocess.TimeoutExpired:
        return None, "virsh dumpxml timed out"
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or f"virsh dumpxml exit={proc.returncode}").strip()
        return None, err
    return proc.stdout, None


def shared_root_stage_relpath(att: Attachment) -> str:
    return att.tag or Path(att.guest_dst or att.host_path).name


def shared_root_stage_host_path(shared_root_dir: str, att: Attachment) -> str:
    return normpath_text(Path(shared_root_dir) / shared_root_stage_relpath(att))


def shared_root_stage_guest_path(att: Attachment) -> str:
    return normpath_text(Path(GUEST_SHARED_ROOT_BASE) / shared_root_stage_relpath(att))


def exact_mount_records_for_target(path: str, mountinfo: list[MountRecord]) -> list[MountRecord]:
    target = normpath_text(path)
    records = [m for m in mountinfo if normpath_text(m.mount_point) == target]
    records.sort(key=lambda m: (m.mount_id, m.parent_id))
    return records


def mount_record_for_target(path: str, mountinfo: list[MountRecord]) -> tuple[MountRecord | None, FindmntRecord | None, dict[str, Any] | None, list[MountRecord]]:
    records = exact_mount_records_for_target(path, mountinfo)
    rec = records[-1] if records else None
    fm = findmnt_target(path)
    detail = findmnt_target_detailed(path)
    return rec, fm, detail, records


def _split_findmnt_bracket_source(raw: str) -> tuple[str, str | None]:
    m = re.match(r"^(.*)\[(/.+)\]$", raw.strip())
    if not m:
        return raw.strip(), None
    return m.group(1).strip(), m.group(2).strip()


def _joined_source_root(source: str, root: str) -> str | None:
    source = (source or "").strip()
    root = (root or "").strip()
    if not root or not root.startswith("/"):
        return None
    if source.startswith("/"):
        return normpath_text(Path(source) / root.lstrip("/"))
    if source and source != "none":
        return normpath_text(Path("/") / source / root.lstrip("/"))
    return None


def _candidate_source_paths(rec: MountRecord | None, fm: FindmntRecord | None, detail: dict[str, Any] | None) -> dict[str, str]:
    out: dict[str, str] = {}
    if rec is not None:
        if rec.root.startswith("/"):
            out["mountinfo_root"] = rec.root
        if rec.source.startswith("/"):
            out["mountinfo_source"] = rec.source
        joined = _joined_source_root(rec.source, rec.root)
        if joined:
            out["mountinfo_source_plus_root"] = joined
    if fm is not None:
        raw = (fm.source or "").strip()
        if raw:
            out["findmnt_source"] = raw
            base, bracket_root = _split_findmnt_bracket_source(raw)
            if base.startswith("/"):
                out["findmnt_source_base"] = base
            joined = _joined_source_root(base, bracket_root or "")
            if joined:
                out["findmnt_source_plus_root"] = joined
    if isinstance(detail, dict):
        for key in ["source", "root"]:
            val = detail.get(key)
            if isinstance(val, str) and val:
                out[f"findmnt_detail_{key}"] = val
        detail_source = str(detail.get("source") or "").strip()
        detail_root = str(detail.get("root") or "").strip()
        if detail_source:
            base, bracket_root = _split_findmnt_bracket_source(detail_source)
            if base.startswith("/"):
                out["findmnt_detail_source_base"] = base
            joined = _joined_source_root(base, bracket_root or detail_root)
            if joined:
                out["findmnt_detail_source_plus_root"] = joined
        vals = detail.get("sources")
        if isinstance(vals, list):
            iterable = vals
        elif isinstance(vals, str):
            iterable = [v.strip() for v in vals.split(",") if v.strip()]
        else:
            iterable = []
        for idx, raw in enumerate(iterable):
            out[f"findmnt_detail_sources[{idx}]"] = raw
            base, bracket_root = _split_findmnt_bracket_source(raw)
            if base.startswith("/"):
                out[f"findmnt_detail_sources_base[{idx}]"] = base
            joined = _joined_source_root(base, bracket_root or detail_root)
            if joined:
                out[f"findmnt_detail_sources_plus_root[{idx}]"] = joined
    return out


def compare_expected_source(expected_source: str, rec: MountRecord | None, fm: FindmntRecord | None, detail: dict[str, Any] | None) -> dict[str, Any]:
    expected_norm = normpath_text(expected_source)
    expected_real = safe_realpath(expected_source)
    candidates = _candidate_source_paths(rec, fm, detail)
    matches: list[dict[str, str]] = []
    normalized_candidates: dict[str, str] = {}
    for key, raw in candidates.items():
        if not raw:
            continue
        normalized_candidates[key] = raw
        raw_norm = normpath_text(raw) if raw.startswith("/") else raw
        raw_real = safe_realpath(raw) if raw.startswith("/") else raw
        if raw_norm == expected_norm or raw_real == expected_real:
            matches.append({"field": key, "value": raw})
    return {
        "expected_source": expected_source,
        "expected_norm": expected_norm,
        "expected_real": expected_real,
        "matches": matches,
        "candidates": normalized_candidates,
        "matched": bool(matches),
    }


def inspect_mount_target(path: str, mountinfo: list[MountRecord], expected_source: str | None = None, peer_path: str | None = None) -> dict[str, Any]:
    target = normpath_text(path)
    exists = Path(target).exists()
    rec, fm, detail, records = mount_record_for_target(target, mountinfo)
    is_mount = bool(records)
    os_path_ismount = os.path.ismount(target) if exists else False
    responsive, probe = probe_stat(target) if exists else (False, "missing")
    target_lstat = safe_lstat(target) if exists else None
    expected_lstat = safe_lstat(expected_source) if expected_source and Path(expected_source).exists() else None
    peer_lstat = safe_lstat(peer_path) if peer_path and Path(peer_path).exists() else None
    samefile_expected = safe_samefile(target, expected_source) if expected_source else None
    samefile_peer = safe_samefile(target, peer_path) if peer_path else None
    source_compare = compare_expected_source(expected_source, rec, fm, detail) if expected_source else None
    data: dict[str, Any] = {
        "target": target,
        "exists": exists,
        "is_mount": is_mount,
        "os_path_ismount": os_path_ismount,
        "responsive": responsive,
        "probe": probe,
        "record_count": len(records),
        "records": [r.as_dict() for r in records],
        "active_record": rec.as_dict() if rec else None,
        "findmnt": detail if detail is not None else (dc.asdict(fm) if fm else None),
        "target_lstat": target_lstat,
    }
    if expected_source:
        data["expected_source"] = normpath_text(expected_source)
        data["expected_source_lstat"] = expected_lstat
        data["samefile_expected_source"] = samefile_expected
        data["source_compare"] = source_compare
    if peer_path:
        data["peer_path"] = normpath_text(peer_path)
        data["peer_lstat"] = peer_lstat
        data["samefile_peer"] = samefile_peer
    return data


def host_checks(rep: Reporter, cfg: ConfigState, explicit_vm: str | None, cfg_path: Path | None) -> None:
    mountinfo = read_mountinfo()
    vms = candidate_vm_names(cfg, explicit_vm)
    if not vms:
        rep.warn("host", "vm-selection", "No VM name available from config or virsh")
        return

    rep.info("host", "vm-selection", f"Inspecting VMs: {', '.join(vms)}")
    if cfg_path is None:
        rep.warn(
            "host",
            "config",
            "No aivm config was loaded; attachment-aware host checks are limited. Pass --config explicitly or run without sudo-preserving HOME.",
        )

    for vm_name in vms:
        vm_cfg = cfg.vms.get(vm_name, VMConfig(name=vm_name))
        xml_text, xml_err = xml_for_vm(vm_name)
        if xml_err:
            rep.fail("host", vm_name, "Unable to read libvirt XML", error=xml_err)
            continue
        assert xml_text is not None
        try:
            filesystems = parse_domain_filesystems(xml_text)
        except Exception as ex:
            rep.fail("host", vm_name, "Failed to parse domain XML", error=str(ex))
            continue

        virtiofs_entries = [fs for fs in filesystems if fs.get("source_dir") or fs.get("target_tag")]
        if virtiofs_entries:
            rep.ok("host", vm_name, f"Found {len(virtiofs_entries)} virtiofs mapping(s)", mappings=virtiofs_entries)
        else:
            rep.warn("host", vm_name, "No virtiofs mappings found in domain XML")

        mem_ok, mem_data = parse_memory_backing(xml_text)
        if mem_ok is True:
            rep.ok("host", vm_name, "VM XML has memfd/shared memory backing", **mem_data)
        elif mem_ok is False:
            rep.warn("host", vm_name, "VM XML lacks memfd/shared memory backing expected by branch logic", **mem_data)
        else:
            rep.warn("host", vm_name, "Could not determine VM shared-memory backing")

        shared_root_dir = host_shared_root_dir(vm_cfg)
        shared_root_mapping = [fs for fs in virtiofs_entries if normpath_text(fs["source_dir"]) == shared_root_dir or fs["target_tag"] == SHARED_ROOT_TAG]
        if shared_root_mapping:
            rep.ok("host", vm_name, f"Shared-root virtiofs export is present: {shared_root_dir} -> {SHARED_ROOT_TAG}", mapping=shared_root_mapping)
        else:
            rep.fail(
                "host",
                vm_name,
                f"Shared-root virtiofs export missing or not matched to {shared_root_dir} / {SHARED_ROOT_TAG}",
                expected_source=shared_root_dir,
                expected_tag=SHARED_ROOT_TAG,
                actual_mappings=virtiofs_entries,
            )

        shared_root_path = Path(shared_root_dir)
        if shared_root_path.exists():
            rep.ok("host", vm_name, f"Shared-root directory exists: {shared_root_dir}")
        else:
            rep.fail("host", vm_name, f"Shared-root directory missing: {shared_root_dir}")

        nested_mountpoints = sorted({normpath_text(m.mount_point) for m in mountinfo if is_subpath(m.mount_point, shared_root_dir) and normpath_text(m.mount_point) != shared_root_dir})
        rep.info("host", vm_name, f"Found {len(nested_mountpoints)} nested mountpoint(s) under shared-root", shared_root_dir=shared_root_dir, mountpoints=nested_mountpoints)

        staged_by_source: dict[str, list[tuple[str, dict[str, Any]]]] = {}
        inspected_nested: dict[str, dict[str, Any]] = {}
        for mp in nested_mountpoints:
            detail = inspect_mount_target(mp, mountinfo)
            inspected_nested[mp] = detail
            active = detail.get("active_record") or {}
            candidates = []
            for raw in [active.get("root"), active.get("source")]:
                if isinstance(raw, str) and raw.startswith("/"):
                    candidates.extend([safe_realpath(raw), normpath_text(raw)])
            fm = detail.get("findmnt") or {}
            if isinstance(fm, dict):
                for raw in [fm.get("source"), fm.get("root")]:
                    if isinstance(raw, str) and raw.startswith("/"):
                        candidates.extend([safe_realpath(raw), normpath_text(raw)])
            for key in set(candidates):
                staged_by_source.setdefault(key, []).append((mp, detail))

        atts = attachments_for_vm(cfg, vm_name)
        if not atts:
            rep.info("host", vm_name, "No config attachments loaded for this VM; only live host checks were performed")
            continue

        expected_shared_root_mounts: set[str] = set()
        actual_shared_root_mounts: set[str] = set(nested_mountpoints)

        for att in atts:
            subject = f"{vm_name}:{att.mode}:{att.host_path}"
            if att.mode == "shared":
                wanted = [fs for fs in virtiofs_entries if safe_realpath(fs["source_dir"]) == safe_realpath(att.host_path) and fs["target_tag"] == att.tag]
                if wanted:
                    fs = wanted[0]
                    if att.access == "ro" and not fs.get("readonly", False):
                        rep.warn("host", subject, "Shared virtiofs mapping exists but is not marked readonly in libvirt XML", tag=att.tag)
                    else:
                        rep.ok("host", subject, "Shared virtiofs mapping present", tag=att.tag, access=att.access)
                else:
                    rep.fail("host", subject, "Expected per-folder shared virtiofs mapping missing from domain XML", tag=att.tag, host_path=att.host_path)
                continue

            if att.mode != "shared-root":
                rep.info("host", subject, f"Skipping host mount checks for mode={att.mode}")
                continue

            expected_mp = shared_root_stage_host_path(shared_root_dir, att)
            expected_shared_root_mounts.add(expected_mp)
            detail = inspect_mount_target(expected_mp, mountinfo, expected_source=att.host_path)
            exists = bool(detail.get("exists"))
            is_mount = bool(detail.get("is_mount"))
            compare = detail.get("source_compare") or {}
            matched = bool(compare.get("matched"))
            samefile_expected = detail.get("samefile_expected_source") is True
            if not matched and samefile_expected:
                matched = True
            options = ""
            active_record = detail.get("active_record") or {}
            if isinstance(active_record, dict):
                options = str(active_record.get("options") or "")
            findmnt_data = detail.get("findmnt") or {}
            if not options and isinstance(findmnt_data, dict):
                options = str(findmnt_data.get("options") or "")
            want_mode = "ro" if att.access == "ro" else "rw"

            if not exists:
                rep.fail(
                    "host",
                    subject,
                    "Expected staged shared-root target path does not exist",
                    expected_mountpoint=expected_mp,
                    host_path=att.host_path,
                    guest_dst=att.guest_dst,
                    access=att.access,
                    bind_detail=detail,
                )
                continue
            if not matched:
                matches = []
                for key in {safe_realpath(att.host_path), normpath_text(att.host_path)}:
                    matches.extend(staged_by_source.get(key, []))
                dedup = {mp: d for mp, d in matches}
                if dedup:
                    rep.fail(
                        "host",
                        subject,
                        "Attachment source is mounted somewhere under shared-root, but not at the expected mountpoint",
                        expected_mountpoint=expected_mp,
                        alternate_mountpoints=sorted(dedup),
                        bind_detail=detail,
                    )
                else:
                    rep.fail(
                        "host",
                        subject,
                        "Staged shared-root mountpoint exists but does not point at the expected source",
                        expected_mountpoint=expected_mp,
                        host_path=att.host_path,
                        bind_detail=detail,
                    )
                continue

            level = "OK"
            message = "Detailed staged bind inspection looks correct"
            if detail.get("record_count", 0) > 1:
                level = "WARN"
                message = "Staged bind target has stacked or duplicate mount records; top record matches expected source"
            elif detail.get("os_path_ismount") is False and detail.get("is_mount") is True:
                level = "INFO"
                message = "Staged bind target is present in mountinfo; os.path.ismount() does not recognize this bind mount"
            if not option_has_mode(options, want_mode):
                level = "WARN"
                message = "Staged bind target source matches expected source, but options do not match configured access"
            if detail.get("responsive") is not True:
                level = "WARN"
                message = "Staged bind target source matches expected source, but the target did not respond cleanly to stat"
            if detail.get("samefile_expected_source") is False:
                level = "WARN"
                message = "Staged bind target source matches by mount metadata, but samefile(source,target) is false"

            payload = {
                "mountpoint": expected_mp,
                "host_path": att.host_path,
                "guest_dst": att.guest_dst,
                "want_mode": want_mode,
                "bind_detail": detail,
            }
            if level == "OK":
                rep.ok("host", subject, message, **payload)
            elif level == "INFO":
                rep.info("host", subject, message, **payload)
            else:
                rep.warn("host", subject, message, **payload)

        extra_mounts = sorted(actual_shared_root_mounts - expected_shared_root_mounts)
        for mp in extra_mounts:
            rep.warn(
                "host",
                vm_name,
                "Found staged shared-root mountpoint that does not correspond to current config attachments",
                extra_mountpoint=mp,
                bind_detail=inspected_nested.get(mp) or inspect_mount_target(mp, mountinfo),
            )

        missing_mounts = sorted(expected_shared_root_mounts - actual_shared_root_mounts)
        if missing_mounts:
            rep.fail("host", vm_name, "Some configured shared-root attachments are missing their expected staged mountpoints", missing_mountpoints=missing_mounts)


def local_virtiofs_mounts(mountinfo: list[MountRecord]) -> list[MountRecord]:
    return [m for m in mountinfo if m.fs_type == "virtiofs"]


def likely_shared_root_bases(mountinfo: list[MountRecord]) -> list[str]:
    roots = []
    for m in mountinfo:
        fm = findmnt_target(m.mount_point)
        source = fm.source if fm else m.source
        if m.fs_type == "virtiofs" and source == SHARED_ROOT_TAG:
            roots.append(normpath_text(m.mount_point))
    return sorted(set(roots))


def infer_guest_shared_root_chains(mountinfo: list[MountRecord], shared_root_bases: list[str]) -> list[dict[str, Any]]:
    chains: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base in shared_root_bases:
        stage_mps = sorted({normpath_text(m.mount_point) for m in mountinfo if is_subpath(m.mount_point, base) and normpath_text(m.mount_point) != base})
        for stage in stage_mps:
            if stage in seen:
                continue
            seen.add(stage)
            stage_detail = inspect_mount_target(stage, mountinfo)
            stage_rec = stage_detail.get("active_record") or {}
            stage_root = str(stage_rec.get("root") or "")
            stage_source = str(stage_rec.get("source") or "")
            stage_fstype = str(stage_rec.get("fs_type") or "")
            peers: list[str] = []
            for rec in mountinfo:
                mp = normpath_text(rec.mount_point)
                if mp == stage or is_subpath(mp, base):
                    continue
                same_chain = False
                if stage_root and rec.root == stage_root and rec.source == stage_source and rec.fs_type == stage_fstype:
                    same_chain = True
                elif safe_samefile(mp, stage) is True:
                    same_chain = True
                if same_chain:
                    peers.append(mp)
            peers = sorted(set(peers))
            chains.append({
                "base": base,
                "stage": stage,
                "stage_detail": stage_detail,
                "peers": peers,
                "peer_details": [inspect_mount_target(peer, mountinfo, peer_path=stage) for peer in peers],
            })
    return chains


def guest_checks(rep: Reporter, cfg: ConfigState, explicit_vm: str | None, cfg_path: Path | None) -> None:
    mountinfo = read_mountinfo()
    virtiofs = local_virtiofs_mounts(mountinfo)
    if virtiofs:
        rep.ok("guest", "virtiofs", f"Detected {len(virtiofs)} virtiofs mount(s)", mounts=[{"mount_point": m.mount_point, "source": m.source, "root": m.root, "options": m.options} for m in virtiofs])
    else:
        rep.warn("guest", "virtiofs", "No virtiofs mounts detected in this mount namespace")

    shared_root_bases = likely_shared_root_bases(mountinfo)
    if shared_root_bases:
        rep.ok("guest", "shared-root-base", f"Detected {len(shared_root_bases)} shared-root base mount(s)", bases=shared_root_bases)
    else:
        rep.warn("guest", "shared-root-base", "Did not find a virtiofs mount whose source tag is aivm-shared-root")

    if cfg_path is None:
        rep.warn(
            "guest",
            "config",
            "No aivm config was loaded; attachment-aware guest checks are limited. Pass --config explicitly if the config is available inside the guest.",
        )

    vm_name = explicit_vm or cfg.active_vm or ""
    atts = attachments_for_vm(cfg, vm_name) if vm_name else []
    if not atts:
        inferred = infer_guest_shared_root_chains(mountinfo, shared_root_bases)
        if inferred:
            rep.info("guest", "config", "No config attachments loaded; inferring shared-root guest bind chains from live mount records")
            for item in inferred:
                if item["peers"]:
                    rep.ok(
                        "guest",
                        item["stage"],
                        f"Inferred {len(item['peers'])} guest bind destination(s) for staged shared-root path",
                        base=item["base"],
                        stage_detail=item["stage_detail"],
                        peers=item["peers"],
                        peer_details=item["peer_details"],
                    )
                else:
                    rep.warn(
                        "guest",
                        item["stage"],
                        "Staged shared-root path has no inferred guest bind destination outside the base mount",
                        base=item["base"],
                        stage_detail=item["stage_detail"],
                    )
        else:
            rep.info("guest", "config", "No config attachments loaded for guest-aware checks; only live virtiofs inspection was performed")
        return

    for att in atts:
        subject = f"{att.vm_name}:{att.mode}:{att.guest_dst or att.host_path}"
        if att.mode not in {"shared", "shared-root"}:
            rep.info("guest", subject, f"Skipping guest mount checks for mode={att.mode}")
            continue
        if not att.guest_dst:
            rep.warn("guest", subject, "Attachment has no guest_dst recorded")
            continue

        rec, fm, detail, records = mount_record_for_target(att.guest_dst, mountinfo)
        if rec is None and fm is None:
            rep.fail("guest", subject, "Expected guest mountpoint is not mounted", guest_dst=att.guest_dst, mode=att.mode, tag=att.tag)
            continue

        actual_source = fm.source if fm else (rec.source if rec else "")
        actual_fstype = fm.fstype if fm else (rec.fs_type if rec else "")
        options = fm.options if fm else (rec.options if rec else "")
        responsive, detail_probe = probe_stat(att.guest_dst)
        want_mode = "ro" if att.access == "ro" else "rw"

        if att.mode == "shared":
            bind_detail = inspect_mount_target(att.guest_dst, mountinfo)
            if actual_fstype == "virtiofs" and actual_source == att.tag:
                if bind_detail.get("record_count", 0) > 1:
                    rep.warn("guest", subject, "Shared guest mount resolves to the expected virtiofs tag, but stacked mount records were found", guest_dst=att.guest_dst, bind_detail=bind_detail)
                else:
                    rep.ok("guest", subject, "Shared guest mount looks correct", guest_dst=att.guest_dst, source=actual_source, fstype=actual_fstype, options=options, bind_detail=bind_detail)
            else:
                rep.fail("guest", subject, "Shared guest mount does not resolve to the expected virtiofs tag", expect_tag=att.tag, actual_source=actual_source, actual_fstype=actual_fstype, options=options, bind_detail=bind_detail)
        else:
            stage_path = shared_root_stage_guest_path(att)
            stage_detail = inspect_mount_target(stage_path, mountinfo)
            dst_detail = inspect_mount_target(att.guest_dst, mountinfo, peer_path=stage_path)
            stage_rec = stage_detail.get("active_record") or {}
            dst_rec = dst_detail.get("active_record") or {}
            stage_fm = stage_detail.get("findmnt") or {}
            dst_fm = dst_detail.get("findmnt") or {}

            stage_source = stage_fm.get("source") if isinstance(stage_fm, dict) else ""
            stage_fstype = stage_fm.get("fstype") if isinstance(stage_fm, dict) else ""
            if not stage_source:
                stage_source = str(stage_rec.get("source") or "")
            if not stage_fstype:
                stage_fstype = str(stage_rec.get("fs_type") or "")
            stage_root = str(stage_rec.get("root") or "")
            dst_root = str(dst_rec.get("root") or "")

            stage_ok = False
            if stage_fstype == "virtiofs" and stage_source == SHARED_ROOT_TAG:
                stage_ok = True
            elif stage_fstype == "virtiofs" and any(is_subpath(stage_path, base) for base in shared_root_bases):
                stage_ok = True
            elif any(stage_source == base or stage_source.startswith(base.rstrip("/") + "/") for base in shared_root_bases):
                stage_ok = True

            dst_ok = False
            if dst_detail.get("samefile_peer") is True:
                dst_ok = True
            elif stage_root and dst_root and stage_root == dst_root:
                dst_ok = True
            elif actual_source == stage_path or actual_source == normpath_text(stage_path):
                dst_ok = True

            payload = {
                "guest_dst": att.guest_dst,
                "expected_stage": stage_path,
                "stage_detail": stage_detail,
                "dst_detail": dst_detail,
                "shared_root_bases": shared_root_bases,
            }
            if stage_ok and dst_ok:
                if stage_detail.get("record_count", 0) > 1 or dst_detail.get("record_count", 0) > 1:
                    rep.warn("guest", subject, "Shared-root guest bind chain looks correct, but stacked mount records were found", **payload)
                else:
                    rep.ok("guest", subject, "Detailed shared-root guest bind chain looks correct", **payload)
            else:
                rep.fail("guest", subject, "Shared-root guest mount chain is inconsistent", **payload)

        if not option_has_mode(options, want_mode):
            rep.warn("guest", subject, "Guest mount options do not match configured access", guest_dst=att.guest_dst, options=options, want_mode=want_mode)
        if not responsive:
            rep.warn("guest", subject, "Guest mount did not respond to stat probe", guest_dst=att.guest_dst, probe=detail_probe)


def render_text(rep: Reporter, role: str, cfg_path: Path | None, cfg: ConfigState, vm_name: str | None) -> str:
    lines: list[str] = []
    lines.append("aivm share doctor")
    lines.append("=" * len(lines[-1]))
    lines.append(f"role={role}")
    lines.append(f"hostname={platform.node()}")
    lines.append(f"config={cfg_path if cfg_path else '(none)'}")
    lines.append(f"active_vm={cfg.active_vm or '(none)'}")
    if vm_name:
        lines.append(f"requested_vm={vm_name}")
    lines.append("")

    grouped: dict[str, list[CheckResult]] = {}
    for item in rep.results:
        grouped.setdefault(item.scope, []).append(item)

    order = ["host", "guest"] + sorted(k for k in grouped if k not in {"host", "guest"})
    for scope in order:
        items = grouped.get(scope)
        if not items:
            continue
        lines.append(scope.upper())
        lines.append("-" * len(scope))
        for item in items:
            prefix = {"OK": "[OK]", "WARN": "[WARN]", "FAIL": "[FAIL]", "INFO": "[INFO]"}.get(item.level, f"[{item.level}]")
            lines.append(f"{prefix} {item.subject}: {item.message}")
            if item.data:
                compact = json.dumps(item.data, indent=2, sort_keys=True)
                for ln in compact.splitlines():
                    lines.append(f"    {ln}")
        lines.append("")

    counts = {k: sum(1 for r in rep.results if r.level == k) for k in ["FAIL", "WARN", "OK", "INFO"]}
    lines.append("SUMMARY")
    lines.append("-------")
    lines.append(f"fails={counts['FAIL']} warns={counts['WARN']} ok={counts['OK']} info={counts['INFO']}")
    if counts["FAIL"] == 0 and counts["WARN"] == 0:
        lines.append("No obvious shared-mount problems were detected.")
    else:
        lines.append("Focus first on FAIL entries, then WARN entries.")
    return "\n".join(lines).rstrip() + "\n"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Inspect aivm virtiofs/shared-root state from the host or the guest.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """
            Examples:
              python aivm_share_doctor_v4.py
              sudo python aivm_share_doctor_v4.py --role host --vm aivm-2404
              python aivm_share_doctor_v4.py --role guest --config ~/.config/aivm/config.toml
              python aivm_share_doctor_v4.py --json
            """
        ).strip(),
    )
    p.add_argument("--role", choices=["auto", "host", "guest"], default="auto")
    p.add_argument("--config", type=Path, default=None)
    p.add_argument("--vm", help="VM name to inspect. Defaults to active_vm from config when available.")
    p.add_argument("--json", action="store_true", help="Emit machine-readable JSON instead of text.")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    role = args.role
    if role == "auto":
        role = detect_local_role()
    cfg_path, cfg = resolve_config_path(args.config, args.vm)
    rep = Reporter()

    if role in {"host", "unknown"}:
        host_checks(rep, cfg, args.vm, cfg_path)
    if role in {"guest", "unknown"}:
        guest_checks(rep, cfg, args.vm, cfg_path)
    if role == "unknown":
        rep.warn("runtime", "role-detection", "Could not confidently detect host vs guest, so both check sets were attempted")

    if args.json:
        payload = {"role": role, "config": str(cfg_path) if cfg_path else None, "active_vm": cfg.active_vm, "results": [r.as_dict() for r in rep.results], "exit_code": rep.exit_code()}
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(render_text(rep, role, cfg_path, cfg, args.vm), end="")
    return rep.exit_code()


if __name__ == "__main__":
    raise SystemExit(main())

