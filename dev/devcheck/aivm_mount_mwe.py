#!/usr/bin/env python3
r"""

Developer Usage
---------------

python dev/devcheck/aivm_mount_mwe.py info \
  --ssh-target aivm-2404 \
  --vm-name aivm-2404 \
  --guest-shared-base /mnt/aivm-shared \
  --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root

python ~/code/aivm/dev/devcheck/aivm_mount_mwe.py  cycle   --ssh-target aivm-2404   --vm-name aivm-2404   --guest-shared-base /mnt/aivm-shared   --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root   --runid stacktest1   --order host-first


aivm vm restart
# in the aivm code dir to get the shared root
aivm code .

ssh aivm-2404 'findmnt -T /mnt/aivm-shared'
sudo test -d /var/lib/libvirt/aivm/aivm-2404/shared-root && echo ok

sudo findmnt -M /var/lib/libvirt/aivm/aivm-2404/shared-root/__mwe__/stacktest-clean-1/stage || true
ssh aivm-2404 'findmnt -M /tmp/aivm-mwe/stacktest-clean-1/dst || true'

# TEST HOST FIRST (USE A UNIQUE RUNID)

python ~/code/aivm/dev/devcheck/aivm_mount_mwe.py cycle \
  --ssh-target aivm-2404 \
  --vm-name aivm-2404 \
  --guest-shared-base /mnt/aivm-shared \
  --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
  --runid stacktest-clean-1 \
  --order host-first \
  --reconcile-retries 10 \
  --reconcile-sleep 1.0 \
  --keep-on-cleanup-fail

# TEST GUEST FIRST (USE A UNIQUE RUNID)

python ~/code/aivm/dev/devcheck/aivm_mount_mwe.py cycle \
  --ssh-target aivm-2404 \
  --vm-name aivm-2404 \
  --guest-shared-base /mnt/aivm-shared \
  --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
  --runid stacktest--guest-clean-2 \
  --order guest-first \
  --reconcile-retries 10 \
  --reconcile-sleep 1.0 \
  --keep-on-cleanup-fail

RUNID=stacktest1
for i in $(seq 1 20); do
  echo "=== cycle $i guest-first ==="
  python dev/devcheck/aivm_mount_mwe.py cycle \
    --ssh-target aivm-2404 \
    --vm-name aivm-2404 \
    --guest-shared-base /mnt/aivm-shared \
    --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
    --runid "$RUNID" \
    --order guest-first || break
done


RUNID=stacktest2
for i in $(seq 1 30); do
  echo "=== cycle $i host-first ==="
  python dev/devcheck/aivm_mount_mwe.py cycle \
    --ssh-target aivm-2404 \
    --vm-name aivm-2404 \
    --guest-shared-base /mnt/aivm-shared \
    --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
    --runid "$RUNID" \
    --order host-first || break
done


RUNID=stacktest3
for i in $(seq 1 30); do
  echo "=== cycle $i lazy guest+host ==="
  python dev/devcheck/aivm_mount_mwe.py cycle \
    --ssh-target aivm-2404 \
    --vm-name aivm-2404 \
    --guest-shared-base /mnt/aivm-shared \
    --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
    --runid "$RUNID" \
    --order guest-first \
    --lazy-host \
    --lazy-guest || break
done


"""
from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import textwrap
import time
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence


@dataclass
class CmdResult:
    argv: list[str]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(argv: Sequence[str], check: bool = False) -> CmdResult:
    proc = subprocess.run(list(argv), capture_output=True, text=True, check=False)
    res = CmdResult(list(argv), proc.returncode, proc.stdout or "", proc.stderr or "")
    if check and not res.ok:
        raise RuntimeError(
            f"command failed ({res.returncode}): {' '.join(shlex.quote(x) for x in argv)}\n"
            f"stdout:\n{res.stdout}\n"
            f"stderr:\n{res.stderr}"
        )
    return res


def q(text: str) -> str:
    return shlex.quote(text)


def maybe_sudo(argv: Sequence[str], enabled: bool) -> list[str]:
    return (["sudo"] + list(argv)) if enabled else list(argv)


