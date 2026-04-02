"""Shared-root attachment helpers: host bind, VM mapping, and guest bind management."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args
from ..vm import attach_vm_share, vm_share_mappings
from ..vm.share import SHARED_ROOT_VIRTIOFS_TAG, ResolvedAttachment
from .resolve import ATTACHMENT_ACCESS_RO, ATTACHMENT_ACCESS_RW

SHARED_ROOT_GUEST_MOUNT_ROOT = '/mnt/aivm-shared'


def _shared_root_host_dir(cfg: AgentVMConfig) -> Path:
    return Path(cfg.paths.base_dir) / cfg.vm.name / 'shared-root'


def _shared_root_host_target(cfg: AgentVMConfig, token: str) -> Path:
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '-', str(token or '').strip()).strip('-')
    if not safe:
        raise RuntimeError('shared-root attachment token is empty.')
    return _shared_root_host_dir(cfg) / safe


def _shared_root_guest_mount_cmd(
    cfg: AgentVMConfig,
    ip: str,
    *,
    read_only: bool,
) -> list[str]:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    mount_cmd = (
        f'sudo -n mount -t virtiofs -o ro {shlex.quote(SHARED_ROOT_VIRTIOFS_TAG)} '
        f'{shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}'
        if read_only
        else f'sudo -n mount -t virtiofs {shlex.quote(SHARED_ROOT_VIRTIOFS_TAG)} '
        f'{shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}'
    )
    remount_cmd = (
        f'sudo -n mount -o remount,ro {shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}'
        if read_only
        else f'sudo -n mount -o remount,rw {shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}'
    )
    remote = (
        'set -euo pipefail; '
        f'sudo -n mkdir -p {shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}; '
        f'if mountpoint -q {shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)}; then '
        f'opts="$(findmnt -n -o OPTIONS --target {shlex.quote(SHARED_ROOT_GUEST_MOUNT_ROOT)} 2>/dev/null || true)"; '
        f'case ",$opts," in *,{"ro" if read_only else "rw"},*) : ;; *) {remount_cmd} ;; esac; '
        'else '
        f'{mount_cmd}; '
        'fi'
    )
    return [
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


def _ensure_shared_root_parent_dir(
    cfg: AgentVMConfig,
    *,
    dry_run: bool,
) -> None:
    if dry_run:
        print(
            f'DRYRUN: would create shared-root parent directory {_shared_root_host_dir(cfg)}'
        )
        return
    mgr = CommandManager.current()
    with mgr.intent(
        'Prepare shared-root mapping',
        why='libvirt needs the shared-root export directory to exist before the VM definition can use it.',
        role='modify',
    ):
        with mgr.step(
            'Prepare shared-root parent directory',
            why='Create the host-side shared-root export directory used by virtiofs.',
            approval_scope=f'shared-root-parent:{cfg.vm.name}',
        ):
            mgr.submit(
                ['mkdir', '-p', str(_shared_root_host_dir(cfg))],
                sudo=True,
                role='modify',
                summary='Create shared-root parent directory',
                detail=f'target={_shared_root_host_dir(cfg)}',
            )


def _mount_source_compare_candidates(raw_source: str) -> list[str]:
    """Return plausible path-like interpretations of a ``findmnt SOURCE`` value.

    ``findmnt -o SOURCE`` is not stable across bind-mount backends. For the
    same host bind mount it may report:

    * a literal source path,
    * a literal path with a bracketed subpath suffix, or
    * a backing device or dataset name with a bracketed subpath suffix.

    This helper expands one raw SOURCE string into a short list of candidates
    that can be compared against the expected host source path. The original
    value is kept first, and when a ``[...]`` suffix is present we also expose
    the prefix and the bracket payload.
    """
    raw = str(raw_source or '').strip()
    if not raw:
        return []
    candidates: list[str] = []

    def _add(value: str) -> None:
        item = value.strip()
        if item and item not in candidates:
            candidates.append(item)

    _add(raw)
    if raw.endswith(']') and '[' in raw:
        prefix, bracket = raw.rsplit('[', 1)
        _add(prefix)
        _add(bracket[:-1])
    return candidates


@dataclass(frozen=True)
class FindmntTargetInfo:
    source: str = ''
    root: str = ''
    fstype: str = ''
    code: int = 1

    @property
    def is_mountpoint(self) -> bool:
        return self.code == 0 and bool(self.source or self.root or self.fstype)


def _parse_findmnt_pairs(stdout: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for token in shlex.split(stdout or ''):
        if '=' not in token:
            continue
        key, value = token.split('=', 1)
        values[key.strip().upper()] = value
    return values


def _probe_findmnt_target_source(target: Path) -> FindmntTargetInfo:
    """Read the current source/root metadata for a mount target."""
    mgr = CommandManager.current()
    with mgr.intent(
        'Inspect mount metadata',
        why='Read the current mount metadata before deciding whether host-side repair is needed.',
        role='read',
        visible=False,
    ):
        with mgr.step(
            'Inspect shared-root host bind state',
            why='Determine whether the VM-specific bind target already points at the requested host folder.',
            approval_scope=f'shared-root-host-findmnt:{target}',
        ):
            res = mgr.run(
                ['findmnt', '-P', '-n', '-o', 'SOURCE,ROOT,FSTYPE', '--target', str(target)],
                sudo=True,
                role='read',
                check=False,
                capture=True,
                summary='Inspect current source for host bind target',
                detail=f'target={target}',
            )
    values = _parse_findmnt_pairs(res.stdout or '')
    return FindmntTargetInfo(
        source=values.get('SOURCE', ''),
        root=values.get('ROOT', ''),
        fstype=values.get('FSTYPE', ''),
        code=res.code,
    )


def _shared_root_host_bind_matches_source(
    expected_source: Path,
    target: Path,
    probe: FindmntTargetInfo,
) -> bool:
    expected = str(expected_source.resolve())
    for raw_value in (probe.source, probe.root):
        for candidate in _mount_source_compare_candidates(raw_value):
            try:
                candidate_abs = str(Path(candidate).resolve())
            except Exception:
                candidate_abs = candidate
            if candidate_abs == expected:
                return True
    try:
        source_stat = expected_source.stat()
        target_stat = target.stat()
    except OSError:
        return False
    return (
        source_stat.st_dev == target_stat.st_dev
        and source_stat.st_ino == target_stat.st_ino
    )


def _ensure_shared_root_host_bind(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    yes: bool,
    dry_run: bool,
    allow_disruptive_rebind: bool = True,
) -> Path:
    """Ensure the host-side shared-root bind target exists and points at the requested folder.

    Shared-root mode exposes one virtiofs export to the guest and then bind
    mounts per-attachment host folders underneath that export. This helper is
    responsible for the host-side half of that arrangement:

    * verify the requested source directory exists,
    * inspect the current bind target state,
    * accept already-correct binds without disruption, and
    * otherwise repair the bind target so it points at the requested source.

    The key restore bug fixed here is that ``findmnt SOURCE`` may describe a
    correct bind in non-literal forms such as ``/path[/subpath]`` or
    ``device[/subpath]``. Automatic restore must treat those as healthy matches
    instead of assuming the bind is stale and skipping guest-side repair.

    When ``allow_disruptive_rebind`` is ``False`` the function may still accept
    an already-correct bind, but it will refuse to replace a mismatched mount.
    That is the behavior used during best-effort automatic restore, where we
    want to avoid unexpectedly tearing down a mount the user may still care
    about.
    """
    del yes
    mgr = CommandManager.current()
    source_dir = str(Path(attachment.source_dir).resolve())
    source = Path(source_dir)
    if not source.exists() or not source.is_dir():
        raise RuntimeError(
            f'shared-root source must be an existing directory: {source_dir}'
        )

    target = _shared_root_host_target(cfg, attachment.tag)
    if dry_run:
        print(
            f'DRYRUN: would bind-mount {source_dir} -> {target} for shared-root mode'
        )
        return target

    probe = _probe_findmnt_target_source(target)
    is_mountpoint = probe.is_mountpoint

    if is_mountpoint:
        # findmnt SOURCE for bind mounts is not stable across filesystems.
        # Accept the mount as healthy when SOURCE/ROOT candidates or stat
        # identity show that the existing bind already exposes the requested
        # source.
        if _shared_root_host_bind_matches_source(source, target, probe):
            return target
        if not allow_disruptive_rebind:
            raise RuntimeError(
                'Refusing to replace existing shared-root host bind mount during automatic restore '
                f'(target={target}, expected_source={source_dir}, actual_source={probe.source or "unknown"}, '
                f'actual_root={probe.root or "unknown"}, actual_fstype={probe.fstype or "unknown"}). '
                'Use an explicit attach/detach command to reconcile this mount.'
            )

    source_q = shlex.quote(source_dir)
    target_q = shlex.quote(str(target))

    # Final shell-side idempotence guard: if target is already a mountpoint and
    # stat(source) == stat(target), do not issue another bind mount. This avoids
    # stacking duplicate identical layers even if a prior probe was stale or a
    # concurrent path already repaired the target.
    mount_if_needed_script = (
        'set -euo pipefail; '
        f'if mountpoint -q {target_q}; then '
        f'src_stat="$(stat -Lc %d:%i {source_q} 2>/dev/null || true)"; '
        f'dst_stat="$(stat -Lc %d:%i {target_q} 2>/dev/null || true)"; '
        'if [ -n "$src_stat" ] && [ "$src_stat" = "$dst_stat" ]; then '
        'exit 0; '
        'fi; '
        'fi; '
        f'mount --bind {source_q} {target_q}'
    )

    with mgr.step(
        'Prepare host bind targets',
        why='Ensure the shared-root export directories exist and the VM-specific bind target points at the requested host folder.',
        approval_scope=f'shared-root-host-bind:{cfg.vm.name}:{attachment.tag}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(_shared_root_host_dir(cfg))],
            sudo=True,
            role='modify',
            summary='Create shared-root parent directory',
            detail=f'target={_shared_root_host_dir(cfg)}',
        )
        mgr.submit(
            ['mkdir', '-p', str(target)],
            sudo=True,
            role='modify',
            summary='Create project-specific host bind target',
            detail=f'target={target}',
        )

        if is_mountpoint:
            repair_script = (
                'set -euo pipefail; '
                f'src_stat="$(stat -Lc %d:%i {source_q} 2>/dev/null || true)"; '
                f'dst_stat="$(stat -Lc %d:%i {target_q} 2>/dev/null || true)"; '
                'if mountpoint -q ' + target_q + '; then '
                'if [ -n "$src_stat" ] && [ "$src_stat" = "$dst_stat" ]; then '
                'exit 0; '
                'fi; '
                'fi; '
                f'msg="$(umount {target_q} 2>&1 || true)"; '
                'if [ -n "$msg" ]; then '
                'msg_lc="$(printf "%s" "$msg" | tr "[:upper:]" "[:lower:]")"; '
                'case "$msg_lc" in '
                '*"not mounted"*|*"target is busy"*|*"transport endpoint is not connected"*) '
                'if printf "%s" "$msg_lc" | grep -q "not mounted"; then '
                ':; '
                'else '
                f'umount -l {target_q}; '
                'fi ;; '
                '*) printf "%s\\n" "$msg" >&2; exit 1 ;; '
                'esac; '
                'fi; '
                f'if mountpoint -q {target_q}; then '
                f'src_stat="$(stat -Lc %d:%i {source_q} 2>/dev/null || true)"; '
                f'dst_stat="$(stat -Lc %d:%i {target_q} 2>/dev/null || true)"; '
                'if [ -n "$src_stat" ] && [ "$src_stat" = "$dst_stat" ]; then '
                'exit 0; '
                'fi; '
                'fi; '
                f'mount --bind {source_q} {target_q}'
            )
            mgr.submit(
                ['bash', '-c', repair_script],
                sudo=True,
                role='modify',
                summary='Replace stale host bind target with requested source',
                detail=(
                    f'target={target} expected_source={source_dir} '
                    f'actual_source={probe.source or "unknown"}'
                ),
            )
        else:
            mgr.submit(
                ['bash', '-c', mount_if_needed_script],
                sudo=True,
                role='modify',
                summary='Bind requested host folder to shared-root target',
                detail=f'source={source_dir} target={target}',
            )

    return target


def _ensure_shared_root_vm_mapping(
    cfg: AgentVMConfig,
    *,
    yes: bool,
    dry_run: bool,
    vm_running: bool | None = None,
) -> None:
    """Ensure the VM exposes the shared-root virtiofs export.

    In shared-root mode all per-folder guest mounts ultimately come from one
    libvirt virtiofs mapping rooted at ``_shared_root_host_dir(cfg)`` and tagged
    with ``SHARED_ROOT_VIRTIOFS_TAG``. This helper checks whether that mapping
    already exists, first without sudo and then with sudo if needed, and only
    attaches it when absent.
    """
    del yes
    mgr = CommandManager.current()
    source = str(_shared_root_host_dir(cfg))
    tag = SHARED_ROOT_VIRTIOFS_TAG
    with mgr.step(
        'Inspect shared-root VM mapping',
        why='Check whether the current VM definition already includes the shared-root virtiofs device.',
        approval_scope=f'shared-root-vm-inspect:{cfg.vm.name}',
    ):
        mappings = vm_share_mappings(cfg, use_sudo=False)
    if any(src == source and t == tag for src, t in mappings):
        return
    with mgr.step(
        'Inspect shared-root VM mapping with libvirt privileges',
        why='Some hosts require privileged libvirt access to read the effective filesystem mapping state.',
        approval_scope=f'shared-root-vm-inspect-sudo:{cfg.vm.name}',
    ):
        mappings = vm_share_mappings(cfg, use_sudo=True)
    if any(src == source and t == tag for src, t in mappings):
        return
    with mgr.step(
        'Ensure VM virtiofs mapping',
        why='Attach the shared-root virtiofs device so the guest can reach the shared-root export.',
        approval_scope=f'shared-root-vm-map:{cfg.vm.name}',
    ):
        attach_vm_share(
            cfg,
            source,
            tag,
            dry_run=dry_run,
            vm_running=vm_running,
        )


def _ensure_shared_root_guest_bind(
    cfg: AgentVMConfig,
    ip: str,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
) -> None:
    """Ensure the guest destination is bound to the requested shared-root source.

    This is the guest-side half of shared-root reconciliation. It mounts the
    shared-root virtiofs export inside the VM if needed, bind-mounts the
    per-attachment subdirectory to ``attachment.guest_dst``, and verifies both
    the resulting source and the expected read/write mode. The verification is
    intentionally defensive because guest ``findmnt`` output for bind mounts can
    vary across kernels and filesystems.
    """
    mgr = CommandManager.current()
    source_in_guest = str(
        PurePosixPath(SHARED_ROOT_GUEST_MOUNT_ROOT)
        / (attachment.tag or '').strip()
    )
    expected_root = str(PurePosixPath('/') / (attachment.tag or '').strip())
    expected_virtiofs_source = f'{SHARED_ROOT_VIRTIOFS_TAG}[{expected_root}]'
    if not attachment.tag:
        raise RuntimeError('shared-root attachment token is empty.')
    remount_cmd = (
        f'sudo -n mount -o remount,bind,ro {shlex.quote(attachment.guest_dst)}'
        if attachment.access == ATTACHMENT_ACCESS_RO
        else f'sudo -n mount -o remount,bind,rw {shlex.quote(attachment.guest_dst)}'
    )
    desired_opt = (
        ATTACHMENT_ACCESS_RO
        if attachment.access == ATTACHMENT_ACCESS_RO
        else ATTACHMENT_ACCESS_RW
    )
    script = (
        'set -euo pipefail; '
        f'if [ ! -d {shlex.quote(source_in_guest)} ]; then '
        f'echo "shared-root source missing in guest: {source_in_guest}" >&2; '
        'exit 2; '
        'fi; '
        f'if mountpoint -q {shlex.quote(attachment.guest_dst)}; then '
        f'cur="$(findmnt -n -o SOURCE --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        f'cur_root="$(findmnt -n -o ROOT --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        f'if [ "$cur" = {shlex.quote(source_in_guest)} ]; then '
        ':; '
        f'elif [ "$cur" = {shlex.quote(expected_virtiofs_source)} ]; then '
        ':; '
        f'elif [ "$cur" = "none" ] && [ "$cur_root" = {shlex.quote(expected_root)} ]; then '
        ':; '
        'elif [ "$cur" = "none" ]; then '
        f'src_stat="$(stat -Lc %d:%i {shlex.quote(source_in_guest)} 2>/dev/null || true)"; '
        f'cur_stat="$(stat -Lc %d:%i {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        'if [ -n "$src_stat" ] && [ "$src_stat" = "$cur_stat" ]; then :; else '
        f'sudo -n umount {shlex.quote(attachment.guest_dst)}; '
        'fi; '
        'else '
        f'sudo -n umount {shlex.quote(attachment.guest_dst)}; '
        'fi; '
        'fi; '
        f'if ! mkdir_err="$(sudo -n mkdir -p {shlex.quote(attachment.guest_dst)} 2>&1)"; then '
        'if printf "%s" "$mkdir_err" | grep -qi "transport endpoint is not connected"; then '
        f'sudo -n umount -l {shlex.quote(attachment.guest_dst)} >/dev/null 2>&1 || true; '
        f'sudo -n mkdir -p {shlex.quote(attachment.guest_dst)}; '
        'else '
        'printf "%s\\n" "$mkdir_err" >&2; '
        'exit 2; '
        'fi; '
        'fi; '
        f'if mountpoint -q {shlex.quote(attachment.guest_dst)}; then '
        f'opts="$(findmnt -n -o OPTIONS --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        f'case ",$opts," in *,{desired_opt},*) : ;; *) {remount_cmd} ;; esac; '
        'else '
        f'sudo -n mount --bind {shlex.quote(source_in_guest)} {shlex.quote(attachment.guest_dst)}; '
        f'{remount_cmd}; '
        'fi; '
        f'final_src="$(findmnt -n -o SOURCE --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        f'final_root="$(findmnt -n -o ROOT --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        'final_src_stat=""; '
        'final_dst_stat=""; '
        'source_ok=0; '
        f'if [ "$final_src" = {shlex.quote(source_in_guest)} ]; then '
        'source_ok=1; '
        f'elif [ "$final_src" = {shlex.quote(expected_virtiofs_source)} ]; then '
        'source_ok=1; '
        f'elif [ "$final_src" = "none" ] && [ "$final_root" = {shlex.quote(expected_root)} ]; then '
        'source_ok=1; '
        'elif [ "$final_src" = "none" ]; then '
        f'final_src_stat="$(stat -Lc %d:%i {shlex.quote(source_in_guest)} 2>/dev/null || true)"; '
        f'final_dst_stat="$(stat -Lc %d:%i {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        'if [ -n "$final_src_stat" ] && [ "$final_src_stat" = "$final_dst_stat" ]; then '
        'source_ok=1; '
        'fi; '
        'fi; '
        'if [ "$source_ok" -ne 1 ]; then '
        'echo "shared-root bind verification failed: unexpected source at guest destination" >&2; '
        'echo "  expected: '
        f'{source_in_guest}" >&2; '
        'echo "  actual:   $final_src" >&2; '
        'echo "  expected root: '
        f'{expected_root}" >&2; '
        'echo "  actual root:   $final_root" >&2; '
        'if [ -n "$final_src_stat" -o -n "$final_dst_stat" ]; then '
        'echo "  expected stat: $final_src_stat" >&2; '
        'echo "  actual stat:   $final_dst_stat" >&2; '
        'fi; '
        'exit 2; '
        'fi; '
        f'final_opts="$(findmnt -n -o OPTIONS --target {shlex.quote(attachment.guest_dst)} 2>/dev/null || true)"; '
        f'case ",$final_opts," in *,{desired_opt},*) : ;; *) '
        'echo "shared-root bind verification failed: unexpected mount options at guest destination" >&2; '
        'echo "  expected option: '
        f'{desired_opt}" >&2; '
        'echo "  actual options: $final_opts" >&2; '
        'exit 2; '
        'esac'
    )
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=5,
            batch_mode=True,
        ),
        f'{cfg.vm.user}@{ip}',
        script,
    ]
    if dry_run:
        from loguru import logger

        logger.info('DRYRUN: {}', ' '.join(shlex.quote(c) for c in cmd))
        return
    mount_cmd = _shared_root_guest_mount_cmd(
        cfg,
        ip,
        read_only=(attachment.access == ATTACHMENT_ACCESS_RO),
    )
    with mgr.step(
        'Mount and verify inside guest',
        why='Mount the shared-root export inside the guest, bind it to the requested destination, and verify the resulting source and access mode.',
        approval_scope=(
            f'shared-root-guest-bind:{cfg.vm.name}:{attachment.guest_dst}'
        ),
    ):
        mgr.submit(
            mount_cmd,
            sudo=False,
            role='modify',
            check=True,
            capture=True,
            timeout=20,
            summary='Mount shared-root inside guest',
            detail=(
                f'tag={SHARED_ROOT_VIRTIOFS_TAG} '
                f'destination={SHARED_ROOT_GUEST_MOUNT_ROOT} '
                f'access={attachment.access}'
            ),
        )
        res = mgr.submit(
            cmd,
            sudo=False,
            role='modify',
            check=False,
            capture=True,
            timeout=20,
            summary='Bind guest destination to shared source and verify source/options',
            detail=(
                f'source={source_in_guest} destination={attachment.guest_dst} '
                f'access={attachment.access}'
            ),
        ).result()
    if res.code != 0:
        raise RuntimeError(
            'Failed to bind-mount shared-root attachment inside guest. You may need to stop the VM to run detatch\n'
            f'VM: {cfg.vm.name}\n'
            f'Guest source: {source_in_guest}\n'
            f'Guest destination: {attachment.guest_dst}\n'
            f'Error: {(res.stderr or res.stdout).strip()}'
        )


def _detach_shared_root_host_bind(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    yes: bool,
    dry_run: bool,
) -> None:
    target = _shared_root_host_target(cfg, attachment.tag)
    if dry_run:
        print(f'DRYRUN: would unmount shared-root host bind target {target}')
        return
    mgr = CommandManager.current()
    with mgr.intent(
        'Detach shared-root host bind mount',
        why='Remove the host-side bind target used for the shared-root attachment.',
        role='modify',
    ):
        mounted = (
            mgr.run(
                ['mountpoint', '-q', str(target)],
                sudo=True,
                role='read',
                check=False,
                capture=True,
                summary=f'Inspect shared-root bind target {target}',
            ).code
            == 0
        )
        if mounted:
            res = mgr.run(
                ['umount', str(target)],
                sudo=True,
                check=False,
                capture=True,
                summary=f'Unmount shared-root bind target {target}',
            )
            if res.code != 0:
                msg = ((res.stderr or '') + '\n' + (res.stdout or '')).lower()
                if 'not mounted' in msg:
                    # Benign race: the target was observed as mounted but is
                    # already gone by the time we try to unmount it.
                    pass
                elif 'target is busy' in msg:
                    raise RuntimeError(
                        'Shared-root host bind target is busy and was not detached. '
                        f'target={target}. '
                        'Refusing automatic lazy-unmount during normal detach because it can '
                        'leave callers in a disconnected working-directory state. '
                        'Leave the tree / stop any holders and retry detach. '
                        'Future direction: add an explicit lazy/force detach mode for '
                        'orphaned mount cleanup.'
                    )
                elif 'transport endpoint is not connected' in msg:
                    raise RuntimeError(
                        'Shared-root host bind target appears stale and was not detached. '
                        f'target={target}. '
                        'Refusing automatic lazy-unmount during normal detach. '
                        'Clean up the stale mount explicitly and retry. '
                        'Future direction: add an explicit lazy/force detach mode for '
                        'orphaned mount cleanup.'
                    )
                else:
                    raise RuntimeError(
                        f'Failed to unmount shared-root host bind target {target}: '
                        f'{(res.stderr or res.stdout).strip()}'
                    )
        mgr.run(
            ['rmdir', str(target)],
            sudo=True,
            role='modify',
            check=False,
            capture=True,
            summary=f'Remove shared-root bind target directory {target}',
        )


def _detach_shared_root_guest_bind(
    cfg: AgentVMConfig,
    ip: str,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
) -> None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    source_in_guest = str(
        PurePosixPath(SHARED_ROOT_GUEST_MOUNT_ROOT)
        / (attachment.tag or '').strip()
    )
    script = (
        'set -euo pipefail; '
        f'if mountpoint -q {shlex.quote(attachment.guest_dst)}; then '
        f'sudo umount {shlex.quote(attachment.guest_dst)}; '
        'fi; '
        f'if mountpoint -q {shlex.quote(source_in_guest)}; then '
        f'sudo umount {shlex.quote(source_in_guest)}; '
        'fi'
    )
    cmd = [
        'ssh',
        *ssh_base_args(ident, strict_host_key_checking='accept-new'),
        f'{cfg.vm.user}@{ip}',
        script,
    ]
    if dry_run:
        from loguru import logger
        logger.info('DRYRUN: {}', ' '.join(shlex.quote(c) for c in cmd))
        return
    res = CommandManager.current().run(cmd, sudo=False, check=False, capture=True)
    if res.code != 0:
        raise RuntimeError(
            'Failed to unmount shared-root attachment inside guest.\n'
            f'VM: {cfg.vm.name}\n'
            f'Guest source: {source_in_guest}\n'
            f'Guest destination: {attachment.guest_dst}\n'
            f'Error: {(res.stderr or res.stdout).strip()}'
        )
