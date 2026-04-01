"""Session/reconcile helpers: attachment saving, VM reconciliation, and session preparation."""

from __future__ import annotations

import re
import sys
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from loguru import logger

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..util import CmdError
from ..firewall import apply_firewall
from ..net import ensure_network
from ..runtime import require_ssh_identity, ssh_base_args
from ..status import (
    probe_firewall,
    probe_network,
    probe_ssh_ready,
    probe_vm_state,
)
from ..store import (
    find_attachments_for_vm,
    load_store,
    save_store,
    upsert_attachment,
    upsert_network,
    upsert_vm_with_network,
)
from ..vm import (
    attach_vm_share,
    create_or_start_vm,
    ensure_share_mounted,
    get_ip_cached,
    vm_has_virtiofs_shared_memory,
    vm_share_mappings,
    wait_for_ip,
    wait_for_ssh,
)
from ..vm.drift import (
    attachment_has_mapping as drift_attachment_has_mapping,
    hardware_drift_report,
)
from ..vm.share import (
    align_attachment_tag_with_mappings as drift_align_attachment_tag_with_mappings,
)
from ..vm.share import ResolvedAttachment, SHARED_ROOT_VIRTIOFS_TAG
from ..cli._common import PreparedSession, _cfg_path, _maybe_install_missing_host_deps, _maybe_offer_create_ssh_identity, _record_vm, _resolve_cfg_for_code
from .resolve import (
    ATTACHMENT_ACCESS_RO,
    ATTACHMENT_MODE_GIT,
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    _normalize_attachment_mode,
    _resolve_attachment,
)
from .guest import (
    _apply_guest_derived_symlinks,
    _ensure_attachment_available_in_guest,
    _ensure_git_clone_attachment,
)
from .shared_root import (
    _ensure_shared_root_host_bind,
    _ensure_shared_root_parent_dir,
    _ensure_shared_root_vm_mapping,
    _shared_root_host_dir,
)

log = logger


@dataclass(frozen=True)
class ReconcilePolicy:
    ensure_firewall_opt: bool
    recreate_if_needed: bool
    dry_run: bool
    yes: bool


@dataclass(frozen=True)
class ReconcileResult:
    attachment: ResolvedAttachment
    cached_ip: str | None
    cached_ssh_ok: bool
    shared_root_host_side_ready: bool = False


def _missing_virtiofs_dir_from_error(ex: Exception) -> str | None:
    text = str(ex)
    if isinstance(ex, CmdError):
        text = f'{ex.result.stderr}\n{ex.result.stdout}\n{text}'
    m = re.search(r"virtiofs export directory '([^']+)' does not exist", text)
    return m.group(1) if m else None


def _maybe_warn_hardware_drift(cfg: AgentVMConfig) -> None:
    """Warn about hardware drift using the shared drift report."""
    report = hardware_drift_report(cfg, use_sudo=True)
    if not report.available or report.ok is True:
        return

    print(
        f'⚠️ VM {cfg.vm.name} is already defined and differs from config for hardware settings.'
    )
    for item in report.items:
        if item.key == 'cpus':
            print(f'  - cpus: current={item.actual} desired={item.expected}')
        elif item.key == 'ram_mb':
            print(f'  - ram_mb: current={item.actual} desired={item.expected}')
    print('Suggested non-destructive apply commands:')
    print(f'  sudo virsh shutdown {cfg.vm.name}   # if VM is running')
    for item in report.items:
        if item.key == 'cpus':
            print(
                f'  sudo virsh setvcpus {cfg.vm.name} {item.expected} --config'
            )
        elif item.key == 'ram_mb':
            kib = int(str(item.expected)) * 1024
            print(f'  sudo virsh setmaxmem {cfg.vm.name} {kib} --config')
            print(f'  sudo virsh setmem {cfg.vm.name} {kib} --config')
    print(
        'These updates preserve VM disk/state. Recreate is only needed for definition-level changes that cannot be edited in place.'
    )


def _resolve_ip_for_ssh_ops(
    cfg: AgentVMConfig, *, yes: bool, purpose: str
) -> str:
    ip = get_ip_cached(cfg)
    if ip:
        ssh_ok = bool(probe_ssh_ready(cfg, ip).ok)
        if ssh_ok:
            return ip
    mgr = CommandManager.current()
    with mgr.intent(
        f'Resolve IP for VM {cfg.vm.name}',
        why=purpose,
        role='read',
    ):
        ip = wait_for_ip(cfg, timeout_s=360, dry_run=False)
        wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
        return ip


