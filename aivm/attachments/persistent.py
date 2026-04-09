"""Declarative shared-root attachment helpers.

This mode keeps the host-side shared-root bind staging stable while persisting
the desired guest-visible bind mounts as declarations that can be replayed at
boot or during lightweight reconcile.
"""

from __future__ import annotations

import json
import shlex
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..persistent_replay import (
    DECLARED_ATTACHMENT_GUEST_STATE_PATH,
    DECLARED_ATTACHMENT_HOST_MANIFEST_NAME,
    DECLARED_ATTACHMENT_HOST_META_DIR,
    DECLARED_ATTACHMENT_REPLAY_BIN,
    DECLARED_ATTACHMENT_REPLAY_SERVICE,
    DECLARED_ROOT_GUEST_MOUNT_ROOT,
    DECLARED_ROOT_VIRTIOFS_TAG,
    declared_replay_python,
    declared_replay_service_unit,
)
from ..runtime import require_ssh_identity, ssh_base_args
from ..store import find_attachments_for_vm, load_store
from ..vm import attach_vm_share, vm_share_mappings
from ..vm.share import ResolvedAttachment
from .resolve import ATTACHMENT_MODE_DECLARED
from .shared_root import _shared_root_host_target


def _declared_root_host_dir(cfg: AgentVMConfig) -> Path:
    return Path(cfg.paths.base_dir) / cfg.vm.name / 'declared-root'


@dataclass(frozen=True)
class DeclaredAttachmentRecord:
    attachment_id: str
    mode: str
    source_dir: str
    host_lexical_path: str
    shared_root_token: str
    guest_dst: str
    access: str
    enabled: bool = True


def _declared_host_meta_dir(cfg: AgentVMConfig) -> Path:
    return _declared_root_host_dir(cfg) / DECLARED_ATTACHMENT_HOST_META_DIR


def _declared_host_manifest_path(cfg: AgentVMConfig) -> Path:
    return _declared_host_meta_dir(cfg) / DECLARED_ATTACHMENT_HOST_MANIFEST_NAME


