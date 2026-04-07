"""VM update helpers: drift detection, planning, and application."""

from __future__ import annotations

import json
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import virsh_system_cmd
from ..vm.drift import parse_dominfo_hardware as _parse_dominfo_hardware


@dataclass(frozen=True)
class VMUpdateDrift:
    cpus: tuple[int, int] | None = None
    ram_mb: tuple[int, int] | None = None
    disk_bytes: tuple[int, int] | None = None
    disk_path: str = ''
    notes: tuple[str, ...] = ()

    def has_changes(self) -> bool:
        return any((self.cpus, self.ram_mb, self.disk_bytes))


def _bytes_to_gib(size_bytes: int) -> float:
    return float(size_bytes) / float(1024**3)


def _parse_qemu_img_virtual_size(info_json: str) -> int | None:
    try:
        raw = json.loads(info_json or '{}')
    except Exception:
        return None
    size = raw.get('virtual-size')
    if isinstance(size, int) and size > 0:
        return size
    return None


def _parse_vm_disk_path_from_dumpxml(dumpxml_text: str) -> str | None:
    try:
        root = ET.fromstring(dumpxml_text)
    except ET.ParseError:
        return None
    devices = root.find('devices')
    if devices is None:
        return None
    for disk in devices.findall('disk'):
        if disk.get('device') != 'disk':
            continue
        source = disk.find('source')
        if source is None:
            continue
        source_file = (source.get('file') or '').strip()
        if source_file:
            return source_file
    return None


def _parse_vm_network_from_dumpxml(dumpxml_text: str) -> str | None:
    try:
        root = ET.fromstring(dumpxml_text)
    except ET.ParseError:
        return None
    devices = root.find('devices')
    if devices is None:
        return None
    for iface in devices.findall('interface'):
        if (iface.get('type') or '').strip() != 'network':
            continue
        source = iface.find('source')
        if source is None:
            continue
        network_name = (source.get('network') or '').strip()
        if network_name:
            return network_name
    return None


