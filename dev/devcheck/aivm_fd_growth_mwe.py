#!/usr/bin/env python3
r"""
Standalone MWE for testing FD growth risk in a single shared-root virtiofs design.

Purpose
-------
This script is a design artifact, not a reproduction of AIVM internals.

It tests the specific hypothesis that matters for product direction:

    If host-side bind mounts inside a live shared-root virtiofs export are kept
    stable, and only guest-side exposure binds churn, do the relevant virtiofsd
    / qemu FD counts plateau instead of growing without bound?

The script can run in two policies:

* stable-host
    Create host-side staged bind mounts once, keep them mounted for the whole
    run, and repeatedly attach/detach only the guest-visible bind mounts.

* churn-host
    Recreate and detach the host-side staged bind mounts every iteration,
    in addition to guest-side bind churn.

This lets the same standalone artifact compare the risky topology against the
proposed safer topology.

Model
-----
The test topology is:

    host_src[i]
        A real host directory with test files.

    host_stage[i]
        A bind mount of host_src[i] placed *inside* the already-existing host
        shared-root export directory.

    guest_stage[i]
        The same staged directory as seen inside the guest via the existing
        virtiofs shared-root mount.

    guest_dst[i]
        A guest-visible bind mount of guest_stage[i]. This is the thing we
        intentionally churn in stable-host mode.

Path layout
-----------
For runid=example and slot i:

    host_scratch_root = /tmp/aivm-fd-mwe/<runid>
    host_src[i]       = /tmp/aivm-fd-mwe/<runid>/src/slot-XX
    host_stage[i]     = <host_export_dir>/__fd_mwe__/<runid>/slot-XX
    guest_stage[i]    = <guest_shared_base>/__fd_mwe__/<runid>/slot-XX
    guest_dst[i]      = /tmp/aivm-fd-mwe/<runid>/dst/slot-XX

What this script verifies
-------------------------
1. The guest can see the expected staged content.
2. Host -> guest updates are visible immediately.
3. Guest -> host updates are visible immediately.
4. Guest bind attach/detach succeeds repeatedly.
5. Relevant virtiofsd / qemu processes serving the shared-root export can be
   identified from the host side.
6. Both process-global FD counts and run-scoped FD counts under this
   runid's subtree can be sampled after each iteration.
7. Baseline-relative deltas can be reported for the current policy, which is
   more informative than comparing absolute totals from long-lived worker
   processes.

Preconditions
-------------
This script does NOT create the base virtiofs shared-root export. It assumes:

* the VM is already running
* the guest already has a virtiofs mount like /mnt/aivm-shared
* the host already has the corresponding export directory like
  /var/lib/libvirt/aivm/<vm>/shared-root

Typical workflow
----------------
1. Start from a clean VM.
2. Re-establish the base shared-root export used by your environment.
3. Run one stable-host loop and inspect whether run-scoped post-detach
   counts remain near the stable-host baseline.
4. Run a separate churn-host loop from a fresh VM or otherwise equivalent
   clean baseline and compare the post-detach deltas.

Examples
--------
Show computed paths and preflight checks:

    python dev/devcheck/aivm_fd_growth_mwe.py info \
      --ssh-target aivm-2404 \
      --vm-name aivm-2404 \
      --guest-shared-base /mnt/aivm-shared \
      --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
      --runid fd-clean-1 \
      --num-slots 2

Stable host-side binds, churn only guest exposure binds:

    python dev/devcheck/aivm_fd_growth_mwe.py loop \
      --ssh-target aivm-2404 \
      --vm-name aivm-2404 \
      --guest-shared-base /mnt/aivm-shared \
      --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
      --runid fd-stable-1 \
      --num-slots 4 \
      --iterations 20 \
      --host-policy stable-host

Host and guest both churn every iteration:

    python dev/devcheck/aivm_fd_growth_mwe.py loop \
      --ssh-target aivm-2404 \
      --vm-name aivm-2404 \
      --guest-shared-base /mnt/aivm-shared \
      --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
      --runid fd-churn-1 \
      --num-slots 4 \
      --iterations 20 \
      --host-policy churn-host

Cleanup a runid:

    python dev/devcheck/aivm_fd_growth_mwe.py cleanup \
      --ssh-target aivm-2404 \
      --vm-name aivm-2404 \
      --guest-shared-base /mnt/aivm-shared \
      --host-export-dir /var/lib/libvirt/aivm/aivm-2404/shared-root \
      --runid fd-stable-1 \
      --num-slots 4

Conclusion
----------

This MWE was built to answer one design question:

Does FD usage behave better when host-side shared-root bind mounts are kept
stable and only guest-visible bind mounts churn?

The current state is more cautious than an earlier reading of this artifact.
The process-global ``total_fd_count`` metric, by itself, is too coarse to
cleanly distinguish the two policies, because the same long-lived ``virtiofsd``
/ qemu processes can survive across runs and accumulate unrelated history.

This version of the MWE therefore reports additional run-scoped measurements:

* process-global FD counts for the relevant holders, for continuity
* run-scoped FD counts filtered to this runid's host-stage subtree and host
  scratch subtree
* baseline-relative deltas for both the active phase and the post-detach phase
* post-detach mount state for host stages and guest-visible binds

Interpretation
~~~~~~~~~~~~~~
This MWE still supports several important conclusions:

* ``stable-host`` is functionally viable: guest attach/detach cycles work and
  bidirectional host/guest visibility works.
* ``churn-host`` still reproduces host-side busy-unmount behavior under the
  live shared-root export.
* fast interactive unshare should not depend on host-side bind teardown under
  the live export.

What this MWE does **not** yet prove automatically is that ``stable-host`` has
lower final process-global FD pressure than ``churn-host``. A stronger claim
now requires looking at the run-scoped measurements from clean baselines.

Design implication
------------------

The main design recommendation that remains supported is:

Do not make rapid host-side bind mount teardown under the live shared-root
export part of the normal interactive attach/detach lifecycle.

Instead, prefer:

* stable host-side staged bind mounts for currently shared folders
* guest-side bind mount attach/detach as the fast visibility control
* deferred host-side garbage collection or teardown outside the interactive path

If repeated runs from fresh or equivalent baselines show that ``stable-host``
returns to a flat post-detach run-scoped baseline while ``churn-host`` leaves
behind growing run-scoped counts or mounted residue, that would be stronger
support for the ``stable-host / guest-only churn`` design direction.
"""
from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence


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


