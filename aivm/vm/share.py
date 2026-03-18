"""Virtiofs share inspection, attach, and guest-side mount reconciliation.

This module holds the explicit host/guest boundary-extension logic used when
folders are shared into VMs.
"""

from __future__ import annotations

import shlex
import tempfile
import time
import xml.etree.ElementTree as ET
from pathlib import Path

from loguru import logger

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args, virsh_system_cmd
from ..util import CmdError, run_cmd

log = logger


def _is_virtiofs_filesystem(fs: ET.Element) -> bool:
    """Return True only for filesystem devices backed by virtiofs."""
    driver = fs.find('driver')
    if driver is None:
        return False
    return driver.attrib.get('type', '').strip().lower() == 'virtiofs'


def _dumpxml_text(
    cfg: AgentVMConfig,
    *,
    use_sudo: bool,
    summary: str,
    detail: str = '',
) -> str | None:
    mgr = CommandManager.current()
    res = mgr.submit(
        virsh_system_cmd('dumpxml', cfg.vm.name),
        sudo=use_sudo,
        role='read',
        check=False,
        capture=True,
        summary=summary,
        detail=detail,
    ).result()
    if res.code != 0 or not res.stdout.strip():
        return None
    return res.stdout


def vm_has_virtiofs_shared_memory(
    cfg: AgentVMConfig, *, use_sudo: bool = True
) -> bool | None:
    """Check if domain XML includes shared memory backing required by virtiofs."""
    xml_text = _dumpxml_text(
        cfg,
        use_sudo=use_sudo,
        summary=f'Inspect VM shared-memory backing for {cfg.vm.name}',
        detail='Read domain XML to verify memfd/shared backing for virtiofs.',
    )
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return None
    mb = root.find('.//memoryBacking')
    if mb is None:
        return False
    src = mb.find('source')
    acc = mb.find('access')
    src_type = (src.attrib.get('type', '') if src is not None else '').strip()
    acc_mode = (acc.attrib.get('mode', '') if acc is not None else '').strip()
    return src_type == 'memfd' and acc_mode == 'shared'


def vm_has_share(
    cfg: AgentVMConfig, source_dir: str, tag: str, *, use_sudo: bool = True
) -> bool:
    cfg = cfg.expanded_paths()
    if not source_dir or not tag:
        return False
    xml_text = _dumpxml_text(
        cfg,
        use_sudo=use_sudo,
        summary=f'Inspect VM filesystem mappings for {cfg.vm.name}',
        detail='Read domain XML to look for the requested virtiofs source/tag pair.',
    )
    if not xml_text:
        return False
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return False
    want_src = str(Path(source_dir).resolve())
    want_tag = tag
    for fs in root.findall('.//devices/filesystem'):
        if not _is_virtiofs_filesystem(fs):
            continue
        src = fs.find('source')
        tgt = fs.find('target')
        src_dir = src.attrib.get('dir', '') if src is not None else ''
        tgt_dir = tgt.attrib.get('dir', '') if tgt is not None else ''
        if src_dir == want_src and tgt_dir == want_tag:
            return True
    return False


def vm_share_mappings(
    cfg: AgentVMConfig, *, use_sudo: bool = True
) -> list[tuple[str, str]]:
    """Return virtiofs filesystem mappings as (source_dir, target_tag)."""
    xml_text = _dumpxml_text(
        cfg,
        use_sudo=use_sudo,
        summary=f'Inspect VM virtiofs mappings for {cfg.vm.name}',
        detail='Read domain XML to enumerate current virtiofs source/tag mappings.',
    )
    if not xml_text:
        return []
    try:
        root = ET.fromstring(xml_text)
    except Exception:
        return []
    mappings: list[tuple[str, str]] = []
    for fs in root.findall('.//devices/filesystem'):
        if not _is_virtiofs_filesystem(fs):
            continue
        src = fs.find('source')
        tgt = fs.find('target')
        src_dir = src.attrib.get('dir', '') if src is not None else ''
        tgt_dir = tgt.attrib.get('dir', '') if tgt is not None else ''
        if src_dir or tgt_dir:
            mappings.append((src_dir, tgt_dir))
    return mappings


def attach_vm_share(
    cfg: AgentVMConfig,
    source_dir: str,
    tag: str,
    *,
    dry_run: bool = False,
    vm_running: bool | None = None,
) -> None:
    """Attach a virtiofs share mapping to an existing VM definition."""
    cfg = cfg.expanded_paths()
    if not source_dir:
        raise RuntimeError('Share source_dir is empty.')
    source_dir = str(Path(source_dir).resolve())
    if not tag:
        raise RuntimeError(
            'Share tag is empty; cannot attach filesystem mapping.'
        )
    if dry_run:
        log.info(
            'DRYRUN: attach virtiofs share source={} tag={}', source_dir, tag
        )
        return
    xml = f"""<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs'/>
  <source dir='{source_dir}'/>
  <target dir='{tag}'/>
</filesystem>
"""
    with tempfile.NamedTemporaryFile('w', delete=False) as f:
        f.write(xml)
        tmp = f.name
    mgr = CommandManager.current()
    if vm_running is None:
        state = (
            mgr.submit(
                ['virsh', 'domstate', cfg.vm.name],
                sudo=True,
                role='read',
                check=False,
                capture=True,
                summary=f'Check whether VM {cfg.vm.name} is running before live attach',
            )
            .stdout.strip()
            .lower()
        )
        is_running = 'running' in state
    else:
        is_running = bool(vm_running)
    attach_cmd = (
        ['virsh', 'attach-device', cfg.vm.name, tmp, '--live', '--config']
        if is_running
        else ['virsh', 'attach-device', cfg.vm.name, tmp, '--config']
    )
    attach_summary = (
        f'Attach virtiofs device to running VM {cfg.vm.name}'
        if is_running
        else f'Attach virtiofs device to VM config for {cfg.vm.name}'
    )
    res = mgr.submit(
        attach_cmd,
        sudo=True,
        role='modify',
        check=False,
        capture=True,
        summary=attach_summary,
        detail=f'source={source_dir} tag={tag}',
    ).result()
    if res.code == 0:
        return
    msg = ((res.stderr or '') + '\n' + (res.stdout or '')).lower()
    if 'target already exists' in msg:
        current = vm_share_mappings(cfg, use_sudo=True)
        if any(src == source_dir and tgt == tag for src, tgt in current):
            log.info(
                'Virtiofs mapping already present for vm={} source={} tag={}; treating attach as satisfied.',
                cfg.vm.name,
                source_dir,
                tag,
            )
            return
    raise CmdError(attach_cmd, res)


