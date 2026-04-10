"""Persistent attachment helpers.

Host state is authoritative. The desired persistent-attachment manifest is
stored on the host outside the virtiofs export tree, then synced one-way into
the guest-local replay input at /var/lib/aivm/attachments.json. The guest
replay helper only reads that local file and reapplies mounts from there.
"""

from __future__ import annotations

import json
import os
import shlex
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger as log

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..persistent_replay import (
    PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME,
    PERSISTENT_ATTACHMENT_REPLAY_BIN,
    PERSISTENT_ATTACHMENT_REPLAY_SERVICE,
    PERSISTENT_ATTACHMENT_GUEST_STATE_PATH,
    PERSISTENT_ROOT_GUEST_MOUNT_ROOT,
    PERSISTENT_ROOT_VIRTIOFS_TAG,
    persistent_replay_python,
    persistent_replay_service_unit,
)
from ..runtime import require_ssh_identity, ssh_base_args
from ..store import find_attachments_for_vm, load_store, persistent_host_state_dir
from ..vm import attach_vm_share, vm_share_mappings
from ..vm.share import ResolvedAttachment
from .resolve import ATTACHMENT_MODE_PERSISTENT
from .shared_root import _shared_root_host_target


def _persistent_root_host_dir(cfg: AgentVMConfig) -> Path:
    return Path(cfg.paths.base_dir) / cfg.vm.name / 'persistent-root'


@dataclass(frozen=True)
class PersistentAttachmentRecord:
    attachment_id: str
    mode: str
    source_dir: str
    host_lexical_path: str
    shared_root_token: str
    guest_dst: str
    access: str
    enabled: bool = True


def _persistent_host_state_dir(cfg: AgentVMConfig) -> Path:
    # Keep the canonical manifest outside the exported persistent-root tree so
    # the guest replay helper never depends on reading through virtiofs.
    # This lives in user-owned app data, not under the libvirt-managed VM tree.
    return persistent_host_state_dir(cfg.vm.name)


def _persistent_host_manifest_path(cfg: AgentVMConfig) -> Path:
    return _persistent_host_state_dir(cfg) / PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME


def _write_text_if_changed(path: Path, text: str) -> bool:
    new_bytes = text.encode('utf-8')
    if path.exists() and path.read_bytes() == new_bytes:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        'wb',
        dir=str(path.parent),
        delete=False,
    ) as file:
        file.write(new_bytes)
        tmp_name = file.name
    os.replace(tmp_name, path)
    return True