def _record_attachment(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    host_src: Path,
    mode: str,
    access: str,
    guest_dst: str,
    tag: str,
    force: bool = False,
) -> Path:
    # Persist the lexical (unresolved) host path when it differs from the
    # resolved path so that restore can recreate companion guest symlinks.
    lexical_str = str(host_src.expanduser().absolute())
    resolved_str = str(host_src.resolve())
    host_lexical_path = lexical_str if lexical_str != resolved_str else ''

    reg = load_store(cfg_path)
    before = deepcopy(reg)
    upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
    upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
    upsert_attachment(
        reg,
        host_path=host_src,
        vm_name=cfg.vm.name,
        mode=mode,
        access=access,
        guest_dst=guest_dst,
        tag=tag,
        host_lexical_path=host_lexical_path,
        force=force,
    )
    if reg == before:
        log.debug(
            'Attachment record already up to date for vm={} host_src={} in {}',
            cfg.vm.name,
            host_src,
            cfg_path,
        )
        return cfg_path
    return save_store(
        reg,
        cfg_path,
        reason=(
            f'Persist attachment record for {host_src} on VM {cfg.vm.name} '
            f'(mode={mode}, access={access}, guest_dst={guest_dst}).'
        ),
    )


def _saved_vm_attachments(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    primary_attachment: ResolvedAttachment | None = None,
) -> list[ResolvedAttachment]:
    """Return persisted share-like attachments that should be present for this VM.

    ``aivm code`` and related entry points restore the current working folder
    first and then best-effort restore any other saved shared or shared-root
    attachments for the same VM. This helper assembles that restore set while
    de-duplicating by resolved source path and skipping missing or non-directory
    host paths.
    """
    reg = load_store(cfg_path)
    attachments: list[ResolvedAttachment] = []
    seen_sources: set[str] = set()
    if primary_attachment is not None:
        primary_source = str(Path(primary_attachment.source_dir).resolve())
        attachments.append(primary_attachment)
        seen_sources.add(primary_source)

    # Restore any other folders already associated with this VM so a rebooted
    # guest comes back with the broader working set the user previously chose.
    for att in find_attachments_for_vm(reg, cfg.vm.name):
        mode = _normalize_attachment_mode(att.mode)
        if mode not in {
            ATTACHMENT_MODE_SHARED,
            ATTACHMENT_MODE_SHARED_ROOT,
        }:
            continue
        host_src = Path(att.host_path).expanduser()
        try:
            source_dir = str(host_src.resolve())
        except Exception:
            source_dir = str(host_src.absolute())
        if source_dir in seen_sources:
            continue
        if not host_src.exists():
            log.warning(
                'Skipping saved attachment for VM {} because host path is missing: {}',
                cfg.vm.name,
                host_src,
            )
            continue
        if not host_src.is_dir():
            log.warning(
                'Skipping saved attachment for VM {} because host path is not a directory: {}',
                cfg.vm.name,
                host_src,
            )
            continue
        attachments.append(_resolve_attachment(cfg, cfg_path, host_src, ''))
        seen_sources.add(source_dir)
    return attachments