def host_run(args: argparse.Namespace, argv: Sequence[str], check: bool = False) -> CmdResult:
    return run(maybe_sudo(list(argv), args.host_sudo), check=check)


def ssh_argv(args: argparse.Namespace) -> list[str]:
    argv = [
        "ssh",
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
    ]
    if args.ssh_identity:
        argv += ["-i", args.ssh_identity]
    if args.ssh_port:
        argv += ["-p", str(args.ssh_port)]
    argv.append(args.ssh_target)
    return argv


def guest_run(
    args: argparse.Namespace,
    script: str,
    check: bool = False,
    use_sudo: bool = False,
) -> CmdResult:
    prefix = "sudo " if use_sudo else ""
    remote_cmd = f"{prefix}bash -lc {shlex.quote(script)}"
    return run(ssh_argv(args) + [remote_cmd], check=check)


def show(label: str, res: CmdResult) -> None:
    state = "OK" if res.ok else f"RC={res.returncode}"
    print(f"[{state}] {label}")
    print(f"$ {' '.join(shlex.quote(x) for x in res.argv)}")
    if res.stdout.strip():
        print(res.stdout.rstrip())
    if res.stderr.strip():
        print("[stderr]")
        print(res.stderr.rstrip())


def show_if_any(label: str, res: CmdResult) -> None:
    if res.ok or res.stdout.strip() or res.stderr.strip():
        show(label, res)