def _persistent_attachment_records_for_vm(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> list[PersistentAttachmentRecord]:
    reg = load_store(cfg_path)
    records: list[PersistentAttachmentRecord] = []
    for att in find_attachments_for_vm(reg, cfg.vm.name):
        if str(att.mode or '').strip() != ATTACHMENT_MODE_PERSISTENT:
            continue
        records.append(
            PersistentAttachmentRecord(
                attachment_id=str(att.tag or att.host_path),
                mode=str(att.mode or ATTACHMENT_MODE_PERSISTENT),
                source_dir=str(att.host_path),
                host_lexical_path=str(att.host_lexical_path or ''),
                shared_root_token=str(att.tag or ''),
                guest_dst=str(att.guest_dst or ''),
                access=str(att.access or 'rw'),
                enabled=True,
            )
        )
    return sorted(
        records, key=lambda rec: (rec.guest_dst, rec.shared_root_token)
    )


def _persistent_attachment_manifest_text(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> str:
    records = _persistent_attachment_records_for_vm(cfg, cfg_path)
    payload = {
        'schema_version': 1,
        'vm_name': cfg.vm.name,
        'shared_root_mount': PERSISTENT_ROOT_GUEST_MOUNT_ROOT,
        'records': [asdict(rec) for rec in records],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + '\n'


def _run_guest_root_script(
    cfg: AgentVMConfig,
    ip: str,
    *,
    script: str,
    summary: str,
    detail: str,
    dry_run: bool,
    check: bool = True,
) -> object | None:
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
        print(
            f'DRYRUN: would run guest reconcile command: {" ".join(shlex.quote(c) for c in cmd)}'
        )
        return None
    result = CommandManager.current().run(
        cmd,
        sudo=False,
        check=check,
        capture=True,
        summary=summary,
        detail=detail,
    )
    if not check:
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code != 0:
            stderr = str(getattr(result, 'stderr', '') or '').strip()
            stdout = str(getattr(result, 'stdout', '') or '').strip()
            raise RuntimeError(stderr or stdout or f'guest command failed code={code}')
    return result


def _install_guest_text_if_changed(
    cfg: AgentVMConfig,
    ip: str,
    *,
    target: str,
    text: str,
    mode: str,
    summary: str,
    detail: str,
    dry_run: bool,
    check: bool = True,
) -> bool:
    target_path = Path(target)
    target_dir = shlex.quote(str(target_path.parent))
    target_q = shlex.quote(str(target_path))
    script = '\n'.join(
        [
            'set -euo pipefail',
            'tmp="$(mktemp)"',
            'cat > "$tmp" <<\'EOF\'',
            text,
            'EOF',
            f'if [ -f {target_q} ] && cmp -s "$tmp" {target_q}; then',
            '  rm -f "$tmp"',
            '  printf "%s\\n" UNCHANGED',
            '  exit 0',
            'fi',
            f'sudo -n mkdir -p {target_dir}',
            f'sudo -n install -m {mode} "$tmp" {target_q}',
            'rm -f "$tmp"',
            'printf "%s\\n" CHANGED',
        ]
    )
    result = _run_guest_root_script(
        cfg,
        ip,
        script=script,
        summary=summary,
        detail=detail,
        dry_run=dry_run,
        check=check,
    )
    if dry_run or result is None:
        return False
    if not check:
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code != 0:
            stderr = str(getattr(result, 'stderr', '') or '').strip()
            stdout = str(getattr(result, 'stdout', '') or '').strip()
            raise RuntimeError(stderr or stdout or f'guest command failed code={code}')
    stdout = str(getattr(result, 'stdout', '') or '').strip().splitlines()
    return bool(stdout and stdout[-1] == 'CHANGED')


def _sync_persistent_attachment_manifest_to_guest(
    cfg: AgentVMConfig,
    ip: str,
    *,
    dry_run: bool,
    check: bool = True,
) -> bool:
    manifest_path = _persistent_host_manifest_path(cfg)
    remote_target = f'{cfg.vm.user}@{ip}:{PERSISTENT_ATTACHMENT_GUEST_STATE_PATH}'
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    ssh_args = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=5,
            batch_mode=True,
        ),
    ]
    mgr = CommandManager.current()
    if dry_run:
        print(
            'DRYRUN: would sync persistent attachment manifest with rsync '
            f'{manifest_path} -> {remote_target}'
        )
        return False
    with mgr.step(
        'Sync persistent attachment manifest into guest',
        why='Push the host canonical manifest into the guest-local replay input using a checksum-based rsync so unchanged content stays untouched.',
        approval_scope=f'persistent-manifest-sync:{cfg.vm.name}',
    ):
        mgr.run(
            [
                *ssh_args,
                f'{cfg.vm.user}@{ip}',
                f'sudo -n mkdir -p {shlex.quote(str(Path(PERSISTENT_ATTACHMENT_GUEST_STATE_PATH).parent))}',
            ],
            sudo=False,
            role='modify',
            check=check,
            capture=True,
            summary='Prepare guest persistent manifest directory',
            detail=f'target={PERSISTENT_ATTACHMENT_GUEST_STATE_PATH}',
        )
        result = mgr.run(
            [
                'rsync',
                '--archive',
                '--checksum',
                '--itemize-changes',
                '--no-owner',
                '--no-group',
                '--chmod=F644',
                '--rsync-path',
                'sudo -n rsync',
                '-e',
                ' '.join(shlex.quote(arg) for arg in ssh_args),
                str(manifest_path),
                remote_target,
            ],
            sudo=False,
            role='modify',
            check=check,
            capture=True,
            summary='Sync persistent attachment manifest to guest',
            detail=f'source={manifest_path} target={remote_target}',
        )
    if not check:
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code != 0:
            stderr = str(getattr(result, 'stderr', '') or '').strip()
            stdout = str(getattr(result, 'stdout', '') or '').strip()
            raise RuntimeError(stderr or stdout or f'rsync failed code={code}')
    return bool((result.stdout or '').strip())


def _sync_persistent_attachment_manifest_on_host(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    dry_run: bool,
) -> Path:
    manifest_path = _persistent_host_manifest_path(cfg)
    manifest_text = _persistent_attachment_manifest_text(cfg, cfg_path)
    if dry_run:
        print(
            f'DRYRUN: would write persistent attachment manifest to {manifest_path}'
        )
        return manifest_path
    _write_text_if_changed(manifest_path, manifest_text)
    return manifest_path


def _ensure_persistent_root_parent_dir(
    cfg: AgentVMConfig,
    *,
    dry_run: bool,
) -> None:
    target = _persistent_root_host_dir(cfg)
    if dry_run:
        print(f'DRYRUN: would create persistent-root parent directory {target}')
        return
    mgr = CommandManager.current()
    with mgr.step(
        'Prepare persistent-root parent directory',
        why='Create the host-side persistent-root export directory used by the persistent attachment virtiofs device.',
        approval_scope=f'persistent-root-parent:{cfg.vm.name}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(target)],
            sudo=True,
            role='modify',
            summary='Create persistent-root parent directory',
            detail=f'target={target}',
        )


def _ensure_persistent_root_vm_mapping(
    cfg: AgentVMConfig,
    *,
    dry_run: bool,
    vm_running: bool | None = None,
) -> None:
    source = str(_persistent_root_host_dir(cfg))
    tag = PERSISTENT_ROOT_VIRTIOFS_TAG
    mappings = vm_share_mappings(cfg, use_sudo=False)
    if any(src == source and t == tag for src, t in mappings):
        return
    mappings = vm_share_mappings(cfg, use_sudo=True)
    if any(src == source and t == tag for src, t in mappings):
        return
    attach_vm_share(
        cfg,
        source,
        tag,
        dry_run=dry_run,
        vm_running=vm_running,
    )


def _ensure_persistent_root_host_bind(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
) -> Path:
    # Reuse the shared-root target-token layout, but stage it under the
    # dedicated persistent-root export tree so the two backends never share the
    # same virtiofs device or host export directory.
    source = Path(attachment.source_dir).resolve()
    target = (
        _persistent_root_host_dir(cfg)
        / Path(_shared_root_host_target(cfg, attachment.tag)).name
    )
    if dry_run:
        print(
            f'DRYRUN: would bind-mount {source} -> {target} for persistent mode'
        )
        return target
    mgr = CommandManager.current()
    with mgr.step(
        'Prepare persistent-root host bind target',
        why='Ensure the persistent-root staged bind exists without tearing down stable host-side state.',
        approval_scope=f'persistent-root-host-bind:{cfg.vm.name}:{attachment.tag}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(_persistent_root_host_dir(cfg))],
            sudo=True,
            role='modify',
            summary='Create persistent-root parent directory',
            detail=f'target={_persistent_root_host_dir(cfg)}',
        )
        mgr.submit(
            ['mkdir', '-p', str(target)],
            sudo=True,
            role='modify',
            summary='Create persistent-root bind target',
            detail=f'target={target}',
        )
        script = (
            'set -euo pipefail; '
            f'src_stat="$(stat -Lc %d:%i {shlex.quote(str(source))} 2>/dev/null || true)"; '
            f'dst_stat="$(stat -Lc %d:%i {shlex.quote(str(target))} 2>/dev/null || true)"; '
            f'if mountpoint -q {shlex.quote(str(target))} && [ -n "$src_stat" ] && [ "$src_stat" = "$dst_stat" ]; then exit 0; fi; '
            f'mount --bind {shlex.quote(str(source))} {shlex.quote(str(target))}'
        )
        mgr.submit(
            ['bash', '-c', script],
            sudo=True,
            role='modify',
            summary='Bind requested host folder into persistent-root target',
            detail=f'source={source} target={target}',
        )
    return target


def _install_persistent_attachment_replay(
    cfg: AgentVMConfig,
    ip: str,
    *,
    dry_run: bool,
    check: bool = True,
) -> bool:
    replay_py = persistent_replay_python().rstrip('\n')
    service_text = persistent_replay_service_unit().rstrip('\n')
    helper_changed = _install_guest_text_if_changed(
        cfg,
        ip,
        target=PERSISTENT_ATTACHMENT_REPLAY_BIN,
        text=replay_py,
        mode='0755',
        summary='Install persistent attachment replay helper',
        detail='Install or refresh the guest systemd replay helper used for boot-time persistent attachment restore.',
        dry_run=dry_run,
        check=check,
    )
    unit_changed = _install_guest_text_if_changed(
        cfg,
        ip,
        target=f'/etc/systemd/system/{PERSISTENT_ATTACHMENT_REPLAY_SERVICE}',
        text=service_text,
        mode='0644',
        summary='Install persistent attachment replay unit',
        detail='Install or refresh the guest systemd unit that launches persistent attachment replay at boot.',
        dry_run=dry_run,
        check=check,
    )
    if dry_run:
        return False
    if unit_changed:
        _run_guest_root_script(
            cfg,
            ip,
            script=(
                'set -euo pipefail; '
                'sudo -n systemctl daemon-reload; '
                f'sudo -n systemctl enable {PERSISTENT_ATTACHMENT_REPLAY_SERVICE}'
            ),
            summary='Refresh persistent attachment replay unit',
            detail='Reload systemd and ensure the persistent attachment replay service stays enabled after the unit file changes.',
            dry_run=dry_run,
            check=check,
        )
    return helper_changed or unit_changed


def _reconcile_persistent_attachments_in_guest(
    cfg: AgentVMConfig,
    cfg_path: Path,
    ip: str,
    *,
    dry_run: bool,
    replay_even_if_unchanged: bool = True,
    continue_on_error: bool = False,
) -> None:
    # Host writes the canonical desired-state manifest first. The guest-local
    # manifest and helper are refreshed next. Explicit reconcile paths set
    # ``replay_even_if_unchanged`` so we still repair live drift even when the
    # sync steps were no-ops. Secondary restore paths can opt into
    # ``continue_on_error`` so a single bad VM does not abort the broader pass.
    def _strict_reconcile() -> None:
        _sync_persistent_attachment_manifest_on_host(
            cfg, cfg_path, dry_run=dry_run
        )
        guest_manifest_changed = _sync_persistent_attachment_manifest_to_guest(
            cfg,
            ip,
            dry_run=dry_run,
            check=not continue_on_error,
        )
        replay_changed = _install_persistent_attachment_replay(
            cfg,
            ip,
            dry_run=dry_run,
            check=not continue_on_error,
        )
        if dry_run:
            return
        if (
            replay_even_if_unchanged
            or guest_manifest_changed
            or replay_changed
        ):
            replay_result = _run_guest_root_script(
                cfg,
                ip,
                script=f'sudo -n {shlex.quote(PERSISTENT_ATTACHMENT_REPLAY_BIN)}',
                summary='Replay persistent attachment mounts inside guest',
                detail='Verify and repair guest-visible persistent attachment bind mounts from the persisted manifest.',
                dry_run=dry_run,
                check=not continue_on_error,
            )
            if continue_on_error and replay_result is not None:
                code = int(
                    getattr(replay_result, 'code', getattr(replay_result, 'returncode', 0))
                )
                if code != 0:
                    stderr = str(getattr(replay_result, 'stderr', '') or '').strip()
                    stdout = str(getattr(replay_result, 'stdout', '') or '').strip()
                    raise RuntimeError(stderr or stdout or f'guest replay failed code={code}')

    if not continue_on_error:
        _strict_reconcile()
        return
    outer_manager = CommandManager.current()
    isolated_manager = CommandManager(
        yes=outer_manager.yes,
        yes_sudo=outer_manager.yes_sudo,
        auto_approve_readonly_sudo=outer_manager.auto_approve_readonly_sudo,
    )
    CommandManager.activate(isolated_manager)
    try:
        _strict_reconcile()
    except Exception as ex:  # pragma: no cover - guest runtime path
        log.warning(
            'persistent-reconcile: VM {} ip={} failed but restore will continue: {}',
            cfg.vm.name,
            ip,
            ex,
        )
    finally:
        CommandManager.activate(outer_manager)


def _prepare_persistent_attachment_host_and_vm(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
    vm_running: bool | None,
) -> None:
    _ensure_persistent_root_parent_dir(cfg, dry_run=dry_run)
    _ensure_persistent_root_host_bind(
        cfg,
        attachment,
        dry_run=dry_run,
    )
    _ensure_persistent_root_vm_mapping(
        cfg,
        dry_run=dry_run,
        vm_running=vm_running,
    )