def _restore_saved_vm_attachments(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    ip: str,
    primary_attachment: ResolvedAttachment | None,
    yes: bool,
    mirror_home: bool = False,
) -> None:
    """Best-effort restore saved non-primary attachments for a running VM session.

    After the primary folder for the current command has been reconciled, this
    helper walks the other persisted attachments for the VM and attempts to make
    them available inside the guest again. Standard shared attachments are
    restored by ensuring the virtiofs mapping exists and then mounting it in the
    guest. Shared-root attachments reuse the shared-root reconciliation path but
    disable disruptive host-side rebinds, because automatic restore should not
    tear down an unexpected active mount.

    This is the call site that previously logged warnings like
    "Refusing to replace existing shared-root host bind mount during automatic
    restore" and then left guest paths empty after a reboot. The relaxed
    SOURCE-matching logic in ``_ensure_shared_root_host_bind`` allows valid
    existing host binds to pass through here so guest-side rebind repair can
    continue.
    """
    saved_attachments = _saved_vm_attachments(
        cfg,
        cfg_path,
        primary_attachment=primary_attachment,
    )
    if len(saved_attachments) <= 1:
        return

    secondary_attachments = saved_attachments[1:]

    # Build a map from resolved source_dir -> lexical host path so restore can
    # recreate companion guest symlinks when the original attachment was made
    # through a host symlink.
    _restore_reg = load_store(cfg_path)
    _lexical_by_source: dict[str, str] = {
        e.host_path: e.host_lexical_path
        for e in find_attachments_for_vm(_restore_reg, cfg.vm.name)
        if e.host_lexical_path
    }
    shared_secondary = [
        att
        for att in secondary_attachments
        if att.mode == ATTACHMENT_MODE_SHARED
    ]
    mappings: list[tuple[str, str]] = []
    if shared_secondary:
        mappings = vm_share_mappings(cfg, use_sudo=False)
        needs_privileged_probe = False
        for att in shared_secondary:
            aligned = drift_align_attachment_tag_with_mappings(
                att, Path(att.source_dir), mappings
            )
            if not drift_attachment_has_mapping(cfg, aligned, mappings):
                needs_privileged_probe = True
                break

        if needs_privileged_probe:
            mappings = vm_share_mappings(cfg, use_sudo=True)

    restored = 0
    for att in secondary_attachments:
        if att.mode == ATTACHMENT_MODE_SHARED_ROOT:
            aligned = att
            _lx = _lexical_by_source.get(aligned.source_dir)
            _restore_src = Path(_lx) if _lx else Path(aligned.source_dir)
            try:
                _ensure_attachment_available_in_guest(
                    cfg,
                    _restore_src,
                    aligned,
                    ip,
                    yes=bool(yes),
                    dry_run=False,
                    ensure_shared_root_host_side=True,
                    allow_disruptive_shared_root_rebind=False,
                    mirror_home=mirror_home,
                )
                _record_attachment(
                    cfg,
                    cfg_path,
                    host_src=_restore_src,
                    mode=aligned.mode,
                    access=aligned.access,
                    guest_dst=aligned.guest_dst,
                    tag=aligned.tag,
                )
                restored += 1
            except Exception as ex:
                if (
                    isinstance(ex, RuntimeError)
                    and 'Refusing to replace existing shared-root host bind mount during automatic restore'
                    in str(ex)
                ):
                    log.warning(
                        'Skipping saved shared-root attachment restore for VM {} to avoid disrupting an active mount: source={} guest_dst={} token={} detail={}',
                        cfg.vm.name,
                        aligned.source_dir,
                        aligned.guest_dst,
                        aligned.tag,
                        ex,
                    )
                    continue
                log.warning(
                    'Could not restore shared-root attachment for VM {}: source={} guest_dst={} token={} err={}',
                    cfg.vm.name,
                    aligned.source_dir,
                    aligned.guest_dst,
                    aligned.tag,
                    ex,
                )
            continue

        _lx = _lexical_by_source.get(att.source_dir)
        _restore_src = Path(_lx) if _lx else Path(att.source_dir)
        aligned = drift_align_attachment_tag_with_mappings(
            att, Path(att.source_dir), mappings
        )
        if not drift_attachment_has_mapping(cfg, aligned, mappings):
            try:
                attach_vm_share(
                    cfg,
                    aligned.source_dir,
                    aligned.tag,
                    dry_run=False,
                )
            except Exception as ex:
                log.warning(
                    'Could not restore saved attachment for VM {}: source={} guest_dst={} tag={} err={}',
                    cfg.vm.name,
                    aligned.source_dir,
                    aligned.guest_dst,
                    aligned.tag,
                    ex,
                )
                continue
            mappings = vm_share_mappings(cfg, use_sudo=True)
            aligned = drift_align_attachment_tag_with_mappings(
                aligned, Path(aligned.source_dir), mappings
            )
            if not drift_attachment_has_mapping(cfg, aligned, mappings):
                log.warning(
                    'Saved attachment still missing after restore attempt for VM {}: source={} guest_dst={} tag={}',
                    cfg.vm.name,
                    aligned.source_dir,
                    aligned.guest_dst,
                    aligned.tag,
                )
                continue

        try:
            ensure_share_mounted(
                cfg,
                ip,
                guest_dst=aligned.guest_dst,
                tag=aligned.tag,
                read_only=(aligned.access == ATTACHMENT_ACCESS_RO),
                dry_run=False,
            )
            _apply_guest_derived_symlinks(
                cfg,
                ip,
                _restore_src,
                aligned,
                mirror_home=mirror_home,
            )
            _record_attachment(
                cfg,
                cfg_path,
                host_src=_restore_src,
                mode=aligned.mode,
                access=aligned.access,
                guest_dst=aligned.guest_dst,
                tag=aligned.tag,
            )
            restored += 1
        except Exception as ex:
            log.warning(
                'Could not remount saved attachment inside guest for VM {}: source={} guest_dst={} tag={} err={}',
                cfg.vm.name,
                aligned.source_dir,
                aligned.guest_dst,
                aligned.tag,
                ex,
            )
    if restored:
        log.info(
            'Restored {} saved attachment(s) for VM {}',
            restored,
            cfg.vm.name,
        )


