"""Attachment resolution helpers: path computation, mode/access normalisation."""

from __future__ import annotations

import hashlib
from pathlib import Path, PurePosixPath

from ..config import AgentVMConfig
from ..store import find_attachment_for_vm, load_store
from ..vm.share import (
    AttachmentAccess,
    AttachmentMode,
    ResolvedAttachment,
    _ensure_share_tag_len,
)

# Attachment mode constants (string aliases for mode values)
ATTACHMENT_MODE_SHARED = AttachmentMode.SHARED.value
ATTACHMENT_MODE_SHARED_ROOT = AttachmentMode.SHARED_ROOT.value
ATTACHMENT_MODE_GIT = AttachmentMode.GIT.value

# Attachment access constants (string aliases for access values)
ATTACHMENT_ACCESS_RW = AttachmentAccess.RW.value
ATTACHMENT_ACCESS_RO = AttachmentAccess.RO.value

# Attachment mode and access sets for validation
ATTACHMENT_MODES = {
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    ATTACHMENT_MODE_GIT,
}
ATTACHMENT_ACCESS_MODES = {
    ATTACHMENT_ACCESS_RW,
    ATTACHMENT_ACCESS_RO,
}


def _default_primary_guest_dst(host_src: Path) -> str:
    """Compute the default primary guest destination for an attachment.

    Uses the lexical absolute path normally (expanduser + absolute).
    If the host source is itself a symlink, uses the resolved real path as the
    primary destination (so the mount target is the canonical location).
    """
    lexical = host_src.expanduser().absolute()
    if lexical.is_symlink():
        return str(host_src.resolve())
    return str(lexical)


def _resolve_guest_dst(host_src: Path, guest_dst_opt: str) -> str:
    guest_dst_opt = (guest_dst_opt or '').strip()
    if guest_dst_opt:
        return guest_dst_opt
    return _default_primary_guest_dst(host_src)


def _host_symlink_lexical_path(host_src: Path) -> str | None:
    """If host_src (after expanduser/absolute) is a symlink, return its lexical path. Else None."""
    lexical = host_src.expanduser().absolute()
    if lexical.is_symlink():
        return str(lexical)
    return None


def _compute_mirror_home_symlink(
    cfg: AgentVMConfig,
    host_src: Path,
    guest_dst: str,
    *,
    is_default_dst: bool,
) -> str | None:
    """Compute the mirror-home symlink path if behavior.mirror_shared_home_folders is enabled.

    Returns the guest symlink path to create (pointing to guest_dst), or None if
    the mirror should not be created.

    Skip conditions:
    - mirror_shared_home_folders setting is false
    - attachment used an explicit custom guest_dst
    - host path is not under the host user home
    - guest home equals host home (no point mirroring)
    - mirror path would be identical to primary guest_dst
    """
    if not is_default_dst:
        return None
    host_home = Path.home()
    guest_home = PurePosixPath('/home') / cfg.vm.user
    if str(guest_home) == str(host_home):
        return None
    lexical = host_src.expanduser().absolute()
    try:
        rel = lexical.relative_to(host_home)
    except ValueError:
        return None  # host path not under host home
    mirror = str(guest_home / rel)
    if mirror == guest_dst:
        return None  # mirror path same as primary, no-op
    return mirror


def _normalize_attachment_mode(mode: str) -> AttachmentMode:
    raw = str(mode or '').strip().lower()
    if not raw:
        return AttachmentMode(ATTACHMENT_MODE_SHARED_ROOT)
    aliases = {
        'clone': ATTACHMENT_MODE_GIT,
        'cloned': ATTACHMENT_MODE_GIT,
        'repo': ATTACHMENT_MODE_GIT,
        'git': ATTACHMENT_MODE_GIT,
        'sharedroot': ATTACHMENT_MODE_SHARED_ROOT,
        'shared_root': ATTACHMENT_MODE_SHARED_ROOT,
        'root': ATTACHMENT_MODE_SHARED_ROOT,
        ATTACHMENT_MODE_SHARED: ATTACHMENT_MODE_SHARED,
        ATTACHMENT_MODE_SHARED_ROOT: ATTACHMENT_MODE_SHARED_ROOT,
    }
    resolved = aliases.get(raw, raw)
    if resolved not in ATTACHMENT_MODES:
        allowed = ', '.join(sorted(ATTACHMENT_MODES))
        raise RuntimeError(f'--mode must be one of: {allowed}')
    return AttachmentMode(resolved)


