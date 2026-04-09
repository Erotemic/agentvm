"""CLI commands for VM lifecycle, attach/code/ssh workflows, and sync/provision."""

from __future__ import annotations

import shlex
import sys
from copy import deepcopy
from dataclasses import asdict
from pathlib import Path
from typing import Any

import scriptconfig as scfg

from ..attachments.guest import (
    _ensure_attachment_available_in_guest,
    _upsert_ssh_config_entry,
)
from ..attachments.resolve import (
    ATTACHMENT_MODE_DECLARED,
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    _normalize_attachment_access,
    _normalize_attachment_mode,
    _resolve_attachment,
)
from ..attachments.declared import (
    _prepare_declared_attachment_host_and_vm,
    _reconcile_declared_attachments_in_guest,
    _sync_declared_attachment_manifest_on_host,
)
from ..attachments.session import (
    _maybe_warn_hardware_drift,
    _prepare_attached_session,
    _record_attachment,
    _resolve_ip_for_ssh_ops,
)
from ..attachments.shared_root import (
    _detach_shared_root_guest_bind,
    _detach_shared_root_host_bind,
    _ensure_shared_root_host_bind,
    _ensure_shared_root_vm_mapping,
)
from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args
from ..status import (
    probe_vm_state,
)
from ..store import (
    find_attachment_for_vm,
    find_network,
    load_store,
    network_users,
    remove_attachment,
    remove_vm,
    save_store,
)
from ..util import which
from ..vm import (
    attach_vm_share,
    create_or_start_vm,
    destroy_vm,
    detach_vm_share,
    provision,
    restart_vm,
    shutdown_vm,
    sync_settings,
    vm_share_mappings,
    vm_status,
    wait_for_ip,
)
from ..vm import (
    ssh_config as mk_ssh_config,
)
from ..vm.drift import (
    attachment_has_mapping as drift_attachment_has_mapping,
)
from ..vm.share import (
    ResolvedAttachment,
)
from ..vm.share import (
    align_attachment_tag_with_mappings as drift_align_attachment_tag_with_mappings,
)
from ..vm.create_ops import (
    create_vm_from_defaults,
)
from ..vm.update_ops import (
    _apply_vm_update,
    _maybe_restart_vm_after_update,
    _print_vm_update_plan,
    _vm_update_drift,
)
from ._common import (
    _BaseCommand,
    _cfg_path,
    _load_cfg,
    _load_cfg_with_path,
    _maybe_install_missing_host_deps,
    _maybe_offer_create_ssh_identity,
    _record_vm,
    _resolve_cfg_for_code,
    log,
)


class VMUpCLI(_BaseCommand):
    """Create the VM if needed, or start it if already defined."""

    recreate: Any = scfg.Value(
        False, isflag=True, help='Destroy and recreate if it exists.'
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config)
        _maybe_install_missing_host_deps(
            yes=bool(args.yes), dry_run=bool(args.dry_run)
        )
        mgr = CommandManager.current()
        with mgr.intent(
            f'Create/start VM {cfg.vm.name}',
            why='Ensure the managed VM exists and is running with the configured resources.',
            role='modify',
        ):
            create_or_start_vm(
                cfg, dry_run=args.dry_run, recreate=args.recreate
            )
        if not args.dry_run and not args.recreate:
            _maybe_warn_hardware_drift(cfg)
        if not args.dry_run:
            _record_vm(cfg, cfg_path)
        return 0


class VMDownCLI(_BaseCommand):
    """Gracefully shut down the VM."""

    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Shut down VM {cfg.vm.name}',
            why='Gracefully stop the VM by sending an ACPI shutdown signal to the guest OS.',
            role='modify',
        ):
            shutdown_vm(cfg, dry_run=args.dry_run)
        return 0


class VMRestartCLI(_BaseCommand):
    """Gracefully restart the VM (shutdown then start)."""

    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Restart VM {cfg.vm.name}',
            why='Gracefully stop and then start the VM to apply changes or recover from transient issues.',
            role='modify',
        ):
            restart_vm(cfg, dry_run=args.dry_run)
        return 0


