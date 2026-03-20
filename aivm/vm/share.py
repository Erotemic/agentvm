"""Virtiofs share inspection, attach, and guest-side mount reconciliation.

This module holds the explicit host/guest boundary-extension logic used when
folders are shared into VMs.
"""

from __future__ import annotations

import hashlib
import re
import shlex
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path

from loguru import logger

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args, virsh_system_cmd
from ..util import CmdError, run_cmd

log = logger


class AttachmentMode(StrEnum):
    """Attachment mode for VM shared folders.

    These modes determine how host directories are shared with the VM:
    - SHARED: Direct virtiofs mount of the host directory
    - SHARED_ROOT: VM-specific bind mount via shared-root directory
    - GIT: Git clone of the host repo into the guest
    """

    SHARED = 'shared'
    SHARED_ROOT = 'shared-root'
    GIT = 'git'


class AttachmentAccess(StrEnum):
    """Attachment access mode for VM shared folders.

    These modes determine the access permissions for shared folders:
    - RW: Read-write access (default)
    - RO: Read-only access
    """

    RW = 'rw'
    RO = 'ro'


# Shared-root tag constant
SHARED_ROOT_VIRTIOFS_TAG = 'aivm-shared-root'


@dataclass(frozen=True)
class ResolvedAttachment:
    """A resolved attachment with all fields computed for VM access.

    This is used throughout the drift detection and attachment reconcile flows.
    """

    vm_name: str
    mode: AttachmentMode = AttachmentMode.SHARED
    access: AttachmentAccess = AttachmentAccess.RW
    source_dir: str = ''
    guest_dst: str = ''
    tag: str = ''


def _auto_share_tag_for_path(host_src: Path, existing_tags: set[str]) -> str:
    """Generate a unique tag for a host path that doesn't conflict with existing tags.

    This is used for tag alignment when attaching shares to avoid virtiofs conflicts.
    """
    max_len = 36
    raw = re.sub(r'[^A-Za-z0-9_.-]+', '-', host_src.name or 'hostcode').strip(
        '-'
    )
    base = f'hostcode-{raw}' if raw else 'hostcode'
    base = base[:max_len]
    if base not in existing_tags:
        return base
    suffix = hashlib.sha1(str(host_src).encode('utf-8')).hexdigest()[:8]
    tag = f'{base[: max_len - 1 - len(suffix)]}-{suffix}'
    if tag not in existing_tags:
        return tag
    idx = 2
    while True:
        tail = f'-{suffix[:5]}-{idx}'
        cand = f'{base[: max_len - len(tail)]}{tail}'
        if cand not in existing_tags:
            return cand
        idx += 1


def _ensure_share_tag_len(
    tag: str, host_src: Path, existing_tags: set[str]
) -> str:
    """Ensure a tag is within the 36-character limit, generating a new one if needed.

    Args:
        tag: The proposed tag.
        host_src: The host source path (used for tag generation if needed).
        existing_tags: Set of tags already in use.

    Returns:
        A tag that is at most 36 characters and doesn't conflict with existing tags.
    """
    tag = (tag or '').strip()
    if tag and len(tag) <= 36:
        return tag
    return _auto_share_tag_for_path(host_src, existing_tags)


def align_attachment_tag_with_mappings(
    att: 'ResolvedAttachment', host_src: Path, mappings: list[tuple[str, str]]
) -> 'ResolvedAttachment':
    """Align an attachment's tag with existing mappings to avoid conflicts.

    This ensures consistent tagging across multiple attachments and avoids
    tag collisions that could cause virtiofs issues.

    Args:
        att: The attachment to align.
        host_src: The host source path (used for tag generation if needed).
        mappings: Current VM mappings.

    Returns:
        A new ResolvedAttachment with an aligned tag.
    """
    existing_tags = {tag for _, tag in mappings if tag}
    tag = _ensure_share_tag_len(att.tag, host_src, existing_tags)
    for src, existing_tag in mappings:
        if src == att.source_dir and existing_tag:
            tag = existing_tag
            break
    has_share = any(src == att.source_dir and t == tag for src, t in mappings)
    if not has_share:
        for src, existing_tag in mappings:
            if existing_tag == tag and src != att.source_dir:
                tag = _auto_share_tag_for_path(host_src, existing_tags)
                break
    return replace(att, tag=tag)


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