def _normalize_attachment_access(access: str) -> AttachmentAccess:
    raw = str(access or '').strip().lower()
    if not raw:
        return AttachmentAccess(ATTACHMENT_ACCESS_RW)
    aliases = {
        'readonly': ATTACHMENT_ACCESS_RO,
        'read-only': ATTACHMENT_ACCESS_RO,
        'read_only': ATTACHMENT_ACCESS_RO,
        ATTACHMENT_ACCESS_RO: ATTACHMENT_ACCESS_RO,
        'readwrite': ATTACHMENT_ACCESS_RW,
        'read-write': ATTACHMENT_ACCESS_RW,
        'read_write': ATTACHMENT_ACCESS_RW,
        ATTACHMENT_ACCESS_RW: ATTACHMENT_ACCESS_RW,
    }
    resolved = aliases.get(raw, raw)
    if resolved not in ATTACHMENT_ACCESS_MODES:
        allowed = ', '.join(sorted(ATTACHMENT_ACCESS_MODES))
        raise RuntimeError(f'--access must be one of: {allowed}')
    return AttachmentAccess(resolved)


def _resolve_attachment(
    cfg: AgentVMConfig,
    cfg_path: Path,
    host_src: Path,
    guest_dst_opt: str,
    mode_opt: str = '',
    access_opt: str = '',
) -> ResolvedAttachment:
    source_dir = str(host_src.resolve())
    guest_dst = _resolve_guest_dst(host_src, guest_dst_opt)
    tag = _ensure_share_tag_len('', host_src, set())
    mode = _normalize_attachment_mode(mode_opt)
    access = _normalize_attachment_access(access_opt)
    reg = load_store(cfg_path)
    att = find_attachment_for_vm(reg, host_src, cfg.vm.name)
    if att is not None:
        saved_mode = _normalize_attachment_mode(att.mode)
        saved_access = _normalize_attachment_access(att.access)
        if mode_opt and mode != saved_mode:
            raise RuntimeError(
                'Attachment mode mismatch for existing folder attachment.\n'
                f'VM: {cfg.vm.name}\n'
                f'Host folder: {host_src}\n'
                f'Saved mode: {saved_mode}\n'
                f'Requested mode: {mode}\n'
                'Changing attachment mode requires an explicit detach + reattach.\n'
                'Run:\n'
                f'  aivm detach {host_src}\n'
                f'  aivm attach {host_src} --mode {mode}'
            )
        if access_opt and access != saved_access:
            raise RuntimeError(
                'Attachment access mismatch for existing folder attachment.\n'
                f'VM: {cfg.vm.name}\n'
                f'Host folder: {host_src}\n'
                f'Saved access: {saved_access}\n'
                f'Requested access: {access}\n'
                'Changing attachment access requires an explicit detach + reattach.\n'
                'Run:\n'
                f'  aivm detach {host_src}\n'
                f'  aivm attach {host_src} --access {access}'
            )
        if not mode_opt and att.mode:
            mode = saved_mode
        if not access_opt:
            access = saved_access
        if not guest_dst_opt and att.guest_dst:
            guest_dst = att.guest_dst
        if att.tag:
            tag = att.tag
    if access == ATTACHMENT_ACCESS_RO and mode == ATTACHMENT_MODE_GIT:
        raise NotImplementedError(
            'Read-only attachments are currently only implemented for '
            f"'{ATTACHMENT_MODE_SHARED}' and '{ATTACHMENT_MODE_SHARED_ROOT}' modes. "
            f'Requested mode: {mode}'
        )
    if mode == ATTACHMENT_MODE_GIT:
        tag = ''
    return ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=mode,
        access=access,
        source_dir=source_dir,
        guest_dst=guest_dst,
        tag=tag,
    )