class VMCreateCLI(_BaseCommand):
    """Create a managed VM from config-store defaults and start it."""

    vm: Any = scfg.Value('', help='Optional VM name override.')
    set_default: Any = scfg.Value(
        False,
        isflag=True,
        help='Set the created VM as the active default VM.',
    )
    force: Any = scfg.Value(
        False,
        isflag=True,
        help='Overwrite existing VM entry and recreate VM definition if present.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMCreateCLI.main vm={} set_default={} force={} dry_run={} yes={}',
            args.vm,
            bool(args.set_default),
            bool(args.force),
            bool(args.dry_run),
            bool(args.yes),
        )
        cfg_path = _cfg_path(args.config)
        return create_vm_from_defaults(
            cfg_path,
            vm_override=args.vm if args.vm else None,
            set_default=bool(args.set_default),
            force=bool(args.force),
            dry_run=bool(args.dry_run),
            yes=bool(args.yes),
        )


class VMWaitIPCLI(_BaseCommand):
    """Wait for and print the VM IPv4 address."""

    timeout: Any = scfg.Value(360, type=int, help='Timeout seconds.')
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _load_cfg(args.config)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Wait for IP for {cfg.vm.name}',
            why='Inspect the VM networking state until an IPv4 address is available.',
            role='read',
        ):
            print(
                wait_for_ip(
                    cfg,
                    timeout_s=args.timeout,
                    dry_run=args.dry_run,
                )
            )
        return 0


class VMStatusCLI(_BaseCommand):
    """Show VM lifecycle status and cached IP information."""

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _load_cfg(args.config)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Inspect VM {cfg.vm.name}',
            why='Read the live libvirt state and cached IP for this managed VM.',
            role='read',
        ):
            print(vm_status(cfg))
        return 0


class VMDestroyCLI(_BaseCommand):
    """Destroy and undefine the VM (shared host directories are not deleted)."""

    vm: Any = scfg.Value(
        '',
        position=1,
        help='Optional VM name override (positional).',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config, vm_opt=args.vm)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Destroy VM {cfg.vm.name}',
            why=(
                'Remove the managed VM domain while leaving host project directories intact.'
            ),
            role='modify',
        ):
            destroy_vm(cfg, dry_run=args.dry_run)
        if not args.dry_run:
            reg = load_store(cfg_path)
            remove_vm(reg, cfg.vm.name, remove_attachments=True)
            save_store(
                reg,
                cfg_path,
                reason=(
                    f'Remove VM record for {cfg.vm.name} after destroying the '
                    'managed libvirt domain.'
                ),
            )
            net_name = (cfg.network.name or '').strip()
            if net_name:
                net = find_network(reg, net_name)
                if net is not None and not network_users(reg, net_name):
                    log.warning(
                        "Network '{}' now has no VM users and remains defined. "
                        'Destroy it explicitly if no longer needed: aivm host net destroy {}',
                        net_name,
                        net_name,
                    )
        return 0


class VMSshConfigCLI(_BaseCommand):
    """Print an SSH config stanza for easy VM access."""

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        print(mk_ssh_config(_load_cfg(args.config)))
        return 0