def _virtiofs_mapping_for_attachment(
    cfg: AgentVMConfig, attachment: ResolvedAttachment
) -> tuple[str, str] | None:
    if attachment.mode == ATTACHMENT_MODE_SHARED:
        return attachment.source_dir, attachment.tag
    if attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
        return str(_shared_root_host_dir(cfg)), SHARED_ROOT_VIRTIOFS_TAG
    return None


def _probe_vm_running_nonsudo(vm_name: str) -> bool | None:
    """Probe whether a VM is running without requiring sudo.

    Returns:
        True if the VM is running, False if not defined/running,
        None if the probe is inconclusive (e.g., permission denied).
    """
    from ..runtime import virsh_system_cmd

    res = CommandManager.current().run(
        virsh_system_cmd('domstate', vm_name),
        sudo=False,
        check=False,
        capture=True,
    )
    if res.code != 0:
        raw_detail = (res.stderr or res.stdout or '').strip().lower()
        if (
            'permission denied' in raw_detail
            or 'authentication failed' in raw_detail
        ):
            return None
        return False
    state = res.stdout.strip().lower()
    return 'running' in state


def _reconcile_attached_vm(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    *,
    policy: ReconcilePolicy,
) -> ReconcileResult:
    """Reconcile VM/network/firewall/share state before code/ssh-style sessions.

    This function is the orchestration pivot for "one-command" UX. It tries to
    preserve an existing running VM when safe, and only escalates to recreate or
    privileged host changes when required for correctness.
    """
    mgr = CommandManager.current()
    with mgr.intent(
        f'Prepare attached session for VM {cfg.vm.name}',
        why=(
            'Reconcile network, firewall, VM power state, and host-share '
            'availability before opening an attached SSH or editor session.'
        ),
        role='modify',
    ):
        cached_ip = get_ip_cached(cfg) if not policy.dry_run else None
        cached_ssh_ok = False
        if cached_ip:
            cached_ssh_ok = bool(probe_ssh_ready(cfg, cached_ip).ok)
        vm_running_probe = (
            _probe_vm_running_nonsudo(cfg.vm.name)
            if not policy.dry_run
            else None
        )

        net_probe = probe_network(cfg, use_sudo=False).ok
        need_network_ensure = (net_probe is False) and (not cached_ssh_ok)
        if need_network_ensure:
            ensure_network(cfg, recreate=False, dry_run=policy.dry_run)

        need_firewall_apply = False
        if (
            cfg.firewall.enabled
            and policy.ensure_firewall_opt
            and (not cached_ssh_ok)
        ):
            fw_probe = probe_firewall(cfg, use_sudo=False).ok
            if fw_probe is None:
                fw_probe = probe_firewall(cfg, use_sudo=True).ok
            need_firewall_apply = fw_probe is not True
        if need_firewall_apply:
            apply_firewall(cfg, dry_run=policy.dry_run)

        recreate = False
        vm_running = vm_running_probe
        mappings: list[tuple[str, str]] = []
        has_share = False
        shared_root_host_side_ready = False
        virtiofs_mapping = _virtiofs_mapping_for_attachment(cfg, attachment)
        if vm_running is None and cached_ssh_ok:
            vm_running = True
        if (
            virtiofs_mapping is not None
            and not policy.dry_run
            and vm_running is True
        ):
            mappings = vm_share_mappings(cfg, use_sudo=False)
            if attachment.mode == ATTACHMENT_MODE_SHARED:
                attachment = drift_align_attachment_tag_with_mappings(
                    attachment, host_src, mappings
                )
                virtiofs_mapping = _virtiofs_mapping_for_attachment(
                    cfg, attachment
                )
            if virtiofs_mapping is not None:
                req_src, req_tag = virtiofs_mapping
                has_share = any(
                    src == req_src and tag == req_tag for src, tag in mappings
                )

        need_vm_start_or_create = policy.dry_run or (vm_running is not True)
        if need_vm_start_or_create:
            _maybe_install_missing_host_deps(
                yes=bool(policy.yes), dry_run=bool(policy.dry_run)
            )
            if attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
                if not policy.dry_run:
                    _ensure_shared_root_parent_dir(cfg, dry_run=False)
            try:
                create_or_start_vm(
                    cfg,
                    dry_run=policy.dry_run,
                    recreate=False,
                    share_source_dir=(
                        virtiofs_mapping[0] if virtiofs_mapping else ''
                    ),
                    share_tag=(virtiofs_mapping[1] if virtiofs_mapping else ''),
                )
            except Exception as ex:
                missing_virtiofs_dir = _missing_virtiofs_dir_from_error(ex)
                if not policy.dry_run and missing_virtiofs_dir is not None:
                    log.warning(
                        'VM {} has stale virtiofs source {}; recreating VM definition',
                        cfg.vm.name,
                        missing_virtiofs_dir,
                    )
                    create_or_start_vm(
                        cfg,
                        dry_run=False,
                        recreate=True,
                        share_source_dir=(
                            virtiofs_mapping[0] if virtiofs_mapping else ''
                        ),
                        share_tag=(
                            virtiofs_mapping[1] if virtiofs_mapping else ''
                        ),
                    )
                else:
                    raise
            vm_running = (
                True
                if policy.dry_run
                else _probe_vm_running_nonsudo(cfg.vm.name)
            )
            if (
                virtiofs_mapping is not None
                and not policy.dry_run
                and vm_running is True
            ):
                mappings = vm_share_mappings(cfg, use_sudo=False)
                if attachment.mode == ATTACHMENT_MODE_SHARED:
                    attachment = drift_align_attachment_tag_with_mappings(
                        attachment, host_src, mappings
                    )
                    virtiofs_mapping = _virtiofs_mapping_for_attachment(
                        cfg, attachment
                    )
                if virtiofs_mapping is not None:
                    req_src, req_tag = virtiofs_mapping
                    has_share = any(
                        src == req_src and tag == req_tag
                        for src, tag in mappings
                    )

        if (
            virtiofs_mapping is not None
            and not policy.dry_run
            and vm_running is True
            and not has_share
        ):
            vm_has_shared_mem = vm_has_virtiofs_shared_memory(
                cfg, use_sudo=False
            )
            if vm_has_shared_mem is False and not policy.recreate_if_needed:
                raise RuntimeError(
                    'Existing VM cannot accept virtiofs attachments because its domain '
                    'definition lacks required shared-memory backing (memfd/shared).\n'
                    f'VM: {cfg.vm.name}\n'
                    f'Requested: source={virtiofs_mapping[0]} tag={virtiofs_mapping[1]} '
                    f'guest_dst={attachment.guest_dst}\n'
                    'Next steps:\n'
                    '  - Re-run with --recreate_if_needed to rebuild the VM definition '
                    'with virtiofs shared-memory support.\n'
                    '  - Or destroy and recreate the VM with the desired share mapping.'
                )
            if policy.recreate_if_needed:
                recreate = True
            else:
                try:
                    if attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
                        with mgr.intent(
                            'Attach and reconcile shared-root mapping',
                            why='Ensure the requested host folder is exposed to the running VM before guest-side bind reconciliation.',
                            role='modify',
                        ):
                            _ensure_shared_root_host_bind(
                                cfg,
                                attachment,
                                yes=bool(policy.yes),
                                dry_run=False,
                            )
                            _ensure_shared_root_vm_mapping(
                                cfg,
                                yes=bool(policy.yes),
                                dry_run=False,
                                vm_running=True,
                            )
                        shared_root_host_side_ready = True
                    else:
                        attach_vm_share(
                            cfg,
                            virtiofs_mapping[0],
                            virtiofs_mapping[1],
                            dry_run=False,
                            vm_running=True,
                        )
                    has_share = True
                except Exception as ex:
                    current_maps = mappings or vm_share_mappings(
                        cfg, use_sudo=False
                    )
                    requested_tag = virtiofs_mapping[1]
                    if current_maps:
                        found = '\n'.join(
                            f'  - source={src or "(none)"} tag={tag or "(none)"}'
                            for src, tag in current_maps
                        )
                    else:
                        found = '  - (no filesystem mappings found)'
                    raise RuntimeError(
                        'Existing VM does not include requested share mapping, and live attach failed.\n'
                        f'VM: {cfg.vm.name}\n'
                        f'Requested: source={virtiofs_mapping[0]} tag={requested_tag} guest_dst={attachment.guest_dst}\n'
                        'Current VM filesystem mappings:\n'
                        f'{found}\n'
                        f'Live attach error: {ex}\n'
                        'Next steps:\n'
                        '  - Re-run with --recreate_if_needed to rebuild the VM definition with the new share.\n'
                        '  - Or use a VM already defined with this share mapping.'
                    )

        if recreate:
            create_or_start_vm(
                cfg,
                dry_run=policy.dry_run,
                recreate=True,
                share_source_dir=(
                    virtiofs_mapping[0] if virtiofs_mapping else ''
                ),
                share_tag=(virtiofs_mapping[1] if virtiofs_mapping else ''),
            )

        return ReconcileResult(
            attachment=attachment,
            cached_ip=cached_ip,
            cached_ssh_ok=cached_ssh_ok,
            shared_root_host_side_ready=shared_root_host_side_ready,
        )


