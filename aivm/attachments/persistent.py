"""Persistent attachment helpers.

Host state is authoritative. The desired persistent-attachment manifest is
stored on the host outside the virtiofs export tree, then synced one-way into
the guest-local replay input at /var/lib/aivm/attachments.json. The guest
replay helper only reads that local file and reapplies mounts from there.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import tempfile
import textwrap
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from loguru import logger as log

from ..commands import CommandManager
from ..commands import CommandError, CommandResult, CommandRole
from ..config import AgentVMConfig
from ..persistent_replay import (
    PERSISTENT_ATTACHMENT_GUEST_STATE_PATH,
    PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME,
    PERSISTENT_ATTACHMENT_HOST_REPLAY_BIN,
    PERSISTENT_ATTACHMENT_HOST_REPLAY_SERVICE_PREFIX,
    PERSISTENT_ATTACHMENT_REPLAY_BIN,
    PERSISTENT_ATTACHMENT_REPLAY_SERVICE,
    PERSISTENT_ROOT_GUEST_MOUNT_ROOT,
    PERSISTENT_ROOT_VIRTIOFS_TAG,
    persistent_host_replay_python,
    persistent_host_replay_service_unit,
    persistent_replay_python,
    persistent_replay_service_unit,
)
from ..runtime import require_ssh_identity, ssh_base_args
from ..store import (
    find_attachments_for_vm,
    load_store,
    persistent_host_state_dir,
)
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
    return (
        _persistent_host_state_dir(cfg)
        / PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME
    )



def _persistent_host_replay_service_name(vm_name: str) -> str:
    return f'{PERSISTENT_ATTACHMENT_HOST_REPLAY_SERVICE_PREFIX}-{vm_name}.service'



def _install_host_text_if_changed(
    target: Path,
    text: str,
    mode: str,
    *,
    label: str,
    dry_run: bool,
) -> bool:
    new_bytes = text.encode('utf-8')
    if target.exists() and target.read_bytes() == new_bytes:
        return False
    if dry_run:
        print(f'DRYRUN: would install {label} to {target}')
        return True
    with tempfile.NamedTemporaryFile('wb', delete=False) as file:
        file.write(new_bytes)
        tmp_name = file.name
    mgr = CommandManager.current()
    try:
        with mgr.step(
            f'Install {label}',
            why=f'Install updated host-side {label} content for persistent attachment replay.',
            approval_scope=f'{label.replace(" ", "-")}:host:{target}',
        ):
            mgr.submit(
                ['mkdir', '-p', str(target.parent)],
                sudo=True,
                role='modify',
                summary=f'Create parent directory for {label}',
                detail=f'target={target.parent}',
            )
            mgr.submit(
                ['install', '-m', mode, tmp_name, str(target)],
                sudo=True,
                role='modify',
                summary=f'Install {label}',
                detail=f'target={target}',
            )
    finally:
        Path(tmp_name).unlink(missing_ok=True)
    return True



def _install_persistent_host_bind_replay(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    dry_run: bool,
) -> bool:
    del cfg_path
    helper_changed = _install_host_text_if_changed(
        Path(PERSISTENT_ATTACHMENT_HOST_REPLAY_BIN),
        persistent_host_replay_python(),
        '0755',
        label='persistent host replay helper',
        dry_run=dry_run,
    )
    service_name = _persistent_host_replay_service_name(cfg.vm.name)
    unit_changed = _install_host_text_if_changed(
        Path('/etc/systemd/system') / service_name,
        persistent_host_replay_service_unit(
            vm_name=cfg.vm.name,
            manifest_path=str(_persistent_host_manifest_path(cfg)),
            export_root=str(_persistent_root_host_dir(cfg)),
        ),
        '0644',
        label='persistent host replay unit',
        dry_run=dry_run,
    )
    if dry_run:
        return helper_changed or unit_changed
    mgr = CommandManager.current()
    with mgr.step(
        'Enable persistent host replay service',
        why='Ensure the host-side persistent bind replay service is available after reboot.',
        approval_scope=f'persistent-host-replay-service:{cfg.vm.name}',
    ):
        if unit_changed:
            mgr.submit(
                ['systemctl', 'daemon-reload'],
                sudo=True,
                role='modify',
                summary='Reload systemd after persistent host replay unit changes',
                detail=f'service={service_name}',
            )
        mgr.submit(
            ['systemctl', 'enable', service_name],
            sudo=True,
            role='modify',
            summary='Enable persistent host replay service',
            detail=f'service={service_name}',
        )
    return helper_changed or unit_changed



def _reconcile_persistent_host_binds(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    dry_run: bool,
    vm_running: bool | None = None,
) -> None:
    records = _persistent_attachment_records_for_vm(cfg, cfg_path)
    for record in records:
        if not record.enabled:
            continue
        host_src = Path(record.source_dir).expanduser()
        if not host_src.exists():
            log.warning(
                'Skipping persistent host bind replay for VM {} because host path is missing: {}',
                cfg.vm.name,
                host_src,
            )
            continue
        if not host_src.is_dir():
            log.warning(
                'Skipping persistent host bind replay for VM {} because host path is not a directory: {}',
                cfg.vm.name,
                host_src,
            )
            continue
        attachment = ResolvedAttachment(
            vm_name=cfg.vm.name,
            mode=ATTACHMENT_MODE_PERSISTENT,
            access=str(record.access or 'rw'),
            source_dir=str(host_src.resolve()),
            guest_dst=str(record.guest_dst or ''),
            tag=str(record.shared_root_token or ''),
        )
        _prepare_persistent_attachment_host_and_vm(
            cfg,
            attachment,
            dry_run=dry_run,
            vm_running=vm_running,
        )


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
    role: CommandRole | None = None,
    check: bool = True,
) -> CommandResult | None:
    result = _run_guest_ssh_script_with_retry(
        cfg,
        ip,
        script=script,
        summary=summary,
        detail=detail,
        dry_run=dry_run,
        role=role,
        check=check,
        connect_timeout_s=15,
        retries=3,
    )
    if not check:
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code != 0:
            stderr = str(getattr(result, 'stderr', '') or '').strip()
            stdout = str(getattr(result, 'stdout', '') or '').strip()
            raise RuntimeError(
                stderr or stdout or f'guest command failed code={code}'
            )
    return result


def _guest_text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()


def _guest_text_stats(text: str) -> tuple[str, int]:
    payload = text.encode('utf-8')
    return hashlib.sha256(payload).hexdigest(), len(payload)


def _guest_text_hash_check_script(target: str, expected_sha256: str) -> str:
    target_q = shlex.quote(target)
    expected_q = shlex.quote(expected_sha256)
    return textwrap.dedent(
        f"""\
        set -euo pipefail
        if [ ! -f {target_q} ]; then
            printf '%s\\n' MISSING
            exit 0
        fi
        actual="$(sudo -n sha256sum {target_q} | cut -d ' ' -f1)"
        if [ "$actual" = {expected_q} ]; then
            printf '%s\\n' MATCH
        else
            printf '%s\\n' MISMATCH
        fi
        """
    ).strip()


def _guest_text_install_script(target: str, text: str, mode: str) -> str:
    target_dir = shlex.quote(str(Path(target).parent))
    target_q = shlex.quote(target)
    text_q = shlex.quote(text)
    return '\n'.join(
        [
            'set -euo pipefail',
            'tmp="$(mktemp)"',
            f"printf '%s' {text_q} > \"$tmp\"",
            f'sudo -n mkdir -p {target_dir}',
            f'sudo -n install -m {mode} "$tmp" {target_q}',
            'rm -f "$tmp"',
        ]
    )


def _is_transient_ssh_transport_failure(text: str) -> bool:
    lowered = text.lower()
    return any(
        marker in lowered
        for marker in (
            'connection timed out during banner exchange',
            'connection timed out',
            'connection refused',
            'connection reset by peer',
            'connection closed by remote host',
            'broken pipe',
            'kex_exchange_identification',
            'no route to host',
        )
    )


def _run_guest_ssh_script_with_retry(
    cfg: AgentVMConfig,
    ip: str,
    *,
    script: str,
    summary: str,
    detail: str,
    dry_run: bool,
    role: CommandRole | None = None,
    check: bool = True,
    connect_timeout_s: int = 15,
    retries: int = 3,
) -> CommandResult | None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=connect_timeout_s,
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
    mgr = CommandManager.current()
    last_result: object | None = None
    for attempt in range(retries + 1):
        result = mgr.run(
            cmd,
            sudo=False,
            role=role,
            check=False,
            capture=True,
            summary=summary,
            detail=detail,
        )
        last_result = result
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code == 0:
            return result
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        transport_error = '\n'.join(
            part for part in (stderr, stdout, f'code={code}') if part
        )
        if attempt < retries and _is_transient_ssh_transport_failure(
            transport_error
        ):
            log.warning(
                (
                    'Transient SSH failure while {} (attempt {}/{}): {}'
                ),
                summary,
                attempt + 1,
                retries + 1,
                stderr or stdout or f'code={code}',
            )
            time.sleep(min(2 * (attempt + 1), 6))
            continue
        if check:
            raise CommandError(cmd, CommandResult(code, stdout, stderr))
        return result
    return last_result


def _run_rsync_with_retry(
    cmd: list[str],
    *,
    summary: str,
    detail: str,
    dry_run: bool,
    check: bool = True,
    retries: int = 3,
) -> CommandResult | None:
    if dry_run:
        print(
            f'DRYRUN: would run rsync command: {" ".join(shlex.quote(c) for c in cmd)}'
        )
        return None
    mgr = CommandManager.current()
    last_result: object | None = None
    for attempt in range(retries + 1):
        result = mgr.run(
            cmd,
            sudo=False,
            role='modify',
            check=False,
            capture=True,
            summary=summary,
            detail=detail,
        )
        last_result = result
        code = int(getattr(result, 'code', getattr(result, 'returncode', 0)))
        if code == 0:
            return result
        stderr = str(getattr(result, 'stderr', '') or '').strip()
        stdout = str(getattr(result, 'stdout', '') or '').strip()
        transport_error = '\n'.join(
            part for part in (stderr, stdout, f'code={code}') if part
        )
        if attempt < retries and _is_transient_ssh_transport_failure(
            transport_error
        ):
            log.warning(
                (
                    'Transient rsync failure while {} (attempt {}/{}): {}'
                ),
                summary,
                attempt + 1,
                retries + 1,
                stderr or stdout or f'code={code}',
            )
            time.sleep(min(2 * (attempt + 1), 6))
            continue
        if check:
            raise CommandError(cmd, CommandResult(code, stdout, stderr))
        return result
    return last_result


def _diagnose_guest_text_mismatch(
    cfg: AgentVMConfig,
    ip: str,
    *,
    target: str,
    text: str,
    label: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    expected_sha256, expected_len = _guest_text_stats(text)
    expected_bytes = text.encode('utf-8')
    target_q = shlex.quote(target)
    stats = _run_guest_root_script(
        cfg,
        ip,
        script=(
            'set -euo pipefail; '
            f'if [ ! -f {target_q} ]; then printf "%s\\n" MISSING; exit 0; fi; '
            f'printf "%s\\n" "$(sudo -n sha256sum {target_q} | cut -d " " -f1)"; '
            f'printf "%s\\n" "$(sudo -n wc -c < {target_q})"'
        ),
        summary=f'Inspect {label} hash mismatch details',
        detail=f'target={target}',
        dry_run=dry_run,
        role='read',
        check=False,
    )
    actual_sha256 = ''
    actual_len = -1
    if stats is not None:
        lines = [line.strip() for line in str(getattr(stats, 'stdout', '') or '').splitlines()]
        if lines:
            actual_sha256 = lines[0]
        if len(lines) > 1:
            try:
                actual_len = int(lines[1])
            except ValueError:
                actual_len = -1
    content = _run_guest_root_script(
        cfg,
        ip,
        script=f'sudo -n cat {target_q}',
        summary=f'Fetch {label} content for verification',
        detail=f'target={target}',
        dry_run=dry_run,
        role='read',
        check=False,
    )
    assert content is not None
    actual_bytes = (content.stdout or '').encode('utf-8')
    actual_sha_calc = hashlib.sha256(actual_bytes).hexdigest()
    actual_len_calc = len(actual_bytes)
    host_file = None
    with tempfile.NamedTemporaryFile('wb', delete=False) as file:
        file.write(expected_bytes)
        host_file = Path(file.name)
    try:
        host_bytes = host_file.read_bytes()
        same_bytes = host_bytes == actual_bytes
    finally:
        host_file.unlink(missing_ok=True)
    log.warning(
        (
            '{} mismatch after install: expected_sha256={} actual_sha256={} '
            'expected_bytes={} actual_bytes={} byte_for_byte_match={}'
        ),
        label,
        expected_sha256,
        actual_sha256 or actual_sha_calc,
        expected_len,
        actual_len if actual_len >= 0 else actual_len_calc,
        same_bytes,
    )


def _install_guest_text_if_changed(
    cfg: AgentVMConfig,
    ip: str,
    *,
    target: str,
    text: str,
    mode: str,
    label: str,
    dry_run: bool,
    check: bool = True,
) -> bool:
    label = str(label).strip() or 'guest text'
    label_title = label[0].upper() + label[1:]
    expected_sha256 = _guest_text_sha256(text)
    check_script = _guest_text_hash_check_script(target, expected_sha256)
    mgr = CommandManager.current()
    with mgr.step(
        f'Check {label} hash',
        why=(
            'Compare the host-rendered helper content against the guest copy '
            'using a checksum so unchanged files stay untouched.'
        ),
        approval_scope=f'{label.replace(" ", "-")}:check:{cfg.vm.name}:{target}',
    ):
        check_result = _run_guest_root_script(
            cfg,
            ip,
            script=check_script,
            summary=f'Check {label} hash',
            detail=f'target={target} expected_sha256={expected_sha256}',
            dry_run=dry_run,
            role='read',
            check=check,
        )
    if dry_run or check_result is None:
        return False
    status = str(getattr(check_result, 'stdout', '') or '').strip().splitlines()
    status = status[-1].strip().upper() if status else ''
    if status not in {'MATCH', 'MISSING', 'MISMATCH'}:
        raise RuntimeError(
            f'Unexpected guest file hash check result for {target}: {status or "<empty>"}'
        )
    log.info('{} hash check result: {}', label_title, status)
    if status == 'MATCH':
        return False
    with mgr.step(
        f'{label_title} differs, installing updated content',
        why=(
            'The guest file hash did not match the host-rendered content, so '
            'the updated file must be written explicitly.'
        ),
        approval_scope=f'{label.replace(" ", "-")}:{cfg.vm.name}:{target}',
    ):
        write_script = _guest_text_install_script(target, text, mode)
        _run_guest_root_script(
            cfg,
            ip,
            script=write_script,
            summary=f'Install {label}',
            detail=f'target={target} expected_sha256={expected_sha256}',
            dry_run=dry_run,
            role='modify',
            check=check,
        )
        verify_result = _run_guest_root_script(
            cfg,
            ip,
            script=check_script,
            summary=f'Verify {label} hash after install',
            detail=f'target={target} expected_sha256={expected_sha256}',
            dry_run=dry_run,
            role='read',
            check=False,
        )
        if verify_result is not None:
            verify_status = (
                str(getattr(verify_result, 'stdout', '') or '')
                .strip()
                .splitlines()
            )
            verify_status = verify_status[-1].strip().upper() if verify_status else ''
            if verify_status != 'MATCH':
                _diagnose_guest_text_mismatch(
                    cfg,
                    ip,
                    target=target,
                    text=text,
                    label=label_title,
                    dry_run=dry_run,
                )
                raise RuntimeError(
                    f'{label_title} still mismatched after install: '
                    f'target={target} status={verify_status or "<empty>"} '
                    f'expected_sha256={expected_sha256}'
                )
    return True


def _sync_persistent_attachment_manifest_to_guest(
    cfg: AgentVMConfig,
    ip: str,
    *,
    dry_run: bool,
    check: bool = True,
) -> bool:
    manifest_path = _persistent_host_manifest_path(cfg)
    remote_target = (
        f'{cfg.vm.user}@{ip}:{PERSISTENT_ATTACHMENT_GUEST_STATE_PATH}'
    )
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    ssh_args = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=15,
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
        _run_guest_ssh_script_with_retry(
            cfg,
            ip,
            script=(
                f'sudo -n mkdir -p {shlex.quote(str(Path(PERSISTENT_ATTACHMENT_GUEST_STATE_PATH).parent))}'
            ),
            summary='Prepare guest persistent manifest directory',
            detail=f'target={PERSISTENT_ATTACHMENT_GUEST_STATE_PATH}',
            dry_run=dry_run,
            role='modify',
            check=check,
        )
        result = _run_rsync_with_retry(
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
            summary='Sync persistent attachment manifest to guest',
            detail=f'source={manifest_path} target={remote_target}',
            dry_run=dry_run,
            check=check,
        )
    assert result is not None
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
    replay_py = persistent_replay_python()
    service_text = persistent_replay_service_unit()
    helper_changed = _install_guest_text_if_changed(
        cfg,
        ip,
        target=PERSISTENT_ATTACHMENT_REPLAY_BIN,
        text=replay_py,
        mode='0755',
        label='guest replay helper',
        dry_run=dry_run,
        check=check,
    )
    unit_changed = _install_guest_text_if_changed(
        cfg,
        ip,
        target=f'/etc/systemd/system/{PERSISTENT_ATTACHMENT_REPLAY_SERVICE}',
        text=service_text,
        mode='0644',
        label='guest replay unit',
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
        _reconcile_persistent_host_binds(
            cfg,
            cfg_path,
            dry_run=dry_run,
            vm_running=True,
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
        if replay_even_if_unchanged or guest_manifest_changed or replay_changed:
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
                    getattr(
                        replay_result,
                        'code',
                        getattr(replay_result, 'returncode', 0),
                    )
                )
                if code != 0:
                    stderr = str(
                        getattr(replay_result, 'stderr', '') or ''
                    ).strip()
                    stdout = str(
                        getattr(replay_result, 'stdout', '') or ''
                    ).strip()
                    raise RuntimeError(
                        stderr or stdout or f'guest replay failed code={code}'
                    )

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