def detach_vm_share(
    cfg: AgentVMConfig, source_dir: str, tag: str, *, dry_run: bool = False
) -> bool:
    """Detach a virtiofs share mapping from an existing VM definition."""
    cfg = cfg.expanded_paths()
    if not source_dir or not tag:
        return False
    source_dir = str(Path(source_dir).resolve())
    xml = f"""<filesystem type='mount' accessmode='passthrough'>
  <driver type='virtiofs'/>
  <source dir='{source_dir}'/>
  <target dir='{tag}'/>
</filesystem>
"""
    if dry_run:
        log.info(
            'DRYRUN: detach virtiofs share source={} tag={}', source_dir, tag
        )
        return True
    with tempfile.NamedTemporaryFile('w', delete=False) as f:
        f.write(xml)
        tmp = f.name
    state = (
        run_cmd(
            ['virsh', 'domstate', cfg.vm.name],
            sudo=True,
            sudo_action='read',
            check=False,
            capture=True,
        )
        .stdout.strip()
        .lower()
    )
    is_running = 'running' in state
    detach_cmd = (
        ['virsh', 'detach-device', cfg.vm.name, tmp, '--live', '--config']
        if is_running
        else ['virsh', 'detach-device', cfg.vm.name, tmp, '--config']
    )
    res = run_cmd(
        detach_cmd,
        sudo=True,
        sudo_action='modify',
        check=False,
        capture=True,
    )
    if res.code != 0:
        msg = ((res.stderr or '') + '\n' + (res.stdout or '')).lower()
        if 'not found' in msg or 'no matching device' in msg:
            return False
        raise CmdError(detach_cmd, res)
    return True


def ensure_share_mounted(
    cfg: AgentVMConfig,
    ip: str,
    *,
    guest_dst: str,
    tag: str,
    read_only: bool = False,
    dry_run: bool = False,
) -> None:
    cfg = cfg.expanded_paths()
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    if not guest_dst:
        raise RuntimeError('Share guest_dst is empty.')
    if not tag:
        raise RuntimeError('Share tag is empty.')
    mount_cmd = (
        f'sudo -n mount -t virtiofs -o ro {shlex.quote(tag)} {shlex.quote(guest_dst)}'
        if read_only
        else f'sudo -n mount -t virtiofs {shlex.quote(tag)} {shlex.quote(guest_dst)}'
    )
    remount_cmd = (
        f'sudo -n mount -o remount,ro {shlex.quote(guest_dst)}'
        if read_only
        else f'sudo -n mount -o remount,rw {shlex.quote(guest_dst)}'
    )
    remote = (
        'set -euo pipefail; '
        f'sudo -n mkdir -p {shlex.quote(guest_dst)}; '
        f'if mountpoint -q {shlex.quote(guest_dst)}; then '
        f'opts="$(findmnt -n -o OPTIONS --target {shlex.quote(guest_dst)} 2>/dev/null || true)"; '
        f'case ",$opts," in *,{"ro" if read_only else "rw"},*) : ;; *) {remount_cmd} ;; esac; '
        'else '
        f'{mount_cmd}; '
        'fi'
    )
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=5,
            batch_mode=True,
        ),
        f'{cfg.vm.user}@{ip}',
        remote,
    ]
    if dry_run:
        log.info('DRYRUN: {}', ' '.join(cmd))
        return
    max_attempts = 12
    retry_sleep_s = 2.0
    for attempt in range(1, max_attempts + 1):
        res = run_cmd(cmd, sudo=False, check=False, capture=True, timeout=20)
        if res.code == 0:
            if attempt > 1:
                log.info(
                    'Guest share mount became ready after {} attempt(s): tag={} dst={}',
                    attempt,
                    tag,
                    guest_dst,
                )
            return
        err = (res.stderr or '').strip() or f'command exited with {res.code}'
        if attempt == 1:
            log.warning(
                'Guest share mount is not ready yet (tag={} dst={}). '
                'Will retry up to {:.0f}s. Initial error: {}',
                tag,
                guest_dst,
                (max_attempts - 1) * retry_sleep_s,
                err,
            )
        elif attempt < max_attempts:
            log.info(
                'Retrying guest share mount ({}/{}): tag={} dst={}',
                attempt,
                max_attempts,
                tag,
                guest_dst,
            )
        if attempt < max_attempts:
            time.sleep(retry_sleep_s)
            continue
        raise RuntimeError(
            'Failed to mount shared folder inside guest after retries.\n'
            f'VM: {cfg.vm.name}\n'
            f'IP: {ip}\n'
            f'Tag: {tag}\n'
            f'Guest destination: {guest_dst}\n'
            f'Last error: {err}\n'
            'Try running the command again; if it keeps failing, inspect guest '
            'kernel logs (e.g. dmesg) and verify virtiofs support in the image.'
        ) from CmdError(cmd, res)