def banner(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def guest_hostname(args: argparse.Namespace) -> str:
    res = guest_run(args, "hostname -s", check=True)
    return res.stdout.strip()


def _parse_findmnt_json(stdout: str) -> list[dict]:
    data = json.loads(stdout)
    items = []

    def walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        items.append(node)
        for child in node.get("children", []) or []:
            walk(child)

    for fs in data.get("filesystems", []) or []:
        walk(fs)
    return items


def _parse_findmnt_pairs(stdout: str) -> list[dict]:
    items = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line:
            continue
        item = {}
        for token in shlex.split(line):
            if "=" in token:
                k, v = token.split("=", 1)
                item[k.lower()] = v
        if item:
            items.append(item)
    return items


def _parse_mountinfo(stdout: str) -> list[dict]:
    items = []
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or " - " not in line:
            continue
        parts = line.split()
        if len(parts) < 10:
            continue
        sep = parts.index("-")
        pre = parts[:sep]
        post = parts[sep + 1 :]
        if len(pre) < 5 or len(post) < 3:
            continue
        items.append(
            {
                "target": pre[4],
                "root": pre[3],
                "fstype": post[0],
                "source": post[1],
                "options": pre[5] if len(pre) > 5 else "",
            }
        )
    return items


def guest_discovery_report(args: argparse.Namespace) -> dict:
    report = {
        "findmnt_json": None,
        "findmnt_pairs": None,
        "mountinfo": None,
        "mount_cmd": None,
        "candidates": [],
        "errors": [],
    }

    res_json = guest_run(args, "command findmnt --json --list -t virtiofs -o TARGET,SOURCE,FSTYPE,OPTIONS")
    report["findmnt_json"] = {"ok": res_json.ok, "stdout": res_json.stdout, "stderr": res_json.stderr}
    if res_json.ok and res_json.stdout.strip():
        try:
            s = res_json.stdout.lstrip()
            if not s.startswith("{"):
                report["errors"].append(f"findmnt_json_not_json: first_bytes={s[:80]!r}")
            else:
                for item in _parse_findmnt_json(res_json.stdout):
                    tgt = item.get("target")
                    src = item.get("source")
                    fstype = item.get("fstype")
                    if tgt and src:
                        report["candidates"].append(
                            {"strategy": "findmnt_json", "target": str(tgt), "source": str(src), "fstype": str(fstype or "")}
                        )
        except Exception as ex:
            report["errors"].append(f"findmnt_json_parse: {ex}")

    res_pairs = guest_run(args, "command findmnt --pairs --list -t virtiofs -o TARGET,SOURCE,FSTYPE,OPTIONS")
    report["findmnt_pairs"] = {"ok": res_pairs.ok, "stdout": res_pairs.stdout, "stderr": res_pairs.stderr}
    if res_pairs.ok and res_pairs.stdout.strip():
        try:
            first = next((ln.strip() for ln in res_pairs.stdout.splitlines() if ln.strip()), "")
            if "=" not in first:
                report["errors"].append(f"findmnt_pairs_not_pairs: first_line={first[:120]!r}")
            else:
                for item in _parse_findmnt_pairs(res_pairs.stdout):
                    tgt = item.get("target")
                    src = item.get("source")
                    fstype = item.get("fstype")
                    if tgt and src:
                        report["candidates"].append(
                            {"strategy": "findmnt_pairs", "target": str(tgt), "source": str(src), "fstype": str(fstype or "")}
                        )
        except Exception as ex:
            report["errors"].append(f"findmnt_pairs_parse: {ex}")

    res_mountinfo = guest_run(args, "command awk '$0 ~ / - virtiofs / { print }' /proc/self/mountinfo")
    report["mountinfo"] = {"ok": res_mountinfo.ok, "stdout": res_mountinfo.stdout, "stderr": res_mountinfo.stderr}
    if res_mountinfo.stdout.strip():
        try:
            for item in _parse_mountinfo(res_mountinfo.stdout):
                tgt = item.get("target")
                src = item.get("source")
                fstype = item.get("fstype")
                if tgt and src:
                    report["candidates"].append(
                        {"strategy": "mountinfo", "target": str(tgt), "source": str(src), "fstype": str(fstype or "")}
                    )
        except Exception as ex:
            report["errors"].append(f"mountinfo_parse: {ex}")

    res_mount = guest_run(args, "command mount | command grep ' type virtiofs ' || true", use_sudo=True)
    report["mount_cmd"] = {"ok": res_mount.ok, "stdout": res_mount.stdout, "stderr": res_mount.stderr}

    seen = set()
    uniq = []
    for item in report["candidates"]:
        key = (item["target"], item["source"], item["fstype"])
        if key not in seen:
            seen.add(key)
            uniq.append(item)
    report["candidates"] = uniq
    return report


def discover_guest_shared_base(args: argparse.Namespace) -> tuple[str, str, dict]:
    report = guest_discovery_report(args)
    candidates = report["candidates"]

    if args.guest_shared_base:
        exact = [c for c in candidates if c["target"] == args.guest_shared_base]
        if exact:
            choice = exact[0]
            return choice["target"], choice["source"], report
        res = guest_run(args, f"findmnt -P -T {q(args.guest_shared_base)} -o TARGET,SOURCE,FSTYPE,OPTIONS")
        if res.ok and res.stdout.strip():
            pairs = _parse_findmnt_pairs(res.stdout)
            good_pairs = [p for p in pairs if p.get("target") and p.get("source")]
            if good_pairs:
                item = good_pairs[0]
                return str(item["target"]), str(item["source"]), report
        raise RuntimeError(f"could not inspect explicit guest shared base {args.guest_shared_base!r}")

    if not candidates:
        raise RuntimeError("no virtiofs mounts found in guest")

    for item in candidates:
        if item["target"] == "/mnt/aivm-shared":
            return item["target"], item["source"], report

    real_sources = [c for c in candidates if c["source"] != "none"]
    if real_sources:
        real_sources.sort(key=lambda x: len(x["target"]))
        choice = real_sources[0]
        return choice["target"], choice["source"], report

    candidates.sort(key=lambda x: len(x["target"]))
    choice = candidates[0]
    return choice["target"], choice["source"], report


def discover_vm_name(args: argparse.Namespace) -> str:
    return args.vm_name or guest_hostname(args)


def discover_host_export_dir(args: argparse.Namespace, vm_name: str, source_tag: str) -> str:
    if args.host_export_dir:
        return args.host_export_dir

    res = host_run(args, ["virsh", "-c", "qemu:///system", "dumpxml", vm_name], check=True)
    root = ET.fromstring(res.stdout)
    matches: list[str] = []
    for fs in root.findall("./devices/filesystem"):
        src = fs.find("source")
        tgt = fs.find("target")
        if src is None or tgt is None:
            continue
        src_dir = src.attrib.get("dir")
        tgt_dir = tgt.attrib.get("dir")
        if src_dir and tgt_dir == source_tag:
            matches.append(src_dir)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RuntimeError(
            f"multiple host exports matched guest source tag {source_tag!r}: {matches}; pass --host-export-dir"
        )
    raise RuntimeError(
        f"no host export matched guest source tag {source_tag!r}; pass --host-export-dir"
    )


def derive_state(args: argparse.Namespace) -> tuple[dict[str, str], dict[str, str], dict[str, str], dict]:
    guest_shared_base, source_tag, report = discover_guest_shared_base(args)
    vm_name = discover_vm_name(args)
    host_export_dir = discover_host_export_dir(args, vm_name, source_tag)

    runid = args.runid or uuid.uuid4().hex[:12]
    host_scratch_root = str(Path(args.host_scratch_root) / runid)
    host_src = str(Path(host_scratch_root) / "src")
    rel_stage = str(Path("__mwe__") / runid / "stage")
    host_stage = str(Path(host_export_dir) / rel_stage)
    guest_stage = str(Path(guest_shared_base) / rel_stage)
    guest_dst = args.guest_dst or str(Path(args.guest_scratch_root) / runid / "dst")

    env = {
        "vm_name": vm_name,
        "guest_shared_base": guest_shared_base,
        "guest_source_tag": source_tag,
        "host_export_dir": host_export_dir,
        "runid": runid,
    }
    host = {"scratch_root": host_scratch_root, "src": host_src, "stage": host_stage}
    guest = {"stage": guest_stage, "dst": guest_dst}
    return env, host, guest, report


def print_discovery(args: argparse.Namespace, env: dict[str, str], host: dict[str, str], guest: dict[str, str]) -> None:
    banner("DISCOVERY")
    print(f"ssh_target        = {args.ssh_target}")
    print(f"vm_name           = {env['vm_name']}")
    print(f"guest_shared_base = {env['guest_shared_base']}")
    print(f"guest_source_tag  = {env['guest_source_tag']}")
    print(f"host_export_dir   = {env['host_export_dir']}")
    print(f"runid             = {env['runid']}")
    print(f"host_src          = {host['src']}")
    print(f"host_stage        = {host['stage']}")
    print(f"guest_stage       = {guest['stage']}")
    print(f"guest_dst         = {guest['dst']}")


def print_discovery_report(report: dict) -> None:
    banner("GUEST DISCOVERY REPORT")
    print(json.dumps(report, indent=2))


def host_findmnt(args: argparse.Namespace, path: str) -> CmdResult:
    return host_run(args, ["findmnt", "-T", path, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION"])


def host_fuser(args: argparse.Namespace, path: str) -> CmdResult:
    return host_run(args, ["fuser", "-vm", path])


def host_busy_holders(args: argparse.Namespace, path: str) -> CmdResult:
    script = (
        f"fuser -vm {q(path)} 2>&1 | "
        "grep -E 'virtiofsd|qemu-system|COMMAND|PID|USER|/var/lib/libvirt|/mnt/aivm-shared' || true"
    )
    return host_run(args, ["bash", "-lc", script])


def host_lsof_mount(args: argparse.Namespace, path: str) -> CmdResult:
    script = f"findmnt -M {q(path)} -o TARGET -n >/dev/null 2>&1 && lsof +D {q(path)} 2>/dev/null | sed -n '1,120p' || true"
    return host_run(args, ["bash", "-lc", script])


def host_stat(args: argparse.Namespace, path: str) -> CmdResult:
    return host_run(args, ["stat", "-Lc", "mode=%A uid=%u gid=%g dev=%d ino=%i type=%F path=%n", path])


def guest_findmnt(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"findmnt -T {q(path)} -o TARGET,SOURCE,FSTYPE,OPTIONS,PROPAGATION")


def guest_fuser(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"fuser -vm {q(path)}")


def guest_stat(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"stat -Lc {q('mode=%A uid=%u gid=%g dev=%d ino=%i type=%F path=%n')} {q(path)}")


def host_git_probe(args: argparse.Namespace, path: str) -> list[CmdResult]:
    out = [host_run(args, ["git", "-C", path, "rev-parse", "--is-inside-work-tree"])]
    if out[-1].ok and out[-1].stdout.strip() == "true":
        out.append(host_run(args, ["git", "-C", path, "status", "--short", "--branch"]))
    return out


def guest_git_probe(args: argparse.Namespace, path: str) -> list[CmdResult]:
    out = [guest_run(args, f"git -C {q(path)} rev-parse --is-inside-work-tree", use_sudo=True)]
    if out[-1].ok and out[-1].stdout.strip() == "true":
        out.append(guest_run(args, f"git -C {q(path)} status --short --branch", use_sudo=True))
    return out


def guest_repo_probe(args: argparse.Namespace, path: str) -> list[CmdResult]:
    out = [
        guest_run(args, f"bash -lc 'test -d {q(str(Path(path) / '.git'))} && echo yes || echo no'", use_sudo=True),
        guest_run(args, f"ls -la {q(path)} | sed -n '1,40p'", use_sudo=True),
    ]
    return out


def prepare_host_source(args: argparse.Namespace, env: dict[str, str], host: dict[str, str]) -> None:
    banner("HOST PREPARE")
    show("mkdir scratch root", host_run(args, ["mkdir", "-p", host["scratch_root"]]))
    show("mkdir host src", host_run(args, ["mkdir", "-p", host["src"]]))
    show("mkdir host stage parent", host_run(args, ["mkdir", "-p", str(Path(host["stage"]).parent)]))

    sentinel = Path(host["src"]) / "MWE_SENTINEL.txt"
    sentinel_text = textwrap.dedent(
        f"""\
        standalone aivm mount MWE
        runid={env['runid']}
        host_src={host['src']}
        """
    )
    show(
        "write sentinel",
        host_run(args, ["bash", "-lc", f"cat > {q(str(sentinel))} <<'EOF'\n{sentinel_text}EOF"]),
    )

    if args.init_git:
        show("git init", host_run(args, ["git", "-C", host["src"], "init", "-q"]))
        show("git config user.name", host_run(args, ["git", "-C", host["src"], "config", "user.name", "MWE"]))
        show("git config user.email", host_run(args, ["git", "-C", host["src"], "config", "user.email", "mwe@example.com"]))
        tracked = Path(host["src"]) / "tracked.txt"
        show("write tracked", host_run(args, ["bash", "-lc", f"printf 'tracked\\n' > {q(str(tracked))}"]))
        show("git add", host_run(args, ["git", "-C", host["src"], "add", "."]))
        show("git commit", host_run(args, ["git", "-C", host["src"], "commit", "-qm", "initial mwe"]))
        untracked = Path(host["src"]) / "untracked.txt"
        show("write untracked", host_run(args, ["bash", "-lc", f"printf 'untracked\\n' > {q(str(untracked))}"]))


def attach(args: argparse.Namespace, env: dict[str, str], host: dict[str, str], guest: dict[str, str]) -> None:
    prepare_host_source(args, env, host)

    banner("HOST ATTACH")
    show("mkdir host stage", host_run(args, ["mkdir", "-p", host["stage"]]))
    if host_is_exact_mountpoint(args, host["stage"]):
        res = host_findmnt(args, host["stage"])
        show("findmnt host stage (already mounted)", res)
        if "//deleted" in res.stdout:
            raise RuntimeError(f"stale host mount detected at {host['stage']}")
    else:
        show("mount --bind host_src -> host_stage", host_run(args, ["mount", "--bind", host["src"], host["stage"]]))
        show_if_any("findmnt host stage", host_findmnt(args, host["stage"]))

    banner("GUEST ATTACH")
    show("mkdir guest dst", guest_run(args, f"mkdir -p {q(guest['dst'])}"))
    if guest_is_exact_mountpoint(args, guest["dst"]):
        show("findmnt guest dst (already mounted)", guest_findmnt(args, guest["dst"]))
    else:
        show("mount --bind guest_stage -> guest_dst", guest_run(args, f"mount --bind {q(guest['stage'])} {q(guest['dst'])}", use_sudo=True))
        if args.access == "ro":
            show("remount guest dst ro", guest_run(args, f"mount -o remount,bind,ro {q(guest['dst'])}", use_sudo=True))
        show_if_any("findmnt guest dst", guest_findmnt(args, guest["dst"]))


def probe(args: argparse.Namespace, host: dict[str, str], guest: dict[str, str]) -> None:
    banner("HOST PROBE")
    show_if_any("stat host src", host_stat(args, host["src"]))
    show_if_any("stat host stage", host_stat(args, host["stage"]))
    show_if_any("findmnt host stage", host_findmnt(args, host["stage"]))
    show_if_any("fuser host stage", host_fuser(args, host["stage"]))
    show_if_any("focused host holders", host_busy_holders(args, host["stage"]))
    show_if_any("lsof host stage", host_lsof_mount(args, host["stage"]))
    for res in host_git_probe(args, host["src"]):
        show_if_any("git host src", res)

    banner("GUEST PROBE")
    show_if_any("stat guest stage", guest_stat(args, guest["stage"]))
    show_if_any("findmnt guest stage", guest_findmnt(args, guest["stage"]))
    show_if_any("fuser guest stage", guest_fuser(args, guest["stage"]))
    show_if_any("stat guest dst", guest_stat(args, guest["dst"]))
    show_if_any("findmnt guest dst", guest_findmnt(args, guest["dst"]))
    show_if_any("fuser guest dst", guest_fuser(args, guest["dst"]))
    for res in guest_git_probe(args, guest["dst"]):
        show_if_any("git guest dst", res)
    for res in guest_repo_probe(args, guest["dst"]):
        show_if_any("repo guest dst", res)


def detach_guest(args: argparse.Namespace, guest: dict[str, str]) -> None:
    banner("DETACH GUEST")
    show_if_any("findmnt guest dst (before)", guest_findmnt(args, guest["dst"]))
    show_if_any("fuser guest dst (before)", guest_fuser(args, guest["dst"]))
    flag = "-l " if args.lazy_guest else ""
    show("umount guest dst", guest_run(args, f"umount {flag}{q(guest['dst'])}", use_sudo=True))
    show_if_any("findmnt guest dst (after)", guest_findmnt(args, guest["dst"]))


def detach_host(args: argparse.Namespace, host: dict[str, str]) -> None:
    banner("DETACH HOST")
    show_if_any("findmnt host stage (before)", host_findmnt(args, host["stage"]))
    show_if_any("fuser host stage (before)", host_fuser(args, host["stage"]))
    argv = ["umount"]
    if args.lazy_host:
        argv.append("-l")
    argv.append(host["stage"])
    show("umount host stage", host_run(args, argv))
    show_if_any("findmnt host stage (after)", host_findmnt(args, host["stage"]))


def reconcile_detach(args: argparse.Namespace, host: dict[str, str], guest: dict[str, str]) -> None:
    banner("DETACH RECONCILE")

    if guest_is_exact_mountpoint(args, guest["dst"]):
        flag = "-l " if args.lazy_guest else ""
        show("retry umount guest dst", guest_run(args, f"umount {flag}{q(guest['dst'])}", use_sudo=True))
        show_if_any("findmnt guest dst (reconcile)", guest_findmnt(args, guest["dst"]))

    if host_is_exact_mountpoint(args, host["stage"]):
        retries = max(1, int(args.reconcile_retries))
        sleep_s = max(0.0, float(args.reconcile_sleep))
        for attempt in range(1, retries + 1):
            argv = ["umount"]
            if args.lazy_host:
                argv.append("-l")
            argv.append(host["stage"])
            show(f"retry umount host stage attempt={attempt}/{retries}", host_run(args, argv))
            show_if_any(f"findmnt host stage (reconcile attempt={attempt})", host_findmnt(args, host["stage"]))
            if not host_is_exact_mountpoint(args, host["stage"]):
                break
            if attempt != retries and sleep_s > 0:
                time.sleep(sleep_s)


def cleanup(args: argparse.Namespace, host: dict[str, str], guest: dict[str, str]) -> None:
    if args.keep:
        banner("KEEP")
        print("keeping scratch paths for manual inspection")
        return
    if guest_is_exact_mountpoint(args, guest["dst"]):
        if args.keep_on_cleanup_fail:
            banner("KEEP")
            print(f"keeping scratch paths because guest dst is still mounted: {guest['dst']}")
            return
        raise RuntimeError(f"refusing cleanup: guest dst still mounted: {guest['dst']}")
    if host_is_exact_mountpoint(args, host["stage"]):
        if args.cleanup_lazy_host_on_fail:
            banner("CLEANUP HOST LAZY UMOUNT")
            show("lazy umount host stage for cleanup", host_run(args, ["umount", "-l", host["stage"]]))
            show_if_any("findmnt host stage (after lazy cleanup)", host_findmnt(args, host["stage"]))
        if host_is_exact_mountpoint(args, host["stage"]):
            if args.keep_on_cleanup_fail:
                banner("KEEP")
                print(f"keeping scratch paths because host stage is still mounted: {host['stage']}")
                return
            raise RuntimeError(f"refusing cleanup: host stage still mounted: {host['stage']}")
    banner("CLEANUP")
    show_if_any("rmdir guest dst", guest_run(args, f"rmdir {q(guest['dst'])}"))
    show_if_any("rmdir host stage", host_run(args, ["rmdir", host["stage"]]))
    show_if_any("rm -rf host scratch root", host_run(args, ["rm", "-rf", host["scratch_root"]]))
    parent = str(Path(host["stage"]).parent)
    grandparent = str(Path(parent).parent)
    show_if_any("rmdir host stage parent", host_run(args, ["rmdir", parent]))
    show_if_any("rmdir host __mwe__", host_run(args, ["rmdir", grandparent]))


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone non-invasive mount/unmount MWE")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--ssh-target", required=True, help="for example agent@10.77.0.195")
        sp.add_argument("--ssh-port", type=int, default=None)
        sp.add_argument("--ssh-identity", default=None)
        sp.add_argument("--vm-name", default=None)
        sp.add_argument("--guest-shared-base", default=None)
        sp.add_argument("--host-export-dir", default=None)
        sp.add_argument("--runid", default=None)
        sp.add_argument("--host-scratch-root", default="/tmp/aivm-mwe")
        sp.add_argument("--guest-scratch-root", default="/tmp/aivm-mwe")
        sp.add_argument("--guest-dst", default=None)
        sp.add_argument("--access", choices=["rw", "ro"], default="rw")
        sp.add_argument("--no-host-sudo", action="store_true")
        sp.add_argument("--keep", action="store_true")
        sp.add_argument("--init-git", dest="init_git", action="store_true", default=True)
        sp.add_argument("--no-init-git", dest="init_git", action="store_false")
        sp.add_argument("--debug-discovery", action="store_true")

    info = sub.add_parser("info")
    add_common(info)

    attach_cmd = sub.add_parser("attach")
    add_common(attach_cmd)
    attach_cmd.add_argument("--probe", action="store_true")

    probe_cmd = sub.add_parser("probe")
    add_common(probe_cmd)

    detach_cmd = sub.add_parser("detach")
    add_common(detach_cmd)
    detach_cmd.add_argument("--order", choices=["guest-first", "host-first", "guest-only", "host-only"], default="guest-first")
    detach_cmd.add_argument("--lazy-host", action="store_true")
    detach_cmd.add_argument("--lazy-guest", action="store_true")
    detach_cmd.add_argument("--probe", action="store_true")
    detach_cmd.add_argument("--post-probe", action="store_true")
    detach_cmd.add_argument("--reconcile-retries", type=int, default=5)
    detach_cmd.add_argument("--reconcile-sleep", type=float, default=0.5)
    detach_cmd.add_argument("--cleanup-lazy-host-on-fail", action="store_true")
    detach_cmd.add_argument("--keep-on-cleanup-fail", action="store_true")

    cycle_cmd = sub.add_parser("cycle")
    add_common(cycle_cmd)
    cycle_cmd.add_argument("--order", choices=["guest-first", "host-first", "guest-only", "host-only"], default="guest-first")
    cycle_cmd.add_argument("--lazy-host", action="store_true")
    cycle_cmd.add_argument("--lazy-guest", action="store_true")
    cycle_cmd.add_argument("--reconcile-retries", type=int, default=5)
    cycle_cmd.add_argument("--reconcile-sleep", type=float, default=0.5)
    cycle_cmd.add_argument("--cleanup-lazy-host-on-fail", action="store_true")
    cycle_cmd.add_argument("--keep-on-cleanup-fail", action="store_true")

    return p



def host_is_exact_mountpoint(args: argparse.Namespace, path: str) -> bool:
    res = host_run(args, ["findmnt", "-M", path, "-o", "TARGET", "-n"])
    return res.ok and res.stdout.strip() == path


def guest_is_exact_mountpoint(args: argparse.Namespace, path: str) -> bool:
    res = guest_run(args, f"findmnt -M {q(path)} -o TARGET -n")
    return res.ok and res.stdout.strip() == path

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.host_sudo = not args.no_host_sudo

    env = host = guest = report = None
    errors = []

    try:
        env, host, guest, report = derive_state(args)
    except Exception as ex:
        errors.append(f"derive_state failed: {ex}")
        try:
            report = guest_discovery_report(args)
        except Exception as ex2:
            errors.append(f"guest_discovery_report failed: {ex2}")
        try:
            vm_name = discover_vm_name(args)
        except Exception as ex3:
            errors.append(f"discover_vm_name failed: {ex3}")
            vm_name = None
        if report and vm_name and args.host_export_dir is None:
            candidates = report.get("candidates") or []
            real_sources = [c for c in candidates if c.get("source") not in (None, "none")]
            if real_sources:
                try:
                    discover_host_export_dir(args, vm_name, str(real_sources[0]["source"]))
                except Exception as ex4:
                    errors.append(f"discover_host_export_dir failed: {ex4}")

    if report and (args.debug_discovery or env is None):
        print_discovery_report(report)

    if env is None or host is None or guest is None:
        banner("PRECHECK FAILURES")
        for err in errors:
            print(f"- {err}")
        print("\nTry again with explicit values, for example:")
        print(
            "python3 aivm_mount_mwe.py info "
            "--ssh-target agent@10.77.0.195 "
            "--vm-name aivm-2404 "
            "--guest-shared-base /mnt/aivm-shared "
            "--host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root"
        )
        return 2

    print_discovery(args, env, host, guest)

    if args.cmd == "info":
        print(json.dumps({"env": env, "host": host, "guest": guest}, indent=2))
        return 0

    if args.cmd == "attach":
        attach(args, env, host, guest)
        if args.probe:
            probe(args, host, guest)
        return 0

    if args.cmd == "probe":
        probe(args, host, guest)
        return 0

    if args.cmd == "detach":
        if args.probe:
            probe(args, host, guest)
        banner("DETACH ORDER")
        print(f"order={args.order} lazy_host={args.lazy_host} lazy_guest={args.lazy_guest}")
        if args.order == "guest-first":
            detach_guest(args, guest)
            detach_host(args, host)
        elif args.order == "host-first":
            detach_host(args, host)
            detach_guest(args, guest)
        elif args.order == "guest-only":
            detach_guest(args, guest)
        elif args.order == "host-only":
            detach_host(args, host)
        else:
            raise AssertionError(args.order)
        reconcile_detach(args, host, guest)
        if args.post_probe:
            probe(args, host, guest)
        cleanup(args, host, guest)
        return 0

    if args.cmd == "cycle":
        attach(args, env, host, guest)
        probe(args, host, guest)
        banner("DETACH ORDER")
        print(f"order={args.order} lazy_host={args.lazy_host} lazy_guest={args.lazy_guest}")
        if args.order == "guest-first":
            detach_guest(args, guest)
            detach_host(args, host)
        elif args.order == "host-first":
            detach_host(args, host)
            detach_guest(args, guest)
        elif args.order == "guest-only":
            detach_guest(args, guest)
        elif args.order == "host-only":
            detach_host(args, host)
        else:
            raise AssertionError(args.order)
        reconcile_detach(args, host, guest)
        probe(args, host, guest)
        cleanup(args, host, guest)
        return 0

    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