class VMProvisionCLI(_BaseCommand):
    """Provision the VM with optional developer packages."""

    vm: Any = scfg.Value(
        '',
        help='Optional VM name override.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        if args.config is not None or _cfg_path(None).exists():
            cfg = _load_cfg(args.config)
        else:
            cfg, _ = _resolve_cfg_for_code(
                config_opt=None,
                vm_opt=args.vm,
                host_src=Path.cwd(),
            )
        if not args.dry_run:
            _resolve_ip_for_ssh_ops(
                cfg,
                yes=bool(args.yes),
                purpose='Query VM networking state before SSH provisioning.',
            )
        provision(cfg, dry_run=args.dry_run)
        return 0


class VMSyncSettingsCLI(_BaseCommand):
    """Copy host user settings/files into the VM user home."""

    paths: Any = scfg.Value(
        '',
        help=(
            'Optional comma-separated host paths to sync. '
            'Defaults to [sync].paths from config.'
        ),
    )
    overwrite: Any = scfg.Value(
        True,
        isflag=True,
        help='Overwrite existing files in VM (default true).',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _load_cfg(args.config)
        if args.dry_run:
            ip = '0.0.0.0'
        else:
            ip = _resolve_ip_for_ssh_ops(
                cfg,
                yes=bool(args.yes),
                purpose='Query VM networking state before settings sync.',
            )
        chosen_paths = _parse_sync_paths_arg(args.paths) if args.paths else None
        result = sync_settings(
            cfg,
            ip,
            paths=chosen_paths,
            overwrite=bool(args.overwrite),
            dry_run=args.dry_run,
        )
        print('🧩 Settings sync summary')
        print(f'  copied: {len(result.copied)}')
        print(f'  skipped_missing: {len(result.skipped_missing)}')
        print(f'  skipped_exists: {len(result.skipped_exists)}')
        print(f'  failed: {len(result.failed)}')
        for k in ('copied', 'skipped_missing', 'skipped_exists', 'failed'):
            for item in getattr(result, k):
                print(f'  - {k}: {item}')
        if result.failed:
            return 2
        return 0


class VMCodeCLI(_BaseCommand):
    """Open a host project folder in VS Code attached to the VM via Remote-SSH."""

    host_src: Any = scfg.Value(
        '.',
        position=1,
        help='Host project directory to share and open (default: current directory).',
    )
    vm: Any = scfg.Value(
        '',
        help='VM name override.',
    )
    guest_dst: Any = scfg.Value(
        '',
        help='Guest mount path override (default: mirrors host_src path).',
    )
    mode: Any = scfg.Value(
        '',
        help='Attachment mode override: shared, shared-root, persistent, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is supported for shared, shared-root, and persistent modes.',
    )
    recreate_if_needed: Any = scfg.Value(
        False,
        isflag=True,
        help='Recreate VM if existing definition lacks the requested share mapping.',
    )
    ensure_firewall: Any = scfg.Value(
        True,
        isflag=True,
        help='Apply firewall rules when firewall.enabled=true.',
    )
    sync_settings: Any = scfg.Value(
        False,
        isflag=True,
        help='Sync host settings files into VM before launching VS Code.',
    )
    sync_paths: Any = scfg.Value(
        '',
        help=(
            'Optional comma-separated paths used when --sync_settings is set. '
            'Defaults to [sync].paths.'
        ),
    )
    force: Any = scfg.Value(
        False,
        isflag=True,
        help='Deprecated no-op; multiple VMs may attach the same folder.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMCodeCLI.main host_src={} vm={} guest_dst={} dry_run={} yes={}',
            args.host_src,
            args.vm,
            args.guest_dst,
            bool(args.dry_run),
            bool(args.yes),
        )
        try:
            session = _prepare_attached_session(
                config_opt=args.config,
                vm_opt=args.vm,
                host_src=Path(args.host_src).expanduser().absolute(),
                guest_dst_opt=args.guest_dst,
                attach_mode_opt=args.mode,
                attach_access_opt=args.access,
                recreate_if_needed=bool(args.recreate_if_needed),
                ensure_firewall_opt=bool(args.ensure_firewall),
                force=bool(args.force),
                dry_run=bool(args.dry_run),
                yes=bool(args.yes),
            )
        except RuntimeError as ex:
            log.opt(exception=True).trace('Failed preparing code session')
            log.error(str(ex))
            return 1
        cfg = session.cfg
        if args.dry_run:
            print(
                f'DRYRUN: would open {session.share_guest_dst} in VS Code via host {cfg.vm.name}'
            )
            return 0
        ip = session.ip
        assert ip is not None

        do_sync = bool(args.sync_settings or cfg.sync.enabled)
        if do_sync:
            chosen_paths = (
                _parse_sync_paths_arg(args.sync_paths)
                if args.sync_paths
                else None
            )
            sync_result = sync_settings(
                cfg,
                ip,
                paths=chosen_paths,
                overwrite=cfg.sync.overwrite,
                dry_run=False,
            )
            if sync_result.failed:
                raise RuntimeError(
                    'Failed syncing one or more settings files:\n'
                    + '\n'.join(sync_result.failed)
                )

        ssh_cfg, ssh_cfg_updated = _upsert_ssh_config_entry(
            cfg, dry_run=False, yes=bool(args.yes)
        )

        if which('code') is None:
            raise RuntimeError(
                'VS Code CLI `code` not found in PATH. Install VS Code and enable the shell command.'
            )
        remote_target = f'ssh-remote+{cfg.vm.name}'
        CommandManager.current().run(
            ['code', '--remote', remote_target, session.share_guest_dst],
            sudo=False,
            check=True,
            capture=False,
        )
        print(
            f'Opened VS Code remote folder {session.share_guest_dst} on host {cfg.vm.name}'
        )
        if ssh_cfg_updated:
            print(f'SSH entry updated in {ssh_cfg}')
        print(f'Folder registered in {session.reg_path}')
        return 0


class VMSSHCLI(_BaseCommand):
    """SSH into the VM and start a shell in the mapped guest directory."""

    host_src: Any = scfg.Value(
        '.',
        position=1,
        help='Host project directory to share and open (default: current directory).',
    )
    vm: Any = scfg.Value(
        '',
        help='VM name override.',
    )
    guest_dst: Any = scfg.Value(
        '',
        help='Guest mount path override (default: mirrors host_src path).',
    )
    mode: Any = scfg.Value(
        '',
        help='Attachment mode override: shared, shared-root, persistent, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is supported for shared, shared-root, and persistent modes.',
    )
    recreate_if_needed: Any = scfg.Value(
        False,
        isflag=True,
        help='Recreate VM if existing definition lacks the requested share mapping.',
    )
    ensure_firewall: Any = scfg.Value(
        True,
        isflag=True,
        help='Apply firewall rules when firewall.enabled=true.',
    )
    force: Any = scfg.Value(
        False,
        isflag=True,
        help='Deprecated no-op; multiple VMs may attach the same folder.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMSSHCLI.main host_src={} vm={} guest_dst={} dry_run={} yes={}',
            args.host_src,
            args.vm,
            args.guest_dst,
            bool(args.dry_run),
            bool(args.yes),
        )
        try:
            session = _prepare_attached_session(
                config_opt=args.config,
                vm_opt=args.vm,
                host_src=Path(args.host_src).expanduser().absolute(),
                guest_dst_opt=args.guest_dst,
                attach_mode_opt=args.mode,
                attach_access_opt=args.access,
                recreate_if_needed=bool(args.recreate_if_needed),
                ensure_firewall_opt=bool(args.ensure_firewall),
                force=bool(args.force),
                dry_run=bool(args.dry_run),
                yes=bool(args.yes),
            )
        except RuntimeError as ex:
            log.error(str(ex))
            return 1
        cfg = session.cfg
        if args.dry_run:
            print(
                f'DRYRUN: would SSH to {cfg.vm.user}@<ip> and cd {session.share_guest_dst}'
            )
            return 0

        ip = session.ip
        assert ip is not None
        ssh_cfg, ssh_cfg_updated = _upsert_ssh_config_entry(
            cfg, dry_run=False, yes=bool(args.yes)
        )
        ident = require_ssh_identity(cfg.paths.ssh_identity_file)
        remote_cmd = (
            f'cd {shlex.quote(session.share_guest_dst)} && exec $SHELL -l'
        )
        ssh_result = CommandManager.current().run(
            [
                'ssh',
                '-t',
                *ssh_base_args(ident, strict_host_key_checking='accept-new'),
                f'{cfg.vm.user}@{ip}',
                remote_cmd,
            ],
            sudo=False,
            check=False,
            capture=False,
        )
        if ssh_result.code != 0:
            log.error(
                'SSH command failed (exit code {}) for {}@{}',
                ssh_result.code,
                cfg.vm.user,
                ip,
            )
            return int(ssh_result.code) if ssh_result.code else 1
        print(f'SSH session ended for {cfg.vm.user}@{ip}')
        if ssh_cfg_updated:
            print(f'SSH entry updated in {ssh_cfg}')
        print(f'Folder registered in {session.reg_path}')
        return 0


class VMAttachCLI(_BaseCommand):
    """Attach/register a host directory to an existing managed VM."""

    vm: Any = scfg.Value('', help='Optional VM name override.')
    host_src: Any = scfg.Value(
        '.', position=1, help='Host directory to attach.'
    )
    guest_dst: Any = scfg.Value('', help='Guest mount path override.')
    mode: Any = scfg.Value(
        '',
        help='Attachment mode: shared, shared-root, persistent, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access: rw or ro (default: saved access or rw). ro is supported for shared, shared-root, and persistent modes.',
    )
    force: Any = scfg.Value(
        False,
        isflag=True,
        help='Deprecated no-op; multiple VMs may attach the same folder.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMAttachCLI.main host_src={} vm={} guest_dst={} mode={} access={} force={} dry_run={} yes={}',
            args.host_src,
            args.vm,
            args.guest_dst,
            args.mode,
            args.access,
            bool(args.force),
            bool(args.dry_run),
            bool(args.yes),
        )
        host_src = Path(args.host_src).expanduser().absolute()
        if not host_src.exists() or not host_src.is_dir():
            raise RuntimeError(
                f'host_src must be an existing directory: {host_src}'
            )

        if args.config:
            cfg, cfg_path = _load_cfg_with_path(args.config, vm_opt=args.vm)
        elif args.vm:
            cfg, cfg_path = _load_cfg_with_path(None, vm_opt=args.vm)
        else:
            cfg, cfg_path = _resolve_cfg_for_code(
                config_opt=None,
                vm_opt='',
                host_src=host_src,
            )

        attachment = _resolve_attachment(
            cfg, cfg_path, host_src, args.guest_dst, args.mode, args.access
        )
        reg = load_store(cfg_path)
        mirror_home = bool(reg.behavior.mirror_shared_home_folders)

        if args.dry_run:
            print(
                f'DRYRUN: would attach {host_src} to VM {cfg.vm.name} at {attachment.guest_dst} ({attachment.mode} mode, access={attachment.access})'
            )
            return 0

        _record_vm(
            cfg,
            cfg_path,
            reason=(
                f'Persist resolved VM/network metadata before attaching '
                f'{host_src} to {cfg.vm.name}.'
            ),
        )
        vm_running = False
        vm_defined = False
        sudo_confirmed = False
        vm_out, vm_defined_probe = probe_vm_state(cfg, use_sudo=False)
        vm_running_probe = bool(vm_out.ok)
        vm_defined = bool(vm_defined_probe)
        if not vm_defined:
            sudo_confirmed = True
            vm_out, vm_defined_probe = probe_vm_state(cfg, use_sudo=True)
            vm_running_probe = bool(vm_out.ok)
            vm_defined = bool(vm_defined_probe)
        if vm_defined:
            vm_running = vm_running_probe is True
            if attachment.mode == ATTACHMENT_MODE_SHARED:
                if not sudo_confirmed:
                    sudo_confirmed = True
                mappings = vm_share_mappings(cfg)
                attachment = drift_align_attachment_tag_with_mappings(
                    attachment, host_src, mappings
                )
                if not drift_attachment_has_mapping(cfg, attachment, mappings):
                    attach_vm_share(
                        cfg,
                        attachment.source_dir,
                        attachment.tag,
                        dry_run=False,
                    )
            elif attachment.mode in {
                ATTACHMENT_MODE_SHARED_ROOT,
                ATTACHMENT_MODE_DECLARED,
            }:
                if not vm_running:
                    mgr = CommandManager.current()
                    with mgr.intent(
                        'Attach and reconcile shared-root mapping',
                        why='Ensure the requested host folder is exposed to the VM before the next guest session uses it.',
                        role='modify',
                    ):
                        if attachment.mode == ATTACHMENT_MODE_DECLARED:
                            _prepare_declared_attachment_host_and_vm(
                                cfg,
                                attachment,
                                dry_run=False,
                                vm_running=False,
                            )
                        else:
                            _ensure_shared_root_host_bind(
                                cfg,
                                attachment,
                                yes=bool(args.yes),
                                dry_run=False,
                            )
                            _ensure_shared_root_vm_mapping(
                                cfg,
                                yes=bool(args.yes),
                                dry_run=False,
                                vm_running=False,
                            )
        reg_path = _record_attachment(
            cfg,
            cfg_path,
            host_src=host_src,
            mode=attachment.mode,
            access=attachment.access,
            guest_dst=attachment.guest_dst,
            tag=attachment.tag,
            force=bool(args.force),
        )
        if attachment.mode == ATTACHMENT_MODE_DECLARED:
            _sync_declared_attachment_manifest_on_host(
                cfg,
                cfg_path,
                dry_run=False,
            )
        if vm_running:
            if _maybe_offer_create_ssh_identity(
                cfg,
                yes=bool(args.yes),
                prompt_reason=(
                    'Generate a dedicated SSH keypair so aivm can reconcile '
                    'the running VM guest attachment state.'
                ),
            ):
                _record_vm(
                    cfg,
                    cfg_path,
                    reason=(
                        f'Persist newly generated SSH identity paths for VM '
                        f'{cfg.vm.name} before guest attachment reconciliation.'
                    ),
                )
            log.info(
                'VM {} is running; reconciling attachment in guest: {} (mode={} access={})',
                cfg.vm.name,
                attachment.guest_dst,
                attachment.mode,
                attachment.access,
            )
            ip = _resolve_ip_for_ssh_ops(
                cfg,
                yes=bool(args.yes),
                purpose='Query VM networking state before reconciling attached folder.',
            )
            _ensure_attachment_available_in_guest(
                cfg,
                host_src,
                attachment,
                ip,
                yes=bool(args.yes),
                dry_run=False,
                ensure_shared_root_host_side=(
                    attachment.mode
                    in {ATTACHMENT_MODE_SHARED_ROOT, ATTACHMENT_MODE_DECLARED}
                ),
                mirror_home=mirror_home,
            )
            if attachment.mode == ATTACHMENT_MODE_DECLARED:
                _reconcile_declared_attachments_in_guest(
                    cfg,
                    cfg_path,
                    ip,
                    dry_run=False,
                )
        print(
            f'Attached {host_src} to VM {cfg.vm.name} ({attachment.mode} mode, access={attachment.access})'
        )
        if vm_running and attachment.mode in {
            ATTACHMENT_MODE_DECLARED,
            ATTACHMENT_MODE_SHARED,
            ATTACHMENT_MODE_SHARED_ROOT,
        }:
            print(f'Mounted in running VM at {attachment.guest_dst}')
        elif vm_running:
            print(f'Guest clone ready at {attachment.guest_dst}')
        elif vm_defined:
            if attachment.mode in {
                ATTACHMENT_MODE_DECLARED,
                ATTACHMENT_MODE_SHARED,
                ATTACHMENT_MODE_SHARED_ROOT,
            }:
                print(
                    f'VM {cfg.vm.name} is not running; share will mount when VM is running and attach/ssh/code is used.'
                )
            else:
                print(
                    f'VM {cfg.vm.name} is not running; guest clone will be created when VM is running and attach/ssh/code is used.'
                )
        print(f'Updated config store: {cfg_path}')
        print(f'Updated attachments: {reg_path}')
        return 0


class VMDetachCLI(_BaseCommand):
    """Detach/unregister a host directory from a managed VM."""

    vm: Any = scfg.Value('', help='Optional VM name override.')
    host_src: Any = scfg.Value(
        '.', position=1, help='Host directory to detach.'
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        host_src = Path(args.host_src).resolve()
        if not host_src.exists() or not host_src.is_dir():
            raise RuntimeError(
                f'host_src must be an existing directory: {host_src}'
            )
        cfg, cfg_path = _resolve_cfg_for_code(
            config_opt=args.config,
            vm_opt=args.vm,
            host_src=host_src,
        )
        reg = load_store(cfg_path)
        att = find_attachment_for_vm(reg, host_src, cfg.vm.name)
        if att is None:
            print(
                f'No attachment found for {host_src} on VM {cfg.vm.name}. '
                'Nothing to do.'
            )
            return 0
        if args.dry_run:
            print(
                f'DRYRUN: would detach {host_src} from VM {cfg.vm.name} ({att.mode} mode)'
            )
            return 0

        vm_out, vm_defined = probe_vm_state(cfg, use_sudo=False)
        vm_defined_probe = vm_defined
        if vm_defined_probe is False:
            vm_out, vm_defined = probe_vm_state(cfg, use_sudo=True)
            vm_defined_probe = vm_defined
        vm_running = bool(vm_out.ok)
        mode = _normalize_attachment_mode(att.mode)
        resolved = ResolvedAttachment(
            vm_name=cfg.vm.name,
            mode=mode,
            access=_normalize_attachment_access(att.access),
            source_dir=str(host_src),
            guest_dst=att.guest_dst or str(host_src),
            tag=att.tag,
        )

        detached_share = False
        detached_shared_root_host_bind = False
        detached_shared_root_guest_bind = False
        detach_failed = False

        if (
            mode == ATTACHMENT_MODE_SHARED
            and vm_defined_probe is True
            and att.tag
        ):
            detached_share = detach_vm_share(
                cfg, att.host_path, att.tag, dry_run=False
            )

        if mode == ATTACHMENT_MODE_SHARED_ROOT:
            if vm_running:
                try:
                    ip = _resolve_ip_for_ssh_ops(
                        cfg,
                        yes=bool(args.yes),
                        purpose='Query VM networking state before detaching shared-root guest mount.',
                    )
                    _detach_shared_root_guest_bind(
                        cfg,
                        ip,
                        resolved,
                        dry_run=False,
                    )
                    detached_shared_root_guest_bind = True
                except Exception as ex:
                    detach_failed = True
                    log.warning(
                        'Could not detach shared-root guest bind mount for VM {} at {}: {}',
                        cfg.vm.name,
                        resolved.guest_dst,
                        ex,
                    )
            if resolved.tag:
                try:
                    _detach_shared_root_host_bind(
                        cfg,
                        resolved,
                        yes=bool(args.yes),
                        dry_run=False,
                    )
                    detached_shared_root_host_bind = True
                except Exception as ex:
                    detach_failed = True
                    log.warning(
                        'Could not detach shared-root host bind mount for VM {} source={} guest_dst={} token={}: {}',
                        cfg.vm.name,
                        resolved.source_dir,
                        resolved.guest_dst,
                        resolved.tag,
                        ex,
                    )
            else:
                detach_failed = True
                log.warning(
                    'Skipping shared-root host bind cleanup for VM {} source={} because attachment token is missing.',
                    cfg.vm.name,
                    resolved.source_dir,
                )
        elif mode == ATTACHMENT_MODE_DECLARED:
            removed = remove_attachment(
                reg, host_path=host_src, vm_name=cfg.vm.name
            )
            if removed:
                save_store(
                    reg,
                    cfg_path,
                    reason=(
                        f'Remove persistent attachment record for {host_src} from VM '
                        f'{cfg.vm.name}.'
                    ),
                )
                _sync_declared_attachment_manifest_on_host(
                    cfg,
                    cfg_path,
                    dry_run=False,
                )
            if vm_running:
                try:
                    ip = _resolve_ip_for_ssh_ops(
                        cfg,
                        yes=bool(args.yes),
                        purpose='Query VM networking state before reconciling persistent attachment removal.',
                    )
                    _reconcile_declared_attachments_in_guest(
                        cfg,
                        cfg_path,
                        ip,
                        dry_run=False,
                    )
                except Exception as ex:
                    detach_failed = True
                    log.warning(
                        'Could not reconcile persistent attachment removal for VM {} source={} guest_dst={} token={}: {}',
                        cfg.vm.name,
                        resolved.source_dir,
                        resolved.guest_dst,
                        resolved.tag,
                        ex,
                    )

        if detach_failed:
            log.error(
                'Detach cleanup was incomplete for {} on VM {}; preserving config record so detach can be retried.',
                host_src,
                cfg.vm.name,
            )
            return 2

        if mode != ATTACHMENT_MODE_DECLARED:
            removed = remove_attachment(
                reg, host_path=host_src, vm_name=cfg.vm.name
            )
            if removed:
                save_store(
                    reg,
                    cfg_path,
                    reason=(
                        f'Remove attachment record for {host_src} from VM '
                        f'{cfg.vm.name}.'
                    ),
                )

        print(f'Detached {host_src} from VM {cfg.vm.name} ({mode} mode)')
        if mode == ATTACHMENT_MODE_SHARED and vm_defined_probe is True:
            if detached_share:
                print('Detached virtiofs mapping from VM definition.')
            elif att.tag:
                print(
                    'No matching virtiofs mapping found in VM definition (already absent).'
                )
        if mode == ATTACHMENT_MODE_SHARED_ROOT:
            if detached_shared_root_host_bind:
                print('Detached shared-root host bind mount.')
            if vm_running and detached_shared_root_guest_bind:
                print('Detached shared-root guest bind mount.')
        if mode == ATTACHMENT_MODE_DECLARED:
            print(
                'Removed persistent attachment intent and refreshed the guest replay manifest.'
            )
        if vm_running and mode == ATTACHMENT_MODE_SHARED:
            print(
                f'If the guest still has {att.guest_dst or host_src} mounted, unmount it inside the VM manually.'
            )
        print(f'Updated config store: {cfg_path}')
        return 0


class VMListCLI(_BaseCommand):
    """List managed VM records (VM-focused view)."""

    section = scfg.Value(
        'vms',
        help='One of: all, vms, networks, folders (default: vms).',
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        from .main import ListCLI

        return ListCLI.main(
            argv=False, section=args.section, config=args.config
        )


class VMUpdateCLI(_BaseCommand):
    """Reconcile VM config drift against live libvirt settings."""

    vm: Any = scfg.Value('', help='Optional VM name override.')
    restart: Any = scfg.Value(
        'auto',
        help='Restart policy when changes require reboot to take effect: auto, always, never.',
    )
    dry_run: Any = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv: bool = True, **kwargs: Any) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        restart_policy = str(args.restart or 'auto').strip().lower()
        if restart_policy not in {'auto', 'always', 'never'}:
            raise RuntimeError('--restart must be one of: auto, always, never')
        cfg, _ = _load_cfg_with_path(args.config, vm_opt=args.vm)
        drift, vm_running = _vm_update_drift(cfg, yes=bool(args.yes))
        if drift.notes:
            print('Detected diagnostics (not auto-applied):')
            for note in drift.notes:
                print(f'  - {note}')
        if not drift.has_changes():
            print(f'VM {cfg.vm.name} is already in sync with config.')
            return 0
        _print_vm_update_plan(cfg, drift)
        mgr = CommandManager.current()
        with mgr.intent(
            f'Update VM {cfg.vm.name}',
            why='Apply editable libvirt hardware changes so the VM matches config.',
            role='modify',
        ):
            changed, restart_required = _apply_vm_update(
                cfg, drift, dry_run=bool(args.dry_run)
            )
        if changed and restart_required and vm_running:
            _maybe_restart_vm_after_update(
                cfg,
                restart_policy=restart_policy,
                dry_run=bool(args.dry_run),
                yes=bool(args.yes),
            )
        elif changed:
            print('Update complete.')
        return 0


class VMModalCLI(scfg.ModalCLI):
    """VM lifecycle subcommands."""

    list = VMListCLI
    create = VMCreateCLI
    up = VMUpCLI
    down = VMDownCLI
    restart = VMRestartCLI
    wait_ip = VMWaitIPCLI
    status = VMStatusCLI
    update = VMUpdateCLI
    destroy = VMDestroyCLI
    ssh_config = VMSshConfigCLI
    provision = VMProvisionCLI
    ssh = VMSSHCLI
    sync_settings = VMSyncSettingsCLI
    attach = VMAttachCLI
    detach = VMDetachCLI
    code = VMCodeCLI


def _parse_sync_paths_arg(paths_arg: str) -> list[str]:
    items = [p.strip() for p in (paths_arg or '').split(',')]
    return [p for p in items if p]