def _declared_attachment_records_for_vm(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> list[DeclaredAttachmentRecord]:
    reg = load_store(cfg_path)
    records: list[DeclaredAttachmentRecord] = []
    for att in find_attachments_for_vm(reg, cfg.vm.name):
        if str(att.mode or '').strip() != ATTACHMENT_MODE_DECLARED:
            continue
        records.append(
            DeclaredAttachmentRecord(
                attachment_id=str(att.tag or att.host_path),
                mode=str(att.mode or ATTACHMENT_MODE_DECLARED),
                source_dir=str(att.host_path),
                host_lexical_path=str(att.host_lexical_path or ''),
                shared_root_token=str(att.tag or ''),
                guest_dst=str(att.guest_dst or ''),
                access=str(att.access or 'rw'),
                enabled=True,
            )
        )
    return sorted(records, key=lambda rec: (rec.guest_dst, rec.shared_root_token))


def _declared_attachment_manifest_text(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> str:
    records = _declared_attachment_records_for_vm(cfg, cfg_path)
    payload = {
        'schema_version': 1,
        'vm_name': cfg.vm.name,
        'shared_root_mount': DECLARED_ROOT_GUEST_MOUNT_ROOT,
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
) -> None:
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
        print(f'DRYRUN: would run guest reconcile command: {" ".join(shlex.quote(c) for c in cmd)}')
        return
    CommandManager.current().run(
        cmd,
        sudo=False,
        check=True,
        capture=True,
        summary=summary,
        detail=detail,
    )


def _sync_declared_attachment_manifest_on_host(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    dry_run: bool,
) -> Path:
    manifest_path = _declared_host_manifest_path(cfg)
    manifest_text = _declared_attachment_manifest_text(cfg, cfg_path)
    if dry_run:
        print(f'DRYRUN: would write persistent attachment manifest to {manifest_path}')
        return manifest_path
    mgr = CommandManager.current()
    meta_dir = _declared_host_meta_dir(cfg)
    manifest_q = shlex.quote(str(manifest_path))
    payload = shlex.quote(manifest_text)
    with mgr.step(
        'Sync persistent attachment manifest',
        why='Update the host-side persistent attachment manifest that the guest boot-time replay helper consumes.',
        approval_scope=f'declared-manifest:{cfg.vm.name}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(meta_dir)],
            sudo=True,
            role='modify',
            summary='Create persistent attachment metadata directory',
            detail=f'target={meta_dir}',
        )
        mgr.submit(
            ['bash', '-c', f'printf %s {payload} > {manifest_q}'],
            sudo=True,
            role='modify',
            summary='Write persistent attachment manifest',
            detail=f'target={manifest_path}',
        )
    return manifest_path


def _ensure_declared_root_parent_dir(
    cfg: AgentVMConfig,
    *,
    dry_run: bool,
) -> None:
    target = _declared_root_host_dir(cfg)
    if dry_run:
        print(f'DRYRUN: would create declared-root parent directory {target}')
        return
    mgr = CommandManager.current()
    with mgr.step(
        'Prepare declared-root parent directory',
        why='Create the host-side declared-root export directory used by the persistent attachment virtiofs device.',
        approval_scope=f'declared-root-parent:{cfg.vm.name}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(target)],
            sudo=True,
            role='modify',
            summary='Create declared-root parent directory',
            detail=f'target={target}',
        )


def _ensure_declared_root_vm_mapping(
    cfg: AgentVMConfig,
    *,
    dry_run: bool,
    vm_running: bool | None = None,
) -> None:
    source = str(_declared_root_host_dir(cfg))
    tag = DECLARED_ROOT_VIRTIOFS_TAG
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


def _ensure_declared_root_host_bind(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
) -> Path:
    # Reuse the shared-root target-token layout, but stage it under the
    # dedicated declared-root export tree so the two backends never share the
    # same virtiofs device or host export directory.
    source = Path(attachment.source_dir).resolve()
    target = _declared_root_host_dir(cfg) / Path(
        _shared_root_host_target(cfg, attachment.tag)
    ).name
    if dry_run:
        print(f'DRYRUN: would bind-mount {source} -> {target} for persistent mode')
        return target
    mgr = CommandManager.current()
    with mgr.step(
        'Prepare declared-root host bind target',
        why='Ensure the declared-root staged bind exists without tearing down stable host-side state.',
        approval_scope=f'declared-root-host-bind:{cfg.vm.name}:{attachment.tag}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(_declared_root_host_dir(cfg))],
            sudo=True,
            role='modify',
            summary='Create declared-root parent directory',
            detail=f'target={_declared_root_host_dir(cfg)}',
        )
        mgr.submit(
            ['mkdir', '-p', str(target)],
            sudo=True,
            role='modify',
            summary='Create declared-root bind target',
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
            summary='Bind requested host folder into declared-root target',
            detail=f'source={source} target={target}',
        )
    return target


def _install_declared_attachment_replay(
    cfg: AgentVMConfig,
    ip: str,
    *,
    dry_run: bool,
) -> None:
    replay_py = declared_replay_python()
    service_text = declared_replay_service_unit()
    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        sudo -n mkdir -p {shlex.quote(str(Path(DECLARED_ATTACHMENT_REPLAY_BIN).parent))}
        sudo -n mkdir -p /etc/systemd/system
        tmp_py="$(mktemp)"
        tmp_service="$(mktemp)"
        cat > "$tmp_py" <<'PYEOF'
        {replay_py}
        PYEOF
        cat > "$tmp_service" <<'SVCEOF'
        {service_text}
        SVCEOF
        sudo -n install -m 0755 "$tmp_py" {shlex.quote(DECLARED_ATTACHMENT_REPLAY_BIN)}
        sudo -n install -m 0644 "$tmp_service" /etc/systemd/system/{DECLARED_ATTACHMENT_REPLAY_SERVICE}
        rm -f "$tmp_py" "$tmp_service"
        sudo -n systemctl daemon-reload
        sudo -n systemctl enable {DECLARED_ATTACHMENT_REPLAY_SERVICE}
        """
    )
    _run_guest_root_script(
        cfg,
        ip,
        script=script,
        summary='Install persistent attachment replay helper',
        detail='Install or refresh the guest systemd replay helper used for boot-time persistent attachment restore.',
        dry_run=dry_run,
    )


def _reconcile_declared_attachments_in_guest(
    cfg: AgentVMConfig,
    cfg_path: Path,
    ip: str,
    *,
    dry_run: bool,
) -> None:
    _sync_declared_attachment_manifest_on_host(cfg, cfg_path, dry_run=dry_run)
    _install_declared_attachment_replay(cfg, ip, dry_run=dry_run)
    _run_guest_root_script(
        cfg,
        ip,
        script=f'sudo -n {shlex.quote(DECLARED_ATTACHMENT_REPLAY_BIN)}',
        summary='Replay persistent attachment mounts inside guest',
        detail='Verify and repair guest-visible persistent attachment bind mounts from the persisted manifest.',
        dry_run=dry_run,
    )


def _prepare_declared_attachment_host_and_vm(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
    vm_running: bool | None,
) -> None:
    _ensure_declared_root_parent_dir(cfg, dry_run=dry_run)
    _ensure_declared_root_host_bind(
        cfg,
        attachment,
        dry_run=dry_run,
    )
    _ensure_declared_root_vm_mapping(
        cfg,
        dry_run=dry_run,
        vm_running=vm_running,
    )