def banner(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


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


def host_findmnt(args: argparse.Namespace, path: str) -> CmdResult:
    return host_run(args, ["findmnt", "-M", path, "-o", "TARGET,SOURCE,FSTYPE,OPTIONS", "-n"])


def guest_findmnt(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"findmnt -M {q(path)} -o TARGET,SOURCE,FSTYPE,OPTIONS -n")


def host_is_exact_mountpoint(args: argparse.Namespace, path: str) -> bool:
    res = host_run(args, ["findmnt", "-M", path, "-o", "TARGET", "-n"])
    return res.ok and res.stdout.strip() == path


def guest_is_exact_mountpoint(args: argparse.Namespace, path: str) -> bool:
    res = guest_run(args, f"findmnt -M {q(path)} -o TARGET -n")
    return res.ok and res.stdout.strip() == path


def host_path_exists(args: argparse.Namespace, path: str) -> bool:
    res = host_run(args, ["bash", "-lc", f"test -e {q(path)}"])
    return res.ok


def guest_path_exists(args: argparse.Namespace, path: str, use_sudo: bool = False) -> bool:
    res = guest_run(args, f"test -e {q(path)}", use_sudo=use_sudo)
    return res.ok


def parse_fuser_pids(stdout: str) -> dict[str, str]:
    """
    Parse fuser -vm output into {pid: command}.
    """
    found: dict[str, str] = {}
    for raw in stdout.splitlines():
        line = raw.strip()
        if not line or line.startswith("USER") or line.endswith(":"):
            continue
        parts = line.split()
        if len(parts) < 4:
            continue
        user, pid, access, command = parts[:4]
        if pid.isdigit():
            found[pid] = command
    return found


def holder_pids_for_path(args: argparse.Namespace, path: str) -> dict[str, str]:
    res = host_run(args, ["bash", "-lc", f"fuser -vm {q(path)} 2>&1 || true"])
    return parse_fuser_pids(res.stdout + ("\n" + res.stderr if res.stderr else ""))


def fd_count_for_pid(args: argparse.Namespace, pid: str) -> int | None:
    res = host_run(
        args,
        ["bash", "-lc", f"test -d /proc/{q(pid)}/fd && ls /proc/{q(pid)}/fd | wc -l || true"],
    )
    text = (res.stdout or "").strip()
    if not text:
        return None
    try:
        return int(text.splitlines()[-1].strip())
    except Exception:
        return None


def command_for_pid(args: argparse.Namespace, pid: str) -> str | None:
    res = host_run(args, ["ps", "-p", pid, "-o", "args="])
    text = (res.stdout or "").strip()
    return text or None


def fd_entries_for_pid(args: argparse.Namespace, pid: str) -> list[dict[str, str]]:
    code = """
import json
import os
import sys

pid = sys.argv[1]
fd_dir = f"/proc/{pid}/fd"
items = []

if os.path.isdir(fd_dir):
    def sort_key(name: str):
        return (0, int(name)) if name.isdigit() else (1, name)

    for name in sorted(os.listdir(fd_dir), key=sort_key):
        path = os.path.join(fd_dir, name)
        try:
            target = os.readlink(path)
        except OSError:
            continue
        items.append({"fd": name, "target": target})

print(json.dumps(items))
"""
    res = host_run(args, ["python3", "-c", code, pid])
    if not res.ok or not (res.stdout or "").strip():
        return []
    try:
        data = json.loads(res.stdout)
    except Exception:
        return []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def normalize_fd_target(target: str) -> str:
    text = target.strip()
    if text.endswith(" (deleted)"):
        text = text[:-10]
    return text


def fd_target_matches_prefix(target: str, prefix: str) -> bool:
    norm = normalize_fd_target(target)
    return norm == prefix or norm.startswith(prefix + "/")


def fd_target_matches_any_prefix(target: str, prefixes: Sequence[str]) -> bool:
    return any(fd_target_matches_prefix(target, prefix) for prefix in prefixes)


def discover_relevant_holders(
    args: argparse.Namespace,
    state: dict[str, Any],
    pid_hints: Sequence[str] | None = None,
) -> dict[str, str]:
    holders: dict[str, str] = {}
    probe_paths = [state["paths"]["host_stage_root"]] + [slot["host_stage"] for slot in state["slots"]]
    for path in probe_paths:
        holders.update(holder_pids_for_path(args, path))
    relevant = {
        pid: cmd
        for pid, cmd in holders.items()
        if ("virtiofsd" in cmd) or ("qemu-system" in cmd)
    }
    for pid in pid_hints or []:
        if pid in relevant:
            continue
        cmd = command_for_pid(args, pid)
        if cmd and (("virtiofsd" in cmd) or ("qemu-system" in cmd)):
            relevant[pid] = cmd
    return {pid: relevant[pid] for pid in sorted(relevant)}


def scoped_fd_metrics(
    entries_by_pid: dict[str, list[dict[str, str]]],
    state: dict[str, Any],
    sample_limit: int = 5,
) -> dict[str, Any]:
    stage_root = state["paths"]["host_stage_root"]
    scratch_root = state["paths"]["host_scratch_root"]
    run_prefixes = [stage_root, scratch_root]

    stage_root_fd_counts: dict[str, int] = {}
    scratch_root_fd_counts: dict[str, int] = {}
    run_scoped_fd_counts: dict[str, int] = {}
    run_scoped_targets_sample: dict[str, list[str]] = {}
    slot_stage_fd_totals: dict[str, int] = {slot["name"]: 0 for slot in state["slots"]}

    for pid, entries in entries_by_pid.items():
        stage_count = 0
        scratch_count = 0
        run_count = 0
        run_samples: list[str] = []
        for entry in entries:
            raw_target = str(entry.get("target", ""))
            target = normalize_fd_target(raw_target)
            if fd_target_matches_prefix(target, stage_root):
                stage_count += 1
            if fd_target_matches_prefix(target, scratch_root):
                scratch_count += 1
            if fd_target_matches_any_prefix(target, run_prefixes):
                run_count += 1
                if len(run_samples) < sample_limit:
                    run_samples.append(target)
            for slot in state["slots"]:
                if fd_target_matches_prefix(target, slot["host_stage"]):
                    slot_stage_fd_totals[slot["name"]] += 1
        stage_root_fd_counts[pid] = stage_count
        scratch_root_fd_counts[pid] = scratch_count
        run_scoped_fd_counts[pid] = run_count
        if run_samples:
            run_scoped_targets_sample[pid] = run_samples

    return {
        "scoped_prefixes": {
            "host_stage_root": stage_root,
            "host_scratch_root": scratch_root,
        },
        "stage_root_fd_counts": stage_root_fd_counts,
        "stage_root_total_fd_count": sum(stage_root_fd_counts.values()),
        "scratch_root_fd_counts": scratch_root_fd_counts,
        "scratch_root_total_fd_count": sum(scratch_root_fd_counts.values()),
        "run_scoped_fd_counts": run_scoped_fd_counts,
        "run_scoped_total_fd_count": sum(run_scoped_fd_counts.values()),
        "run_scoped_targets_sample": run_scoped_targets_sample,
        "slot_stage_fd_totals": slot_stage_fd_totals,
    }


def relevant_holder_snapshot(
    args: argparse.Namespace,
    state: dict[str, Any],
    label: str,
    pid_hints: Sequence[str] | None = None,
) -> dict[str, Any]:
    relevant = discover_relevant_holders(args, state, pid_hints=pid_hints)
    entries_by_pid = {pid: fd_entries_for_pid(args, pid) for pid in sorted(relevant)}
    fd_counts = {
        pid: len(entries_by_pid.get(pid, [])) if entries_by_pid.get(pid) is not None else None
        for pid in sorted(relevant)
    }
    total = sum(v for v in fd_counts.values() if isinstance(v, int))

    relevant_pids = set(relevant)
    lsof_stage_root_entries = lsof_entries_under_path(args, state["paths"]["host_stage_root"])
    lsof_scratch_root_entries = lsof_entries_under_path(args, state["paths"]["host_scratch_root"])
    slot_lsof_counts: dict[str, Any] = {}
    for slot in state["slots"]:
        slot_lsof_counts[slot["name"]] = {
            "host_stage": summarize_lsof_entries(
                lsof_entries_under_path(args, slot["host_stage"]),
                relevant_pids,
            ),
            "host_src": summarize_lsof_entries(
                lsof_entries_under_path(args, slot["host_src"]),
                relevant_pids,
            ),
        }

    lsof_stage_root = summarize_lsof_entries(lsof_stage_root_entries, relevant_pids)
    lsof_scratch_root = summarize_lsof_entries(lsof_scratch_root_entries, relevant_pids)
    return {
        "label": label,
        "probe_paths": [state["paths"]["host_stage_root"]] + [slot["host_stage"] for slot in state["slots"]],
        "holders": relevant,
        "fd_counts": fd_counts,
        "total_fd_count": total,
        "lsof_stage_root": lsof_stage_root,
        "lsof_scratch_root": lsof_scratch_root,
        "lsof_run_total_count": lsof_stage_root["record_count"] + lsof_scratch_root["record_count"],
        "lsof_run_relevant_total_count": (
            lsof_stage_root["relevant_record_count"] + lsof_scratch_root["relevant_record_count"]
        ),
        "slot_lsof_counts": slot_lsof_counts,
    }


def snapshot_delta(current: dict[str, Any], baseline: dict[str, Any] | None) -> dict[str, Any] | None:
    if baseline is None:
        return None
    metrics = [
        "total_fd_count",
        "lsof_run_total_count",
        "lsof_run_relevant_total_count",
    ]
    out = {"baseline_label": baseline.get("label")}
    for metric in metrics:
        cur = current.get(metric)
        base = baseline.get(metric)
        if isinstance(cur, int) and isinstance(base, int):
            out[f"{metric}_delta"] = cur - base

    current_stage = current.get("lsof_stage_root", {})
    baseline_stage = baseline.get("lsof_stage_root", {})
    current_scratch = current.get("lsof_scratch_root", {})
    baseline_scratch = baseline.get("lsof_scratch_root", {})
    for prefix, cur_block, base_block in [
        ("lsof_stage_root", current_stage, baseline_stage),
        ("lsof_scratch_root", current_scratch, baseline_scratch),
    ]:
        for field in ["record_count", "relevant_record_count"]:
            cur = cur_block.get(field)
            base = base_block.get(field)
            if isinstance(cur, int) and isinstance(base, int):
                out[f"{prefix}_{field}_delta"] = cur - base
    return out


def current_mount_state(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    host_stage_mounted = {
        slot["name"]: host_is_exact_mountpoint(args, slot["host_stage"])
        for slot in state["slots"]
    }
    guest_dst_mounted = {
        slot["name"]: guest_is_exact_mountpoint(args, slot["guest_dst"])
        for slot in state["slots"]
    }
    return {
        "host_stage_mounted": host_stage_mounted,
        "guest_dst_mounted": guest_dst_mounted,
        "num_host_stage_mounted": sum(1 for mounted in host_stage_mounted.values() if mounted),
        "num_guest_dst_mounted": sum(1 for mounted in guest_dst_mounted.values() if mounted),
    }


def lsof_entries_under_path(args: argparse.Namespace, path: str) -> list[dict[str, str]]:
    if not host_path_exists(args, path):
        return []
    res = host_run(
        args,
        [
            "bash",
            "-lc",
            f"lsof -n -w -Fpcfn0 +D {q(path)} 2>/dev/null || true",
        ],
    )
    fields = [part for part in res.stdout.split("\x00") if part]
    entries: list[dict[str, str]] = []
    current_pid = ""
    current_cmd = ""
    current_fd = ""
    for field in fields:
        tag = field[:1]
        value = field[1:]
        if tag == "p":
            current_pid = value
            current_fd = ""
        elif tag == "c":
            current_cmd = value
        elif tag == "f":
            current_fd = value
        elif tag == "n":
            entries.append(
                {
                    "pid": current_pid,
                    "command": current_cmd,
                    "fd": current_fd,
                    "name": value,
                }
            )
    return entries


def summarize_lsof_entries(
    entries: list[dict[str, str]],
    relevant_pids: set[str],
    sample_limit: int = 12,
) -> dict[str, Any]:
    total_count = len(entries)
    by_pid: dict[str, int] = {}
    relevant_count = 0
    relevant_by_pid: dict[str, int] = {}
    samples: list[dict[str, str]] = []
    for entry in entries:
        pid = entry.get("pid", "")
        by_pid[pid] = by_pid.get(pid, 0) + 1
        if pid in relevant_pids:
            relevant_count += 1
            relevant_by_pid[pid] = relevant_by_pid.get(pid, 0) + 1
            if len(samples) < sample_limit:
                samples.append(entry)
    return {
        "record_count": total_count,
        "by_pid": by_pid,
        "relevant_record_count": relevant_count,
        "relevant_by_pid": relevant_by_pid,
        "relevant_samples": samples,
    }


def collect_dirty_state(args: argparse.Namespace, state: dict[str, Any]) -> dict[str, Any]:
    mount_state = current_mount_state(args, state)
    dirty: dict[str, Any] = {
        "host_stage_root_exists": host_path_exists(args, state["paths"]["host_stage_root"]),
        "host_scratch_root_exists": host_path_exists(args, state["paths"]["host_scratch_root"]),
        "guest_dst_root_exists": guest_path_exists(args, state["paths"]["guest_dst_root"], use_sudo=True),
        "mount_state": mount_state,
        "dirty_reasons": [],
    }
    if dirty["host_stage_root_exists"]:
        dirty["dirty_reasons"].append("host_stage_root_exists")
    if dirty["host_scratch_root_exists"]:
        dirty["dirty_reasons"].append("host_scratch_root_exists")
    if dirty["guest_dst_root_exists"]:
        dirty["dirty_reasons"].append("guest_dst_root_exists")
    if mount_state["num_host_stage_mounted"] > 0:
        dirty["dirty_reasons"].append("host_stage_already_mounted")
    if mount_state["num_guest_dst_mounted"] > 0:
        dirty["dirty_reasons"].append("guest_dst_already_mounted")
    dirty["is_dirty"] = bool(dirty["dirty_reasons"])
    return dirty


def guest_ls_probe(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"ls -la {q(path)} | sed -n '1,40p'", use_sudo=True)


def guest_read_probe(args: argparse.Namespace, path: str) -> CmdResult:
    return guest_run(args, f"cat {q(path)}", use_sudo=True)


def host_read_probe(args: argparse.Namespace, path: str) -> CmdResult:
    return host_run(args, ["bash", "-lc", f"cat {q(path)}"])


def derive_state(args: argparse.Namespace) -> dict[str, Any]:
    if not args.vm_name:
        raise RuntimeError("--vm-name is required")
    if not args.guest_shared_base:
        raise RuntimeError("--guest-shared-base is required")
    if not args.host_export_dir:
        raise RuntimeError("--host-export-dir is required")

    runid = args.runid or uuid.uuid4().hex[:12]
    host_scratch_root = Path("/tmp/aivm-fd-mwe") / runid
    host_src_root = host_scratch_root / "src"
    host_guest_echo_root = host_scratch_root / "guest-echo"
    host_stage_root = Path(args.host_export_dir) / "__fd_mwe__" / runid
    guest_stage_root = Path(args.guest_shared_base) / "__fd_mwe__" / runid
    guest_dst_root = Path("/tmp/aivm-fd-mwe") / runid / "dst"

    slots: list[dict[str, str]] = []
    for idx in range(args.num_slots):
        name = f"slot-{idx:02d}"
        host_src = host_src_root / name
        host_stage = host_stage_root / name
        guest_stage = guest_stage_root / name
        guest_dst = guest_dst_root / name
        host_to_guest = host_src / "host_to_guest.txt"
        guest_to_host = host_src / "guest_to_host.txt"
        sentinel = host_src / "MWE_SENTINEL.txt"
        slots.append(
            {
                "name": name,
                "host_src": str(host_src),
                "host_stage": str(host_stage),
                "guest_stage": str(guest_stage),
                "guest_dst": str(guest_dst),
                "host_to_guest": str(host_to_guest),
                "guest_to_host": str(guest_to_host),
                "sentinel": str(sentinel),
            }
        )

    return {
        "env": {
            "vm_name": args.vm_name,
            "guest_shared_base": args.guest_shared_base,
            "host_export_dir": args.host_export_dir,
            "runid": runid,
            "num_slots": args.num_slots,
            "host_policy": getattr(args, "host_policy", None),
        },
        "paths": {
            "host_scratch_root": str(host_scratch_root),
            "host_src_root": str(host_src_root),
            "host_stage_root": str(host_stage_root),
            "guest_stage_root": str(guest_stage_root),
            "guest_dst_root": str(guest_dst_root),
            "host_guest_echo_root": str(host_guest_echo_root),
        },
        "slots": slots,
    }


def print_state(state: dict[str, Any]) -> None:
    banner("DISCOVERY")
    print(json.dumps(state, indent=2))


def preflight(args: argparse.Namespace, state: dict[str, Any]) -> None:
    banner("PREFLIGHT")
    show_if_any(
        "guest shared base mount",
        guest_run(args, f"findmnt -T {q(state['paths']['guest_stage_root'])} || findmnt -T {q(args.guest_shared_base)}"),
    )
    show_if_any(
        "host export dir exists",
        host_run(args, ["bash", "-lc", f"test -d {q(args.host_export_dir)} && echo ok || (echo missing; exit 1)"]),
    )
    show_if_any(
        "host lsof available",
        host_run(args, ["bash", "-lc", "command -v lsof >/dev/null && echo ok || (echo missing; exit 1)"]),
    )
    if state["slots"]:
        first = state["slots"][0]
        show_if_any(
            "host stage root mount state",
            host_findmnt(args, first["host_stage"]),
        )
        show_if_any(
            "guest dst mount state",
            guest_findmnt(args, first["guest_dst"]),
        )

    dirty = collect_dirty_state(args, state)
    banner("DIRTY PREFLIGHT STATE")
    print(json.dumps(dirty, indent=2))

    should_fail = getattr(args, "fail_dirty_preflight", False) and args.cmd in {"setup", "loop"}
    if should_fail and dirty["is_dirty"]:
        raise RuntimeError(
            "dirty preflight for comparison run: "
            + ", ".join(dirty["dirty_reasons"])
            + ". Use a fresh runid / clean state, or pass --allow-dirty-preflight to inspect anyway."
        )


def prepare_host_sources(args: argparse.Namespace, state: dict[str, Any]) -> None:
    banner("PREPARE HOST SOURCES")
    host_run(args, ["mkdir", "-p", state["paths"]["host_src_root"]], check=True)
    host_run(args, ["mkdir", "-p", state["paths"]["host_guest_echo_root"]], check=True)
    for slot in state["slots"]:
        src = slot["host_src"]
        script = f"""
            set -euo pipefail
            mkdir -p {q(src)}
            printf '%s\n' {q(slot['name'])} > {q(slot['sentinel'])}
            : > {q(slot['host_to_guest'])}
            : > {q(slot['guest_to_host'])}
        """
        show("prepare source " + slot["name"], host_run(args, ["bash", "-lc", script]))


def ensure_host_stages(args: argparse.Namespace, state: dict[str, Any]) -> None:
    banner("ENSURE HOST STAGES")
    host_run(args, ["mkdir", "-p", state["paths"]["host_stage_root"]], check=True)
    for slot in state["slots"]:
        host_run(args, ["mkdir", "-p", slot["host_stage"]], check=True)
        if host_is_exact_mountpoint(args, slot["host_stage"]):
            show_if_any(f"findmnt host stage {slot['name']} (already mounted)", host_findmnt(args, slot["host_stage"]))
            continue
        show(
            f"mount --bind host_src -> host_stage ({slot['name']})",
            host_run(args, ["mount", "--bind", slot["host_src"], slot["host_stage"]]),
        )
        show_if_any(f"findmnt host stage {slot['name']}", host_findmnt(args, slot["host_stage"]))


def detach_host_stages(args: argparse.Namespace, state: dict[str, Any], lazy: bool = False) -> None:
    banner("DETACH HOST STAGES")
    for slot in state["slots"]:
        if not host_is_exact_mountpoint(args, slot["host_stage"]):
            continue
        argv = ["umount"]
        if lazy:
            argv.append("-l")
        argv.append(slot["host_stage"])
        show(f"umount host stage {slot['name']}", host_run(args, argv))
        show_if_any(f"findmnt host stage {slot['name']} (after)", host_findmnt(args, slot["host_stage"]))


def attach_guest_exposures(args: argparse.Namespace, state: dict[str, Any]) -> None:
    banner("ATTACH GUEST EXPOSURES")
    guest_run(args, f"mkdir -p {q(state['paths']['guest_dst_root'])}", use_sudo=True, check=True)
    for slot in state["slots"]:
        guest_run(args, f"mkdir -p {q(slot['guest_dst'])}", use_sudo=True, check=True)
        if guest_is_exact_mountpoint(args, slot["guest_dst"]):
            show_if_any(f"findmnt guest dst {slot['name']} (already mounted)", guest_findmnt(args, slot["guest_dst"]))
            continue
        show(
            f"mount --bind guest_stage -> guest_dst ({slot['name']})",
            guest_run(args, f"mount --bind {q(slot['guest_stage'])} {q(slot['guest_dst'])}", use_sudo=True),
        )
        show_if_any(f"findmnt guest dst {slot['name']}", guest_findmnt(args, slot["guest_dst"]))


def detach_guest_exposures(args: argparse.Namespace, state: dict[str, Any], lazy: bool = False) -> None:
    banner("DETACH GUEST EXPOSURES")
    flag = "-l " if lazy else ""
    for slot in state["slots"]:
        if not guest_is_exact_mountpoint(args, slot["guest_dst"]):
            continue
        show(
            f"umount guest dst {slot['name']}",
            guest_run(args, f"umount {flag}{q(slot['guest_dst'])}", use_sudo=True),
        )
        show_if_any(f"findmnt guest dst {slot['name']} (after)", guest_findmnt(args, slot["guest_dst"]))


def sync_probe_for_slot(args: argparse.Namespace, slot: dict[str, str], iteration: int) -> dict[str, Any]:
    token = f"iter={iteration} slot={slot['name']}"
    host_append = host_run(
        args,
        ["bash", "-lc", f"printf '%s\n' {q('HOST ' + token)} >> {q(slot['host_to_guest'])}"],
    )
    guest_seen = guest_read_probe(args, slot["guest_dst"] + "/host_to_guest.txt")
    guest_append = guest_run(
        args,
        f"printf '%s\n' {q('GUEST ' + token)} >> {q(slot['guest_dst'] + '/guest_to_host.txt')}",
        use_sudo=True,
    )
    host_seen = host_read_probe(args, slot["guest_to_host"])
    sentinel = guest_read_probe(args, slot["guest_dst"] + "/MWE_SENTINEL.txt")

    return {
        "slot": slot["name"],
        "host_append_ok": host_append.ok,
        "guest_read_ok": guest_seen.ok,
        "guest_append_ok": guest_append.ok,
        "host_read_ok": host_seen.ok,
        "sentinel_ok": sentinel.ok and slot["name"] in sentinel.stdout,
        "guest_ls_ok": guest_ls_probe(args, slot["guest_dst"]).ok,
        "host_token_seen_in_guest": token in guest_seen.stdout,
        "guest_token_seen_on_host": token in host_seen.stdout,
    }


def iteration_probe(args: argparse.Namespace, state: dict[str, Any], iteration: int) -> dict[str, Any]:
    slot_results = [sync_probe_for_slot(args, slot, iteration) for slot in state["slots"]]
    active_snapshot = relevant_holder_snapshot(args, state, label=f"iter-{iteration}-active")
    return {
        "iteration": iteration,
        "slot_results": slot_results,
        "active_snapshot": active_snapshot,
    }


def summarize_iteration(result: dict[str, Any]) -> None:
    banner(f"ITERATION {result['iteration']} SUMMARY")
    print(json.dumps(result, indent=2))


def loop(args: argparse.Namespace, state: dict[str, Any]) -> int:
    prepare_host_sources(args, state)

    baseline_label = "pre-loop-no-stages"
    if args.host_policy == "stable-host":
        ensure_host_stages(args, state)
        baseline_label = "post-stable-setup-pre-loop"

    baseline = relevant_holder_snapshot(args, state, label=baseline_label)
    banner("BASELINE HOLDER SNAPSHOT")
    print(json.dumps(baseline, indent=2))

    results: list[dict[str, Any]] = []
    for iteration in range(1, args.iterations + 1):
        banner(f"BEGIN ITERATION {iteration}")
        if args.host_policy == "churn-host":
            ensure_host_stages(args, state)

        attach_guest_exposures(args, state)
        result = iteration_probe(args, state, iteration)
        detach_guest_exposures(args, state, lazy=args.lazy_guest)

        if args.host_policy == "churn-host":
            detach_host_stages(args, state, lazy=args.lazy_host)

        pid_hints = list(result["active_snapshot"]["holders"].keys())
        post_detach_snapshot = relevant_holder_snapshot(
            args,
            state,
            label=f"iter-{iteration}-post-detach",
            pid_hints=pid_hints,
        )
        result["active_delta_from_baseline"] = snapshot_delta(result["active_snapshot"], baseline)
        result["post_detach_snapshot"] = post_detach_snapshot
        result["post_detach_delta_from_baseline"] = snapshot_delta(post_detach_snapshot, baseline)
        result["post_detach_mount_state"] = current_mount_state(args, state)

        results.append(result)
        summarize_iteration(result)

        if args.iteration_sleep > 0:
            time.sleep(args.iteration_sleep)

    banner("FINAL SUMMARY")
    summary = {
        "host_policy": getattr(args, "host_policy", None),
        "iterations": args.iterations,
        "num_slots": args.num_slots,
        "baseline": baseline,
        "results": results,
    }
    print(json.dumps(summary, indent=2))
    return 0


def cleanup(args: argparse.Namespace, state: dict[str, Any]) -> int:
    detach_guest_exposures(args, state, lazy=args.lazy_guest)
    detach_host_stages(args, state, lazy=args.lazy_host)

    banner("REMOVE DIRECTORIES")
    for slot in state["slots"]:
        show_if_any(f"rmdir guest dst {slot['name']}", guest_run(args, f"rmdir {q(slot['guest_dst'])}", use_sudo=True))
        show_if_any(f"rmdir host stage {slot['name']}", host_run(args, ["rmdir", slot["host_stage"]]))
        show_if_any(f"rm -rf host src {slot['name']}", host_run(args, ["rm", "-rf", slot["host_src"]]))

    show_if_any("rmdir guest dst root", guest_run(args, f"rmdir {q(state['paths']['guest_dst_root'])}", use_sudo=True))
    show_if_any("rmdir host stage root", host_run(args, ["rmdir", state["paths"]["host_stage_root"]]))
    show_if_any("rm -rf host scratch root", host_run(args, ["rm", "-rf", state["paths"]["host_scratch_root"]]))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Standalone FD-growth MWE for shared-root attachment designs")
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--ssh-target", required=True, help="for example aivm-2404 or user@ip")
        sp.add_argument("--ssh-port", type=int, default=None)
        sp.add_argument("--ssh-identity", default=None)
        sp.add_argument("--vm-name", required=True)
        sp.add_argument("--guest-shared-base", required=True)
        sp.add_argument("--host-export-dir", required=True)
        sp.add_argument("--runid", default=None)
        sp.add_argument("--num-slots", type=int, default=4)
        sp.add_argument("--host-sudo", action="store_true", default=True)
        sp.add_argument("--no-host-sudo", dest="host_sudo", action="store_false")
        sp.add_argument("--lazy-host", action="store_true")
        sp.add_argument("--lazy-guest", action="store_true")

    info_cmd = sub.add_parser("info")
    add_common(info_cmd)

    setup_cmd = sub.add_parser("setup")
    add_common(setup_cmd)

    loop_cmd = sub.add_parser("loop")
    add_common(loop_cmd)
    loop_cmd.add_argument("--iterations", type=int, default=10)
    loop_cmd.add_argument("--iteration-sleep", type=float, default=0.0)
    loop_cmd.add_argument("--host-policy", choices=["stable-host", "churn-host"], default="stable-host")

    cleanup_cmd = sub.add_parser("cleanup")
    add_common(cleanup_cmd)

    return p

def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    state = derive_state(args)

    if args.cmd == "info":
        print_state(state)
        preflight(args, state)
        return 0

    if args.cmd == "setup":
        print_state(state)
        preflight(args, state)
        prepare_host_sources(args, state)
        ensure_host_stages(args, state)
        return 0

    if args.cmd == "loop":
        print_state(state)
        preflight(args, state)
        return loop(args, state)

    if args.cmd == "cleanup":
        print_state(state)
        return cleanup(args, state)

    raise AssertionError(args.cmd)


if __name__ == "__main__":
    raise SystemExit(main())