def _prepare_attached_session(
    *,
    config_opt: str | None,
    vm_opt: str,
    host_src: Path,
    guest_dst_opt: str,
    attach_mode_opt: str = '',
    attach_access_opt: str = '',
    recreate_if_needed: bool,
    ensure_firewall_opt: bool,
    force: bool,
    dry_run: bool,
    yes: bool,
) -> PreparedSession:
    """Prepare a ready-to-use VM session rooted at a host folder attachment.

    If no VM context exists, this may bootstrap ``config init`` + ``vm create``
    (with consent/non-interactive policy checks), then continue with attachment,
    IP/SSH readiness, and in-guest mount reconciliation.
    """
    if not host_src.exists():
        raise FileNotFoundError(f'Host source path does not exist: {host_src}')
    if not host_src.is_dir():
        raise RuntimeError(f'Host source path is not a directory: {host_src}')

    try:
        cfg, cfg_path = _resolve_cfg_for_code(
            config_opt=config_opt,
            vm_opt=vm_opt,
            host_src=host_src,
        )
    except RuntimeError as ex:
        msg = str(ex)
        if 'No VM definitions found in config store' not in msg:
            raise
        prefix = 'No VM definitions found in config store: '
        missing_store_path = _cfg_path(config_opt)
        if msg.startswith(prefix):
            tail = msg[len(prefix):]
            # Avoid brittle regex parsing: split at our known guidance suffix.
            store_str = tail.split('. Run `aivm config init`', 1)[0].strip()
            if store_str:
                missing_store_path = Path(store_str).expanduser().resolve()
        missing_store = load_store(missing_store_path)
        need_init = missing_store.defaults is None
        if not yes:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    'No managed VM found for this folder. Re-run with --yes to create one automatically.'
                ) from ex
            print('No managed VM found for this folder.')
            if need_init:
                prompt = (
                    'Run `aivm config init` and `aivm vm create` now? [Y/n]: '
                )
            else:
                prompt = 'Run `aivm vm create` now using existing config defaults? [Y/n]: '
            ans = input(prompt).strip().lower()
            if ans not in {'', 'y', 'yes'}:
                raise RuntimeError('Aborted by user.') from ex
        if need_init:
            from ..cli.config import InitCLI

            InitCLI.main(
                argv=False,
                config=config_opt,
                yes=bool(yes),
                defaults=bool(yes),
                force=False,
            )
        from ..cli.vm import VMCreateCLI

        VMCreateCLI.main(
            argv=False,
            config=config_opt,
            vm=vm_opt,
            yes=bool(yes),
            dry_run=bool(dry_run),
            force=False,
        )
        cfg, cfg_path = _resolve_cfg_for_code(
            config_opt=config_opt,
            vm_opt=vm_opt,
            host_src=host_src,
        )

    if attach_mode_opt or attach_access_opt:
        attachment = _resolve_attachment(
            cfg,
            cfg_path,
            host_src,
            guest_dst_opt,
            attach_mode_opt,
            attach_access_opt,
        )
    else:
        attachment = _resolve_attachment(cfg, cfg_path, host_src, guest_dst_opt)
    reconcile = _reconcile_attached_vm(
        cfg,
        host_src,
        attachment,
        policy=ReconcilePolicy(
            ensure_firewall_opt=bool(ensure_firewall_opt),
            recreate_if_needed=bool(recreate_if_needed),
            dry_run=bool(dry_run),
            yes=bool(yes),
        ),
    )
    attachment = reconcile.attachment
    cached_ip = reconcile.cached_ip

    if (not dry_run) and _maybe_offer_create_ssh_identity(
        cfg,
        yes=bool(yes),
        prompt_reason=(
            'Generate a dedicated SSH keypair so aivm can open SSH/VS Code '
            'sessions and provision the guest.'
        ),
    ):
        _record_vm(
            cfg,
            cfg_path,
            reason=(
                f'Persist newly generated SSH identity paths for VM '
                f'{cfg.vm.name} before preparing the attached session.'
            ),
        )

    if dry_run:
        return PreparedSession(
            cfg=cfg,
            cfg_path=cfg_path,
            host_src=host_src,
            attachment_mode=attachment.mode,
            share_source_dir=attachment.source_dir,
            share_tag=attachment.tag,
            share_guest_dst=attachment.guest_dst,
            ip=None,
            reg_path=None,
            meta_path=None,
        )

    reg_path = _record_attachment(
        cfg,
        cfg_path,
        host_src=host_src,
        mode=attachment.mode,
        access=attachment.access,
        guest_dst=attachment.guest_dst,
        tag=attachment.tag,
        force=bool(force),
    )

    ip = cached_ip if cached_ip else get_ip_cached(cfg)
    if ip:
        ssh_ok = bool(probe_ssh_ready(cfg, ip).ok)
    else:
        ssh_ok = False
    if not ssh_ok:
        ip = wait_for_ip(cfg, timeout_s=360, dry_run=False)
        wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
    if not ip:
        raise RuntimeError('Could not resolve VM IP address.')
    mirror_home = bool(load_store(cfg_path).behavior.mirror_shared_home_folders)
    if attachment.mode in {ATTACHMENT_MODE_SHARED, ATTACHMENT_MODE_SHARED_ROOT}:
        _ensure_attachment_available_in_guest(
            cfg,
            host_src,
            attachment,
            ip,
            yes=bool(yes),
            dry_run=False,
            ensure_shared_root_host_side=(
                attachment.mode == ATTACHMENT_MODE_SHARED_ROOT
                and not reconcile.shared_root_host_side_ready
            ),
            mirror_home=mirror_home,
        )
        _restore_saved_vm_attachments(
            cfg,
            cfg_path,
            ip=ip,
            primary_attachment=attachment,
            yes=bool(yes),
            mirror_home=mirror_home,
        )
    else:
        _ensure_git_clone_attachment(
            cfg,
            host_src,
            attachment,
            ip,
            yes=bool(yes),
            dry_run=False,
        )
        # Apply companion-symlink and mirror-home behavior for git mode too.
        # The git clone creates the primary destination; symlinks come after.
        _apply_guest_derived_symlinks(
            cfg,
            ip,
            host_src,
            attachment,
            mirror_home=mirror_home,
        )
        _restore_saved_vm_attachments(
            cfg,
            cfg_path,
            ip=ip,
            primary_attachment=None,
            yes=bool(yes),
            mirror_home=mirror_home,
        )
    return PreparedSession(
        cfg=cfg,
        cfg_path=cfg_path,
        host_src=host_src,
        attachment_mode=attachment.mode,
        share_source_dir=attachment.source_dir,
        share_tag=attachment.tag,
        share_guest_dst=attachment.guest_dst,
        ip=ip,
        reg_path=reg_path,
        meta_path=None,
    )