def _resolve_vm_disk_path(
    cfg: AgentVMConfig, *, use_sudo: bool
) -> tuple[Path, tuple[str, ...]]:
    notes: list[str] = []
    expected = (
        Path(cfg.paths.base_dir)
        / cfg.vm.name
        / 'images'
        / f'{cfg.vm.name}.qcow2'
    )
    res = CommandManager.current().run(
        virsh_system_cmd('dumpxml', cfg.vm.name),
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if res.code != 0:
        notes.append(
            'Could not read domain XML; falling back to expected aivm disk path.'
        )
        return expected, tuple(notes)
    xml_path = _parse_vm_disk_path_from_dumpxml(res.stdout)
    if not xml_path:
        notes.append(
            'Domain XML has no file-backed disk entry; falling back to expected aivm disk path.'
        )
        return expected, tuple(notes)
    return Path(xml_path), tuple(notes)


def _qemu_img_virtual_size_bytes(
    path: Path, *, use_sudo: bool
) -> tuple[int | None, str]:
    res = CommandManager.current().run(
        ['qemu-img', 'info', '--output=json', str(path)],
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if res.code != 0:
        err = (res.stderr or res.stdout or '').strip()
        return None, err
    return _parse_qemu_img_virtual_size(res.stdout), ''


def _parse_domblkinfo_capacity(domblkinfo_text: str) -> int | None:
    for line in (domblkinfo_text or '').splitlines():
        if ':' not in line:
            continue
        key, val = [x.strip() for x in line.split(':', 1)]
        if key.lower() == 'capacity':
            m = re.search(r'(\d+)', val)
            if m:
                return int(m.group(1))
    return None


def _virsh_domblk_capacity_bytes(
    cfg: AgentVMConfig, path_or_target: str, *, use_sudo: bool
) -> int | None:
    res = CommandManager.current().run(
        virsh_system_cmd('domblkinfo', cfg.vm.name, path_or_target),
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if res.code != 0:
        return None
    return _parse_domblkinfo_capacity(res.stdout)


def _vm_update_drift(
    cfg: AgentVMConfig, *, yes: bool
) -> tuple[VMUpdateDrift, bool]:
    """Compute editable drift between config and live libvirt VM state.

    The update flow is intentionally conservative:
    * prefer non-sudo probes first,
    * escalate to sudo only when required,
    * gather diagnostics in ``notes`` instead of failing hard when a probe is
      inconclusive (for example qemu-img lock contention on running VMs).
    """
    notes: list[str] = []
    mgr = CommandManager.current()
    dominfo = mgr.run(
        virsh_system_cmd('dominfo', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
        summary=f'Inspect VM definition {cfg.vm.name} for update planning',
    )
    if dominfo.code != 0:
        dominfo = mgr.run(
            virsh_system_cmd('dominfo', cfg.vm.name),
            sudo=True,
            check=False,
            capture=True,
            summary=f'Inspect VM definition {cfg.vm.name} with sudo for update planning',
        )
    if dominfo.code != 0:
        raise RuntimeError(
            f"VM '{cfg.vm.name}' is not defined (or inaccessible via sudo)."
        )

    cur_cpus, cur_mem_mib = _parse_dominfo_hardware(dominfo.stdout)
    cpus = (
        (cur_cpus, int(cfg.vm.cpus))
        if cur_cpus is not None and cur_cpus != int(cfg.vm.cpus)
        else None
    )
    ram_mb = (
        (cur_mem_mib, int(cfg.vm.ram_mb))
        if cur_mem_mib is not None and cur_mem_mib != int(cfg.vm.ram_mb)
        else None
    )

    state_res = mgr.run(
        virsh_system_cmd('domstate', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
    )
    if state_res.code != 0:
        state_res = mgr.run(
            virsh_system_cmd('domstate', cfg.vm.name),
            sudo=True,
            check=False,
            capture=True,
        )
    vm_running = (
        state_res.code == 0
        and 'running' in (state_res.stdout or '').strip().lower()
    )

    sudo_confirmed = False

    disk_path, disk_notes = _resolve_vm_disk_path(cfg, use_sudo=False)
    if (
        any('Could not read domain XML' in note for note in disk_notes)
        and not sudo_confirmed
    ):
        sudo_confirmed = True
        disk_path, disk_notes = _resolve_vm_disk_path(cfg, use_sudo=True)
    notes.extend(disk_notes)
    cur_disk, qemu_img_err = _qemu_img_virtual_size_bytes(
        disk_path, use_sudo=False
    )
    if cur_disk is None:
        sudo_confirmed = True
        cur_disk, qemu_img_err = _qemu_img_virtual_size_bytes(
            disk_path, use_sudo=True
        )
    if cur_disk is None:
        if (
            qemu_img_err
            and 'failed to get shared "write" lock' in qemu_img_err.lower()
        ):
            notes.append(
                'qemu-img could not inspect disk while VM was running (shared write lock); falling back to virsh domblkinfo.'
            )
        domblk = _virsh_domblk_capacity_bytes(
            cfg, str(disk_path), use_sudo=bool(sudo_confirmed)
        )
        if domblk is None and not sudo_confirmed:
            sudo_confirmed = True
            domblk = _virsh_domblk_capacity_bytes(
                cfg, str(disk_path), use_sudo=True
            )
        cur_disk = domblk
    desired_disk = int(cfg.vm.disk_gb) * (1024**3)
    disk_bytes = (
        (cur_disk, desired_disk)
        if cur_disk is not None and cur_disk != desired_disk
        else None
    )
    if cur_disk is None:
        notes.append(f'Could not determine disk size from {disk_path}.')

    xml = mgr.run(
        virsh_system_cmd('dumpxml', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
        summary=f'Inspect VM XML for {cfg.vm.name} network details',
    )
    if xml.code != 0:
        sudo_confirmed = True
        xml = mgr.run(
            virsh_system_cmd('dumpxml', cfg.vm.name),
            sudo=True,
            check=False,
            capture=True,
            summary=f'Inspect VM XML for {cfg.vm.name} network details with sudo',
        )
    if xml.code == 0:
        live_network = _parse_vm_network_from_dumpxml(xml.stdout)
        want_network = (cfg.network.name or '').strip()
        if live_network and want_network and live_network != want_network:
            notes.append(
                f'Network drift detected (live={live_network}, config={want_network}); auto-update is not implemented for network rebinding.'
            )

    return (
        VMUpdateDrift(
            cpus=cpus,
            ram_mb=ram_mb,
            disk_bytes=disk_bytes,
            disk_path=str(disk_path),
            notes=tuple(notes),
        ),
        vm_running,
    )


def _print_vm_update_plan(cfg: AgentVMConfig, drift: VMUpdateDrift) -> None:
    print(f'Planned VM update for {cfg.vm.name}:')
    if drift.cpus is not None:
        cur, want = drift.cpus
        print(f'  - cpus: {cur} -> {want}')
    if drift.ram_mb is not None:
        cur, want = drift.ram_mb
        print(f'  - ram_mb: {cur} -> {want}')
    if drift.disk_bytes is not None:
        cur, want = drift.disk_bytes
        print(
            f'  - disk_gb: {_bytes_to_gib(cur):.2f} GiB -> {_bytes_to_gib(want):.2f} GiB ({drift.disk_path})'
        )


def _apply_vm_update(
    cfg: AgentVMConfig, drift: VMUpdateDrift, *, dry_run: bool
) -> tuple[bool, bool]:
    changed = False

    # TODO: Should we check for network config drift here too?
    restart_required = False
    if drift.cpus is not None:
        _, want = drift.cpus
        cmd = virsh_system_cmd('setvcpus', cfg.vm.name, str(want), '--config')
        if dry_run:
            print(f'DRYRUN: {" ".join(cmd)}')
        else:
            CommandManager.current().run(
                cmd, sudo=True, check=True, capture=True
            )
            print(f'Updated CPU count to {want}.')
        changed = True
        restart_required = True
    if drift.ram_mb is not None:
        _, want = drift.ram_mb
        kib = int(want) * 1024
        max_cmd = virsh_system_cmd(
            'setmaxmem', cfg.vm.name, str(kib), '--config'
        )
        mem_cmd = virsh_system_cmd('setmem', cfg.vm.name, str(kib), '--config')
        if dry_run:
            print(f'DRYRUN: {" ".join(max_cmd)}')
            print(f'DRYRUN: {" ".join(mem_cmd)}')
        else:
            mgr = CommandManager.current()
            mgr.run(max_cmd, sudo=True, check=True, capture=True)
            mgr.run(mem_cmd, sudo=True, check=True, capture=True)
            print(f'Updated RAM to {want} MiB.')
        changed = True
        restart_required = True
    if drift.disk_bytes is not None:
        cur, want = drift.disk_bytes
        if want < cur:
            raise RuntimeError(
                f'Disk shrink is not supported safely (live={_bytes_to_gib(cur):.2f} GiB, config={_bytes_to_gib(want):.2f} GiB).'
            )
        if want > cur:
            cmd = ['qemu-img', 'resize', drift.disk_path, f'{cfg.vm.disk_gb}G']
            if dry_run:
                print(f'DRYRUN: {" ".join(cmd)}')
            else:
                CommandManager.current().run(
                    cmd, sudo=True, check=True, capture=True
                )
                print(
                    f'Expanded disk to {_bytes_to_gib(want):.2f} GiB at {drift.disk_path}.'
                )
            changed = True
    return changed, restart_required


def _maybe_restart_vm_after_update(
    cfg: AgentVMConfig, *, restart_policy: str, dry_run: bool, yes: bool
) -> None:
    should_restart = False
    if restart_policy == 'always':
        should_restart = True
    elif restart_policy == 'never':
        should_restart = False
    else:
        if yes:
            should_restart = True
        elif sys.stdin.isatty():
            ans = (
                input(
                    'A restart is needed for CPU/RAM changes to take effect now. Restart VM now? [y/N]: '
                )
                .strip()
                .lower()
            )
            should_restart = ans in {'y', 'yes'}
    if not should_restart:
        print(
            f'CPU/RAM updates are saved, but VM {cfg.vm.name} must be restarted for them to take effect.'
        )
        return
    cmd = virsh_system_cmd('reboot', cfg.vm.name)
    if dry_run:
        print(f'DRYRUN: {" ".join(cmd)}')
    else:
        CommandManager.current().run(cmd, sudo=True, check=True, capture=True)
        print(f'Restarted VM {cfg.vm.name}.')
