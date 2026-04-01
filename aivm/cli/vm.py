"""CLI commands for VM lifecycle, attach/code/ssh workflows, and sync/provision."""

from __future__ import annotations

import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import scriptconfig as scfg

from ..attachments.guest import (
    _ensure_attachment_available_in_guest,
    _upsert_ssh_config_entry,
)
from ..attachments.resolve import (
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    _normalize_attachment_access,
    _normalize_attachment_mode,
    _resolve_attachment,
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
from ..firewall import apply_firewall
from ..net import ensure_network
from ..resource_checks import (
    vm_resource_impossible_lines,
    vm_resource_warning_lines,
)
from ..runtime import require_ssh_identity, ssh_base_args, virsh_system_cmd
from ..status import (
    probe_vm_state,
)
from ..store import (
    find_attachment_for_vm,
    find_network,
    find_vm,
    load_store,
    materialize_vm_cfg,
    network_users,
    remove_attachment,
    remove_vm,
    save_store,
    upsert_network,
    upsert_vm_with_network,
)
from ..util import which
from ..vm import (
    attach_vm_share,
    create_or_start_vm,
    destroy_vm,
    detach_vm_share,
    provision,
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
from ..vm.drift import (
    parse_dominfo_hardware as _parse_dominfo_hardware,
)
from ..vm.share import (
    ResolvedAttachment,
)
from ..vm.share import (
    align_attachment_tag_with_mappings as drift_align_attachment_tag_with_mappings,
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
        reg = load_store(cfg_path)
        if reg.defaults is not None:
            # Work on a copy so per-create overrides (e.g. --vm) never mutate
            # persisted defaults in the registry.
            cfg = deepcopy(reg.defaults).expanded_paths()
        elif reg.vms:
            # Fallback for stores that predate/omit [defaults]: use an existing
            # managed VM definition as the template source for new VM creation.
            template_name = (
                reg.active_vm if find_vm(reg, reg.active_vm) is not None else ''
            )
            if not template_name:
                template_name = sorted(v.name for v in reg.vms)[0]
            cfg = materialize_vm_cfg(reg, template_name).expanded_paths()
            log.warning(
                'No config defaults found; using managed VM {} as create template.',
                template_name,
            )
        else:
            log.error(
                f'No config defaults found in store: {cfg_path}. '
                'Run `aivm config init` first.'
            )
            return 1
        if args.vm:
            cfg.vm.name = str(args.vm).strip()
        for line in vm_resource_warning_lines(cfg):
            log.warning(line)
        if not bool(args.yes):
            cfg = _review_vm_create_overrides_interactive(cfg, cfg_path)
        problems = vm_resource_impossible_lines(cfg)
        if problems:
            detail = '\n  - '.join(problems)
            raise RuntimeError(
                'Requested VM resources are not feasible on this host right now:\n'
                f'  - {detail}\n'
                'Lower vm.ram_mb / vm.cpus and retry.'
            )
        net = find_network(reg, cfg.network.name)
        if net is None:
            upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
        else:
            cfg.network = type(net.network)(**asdict(net.network))
            cfg.firewall = type(net.firewall)(**asdict(net.firewall))
            cfg.network.name = net.name
        existing = find_vm(reg, cfg.vm.name)
        if existing is not None and not args.force:
            log.error(
                f"VM '{cfg.vm.name}' already exists in config store. "
                'Use --force to overwrite. Or use a different name and try again'
            )
            return 1
        _maybe_install_missing_host_deps(
            yes=bool(args.yes), dry_run=bool(args.dry_run)
        )
        mgr = CommandManager.current()
        with mgr.intent(
            f'Create VM {cfg.vm.name}',
            why='Provision the managed network, firewall, and VM definition from config defaults.',
            role='modify',
        ):
            ensure_network(cfg, recreate=False, dry_run=bool(args.dry_run))
            if cfg.firewall.enabled:
                apply_firewall(cfg, dry_run=bool(args.dry_run))
            create_or_start_vm(
                cfg,
                dry_run=bool(args.dry_run),
                recreate=bool(args.force and existing is not None),
            )
        if not args.dry_run:
            prev_active_vm = reg.active_vm
            upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
            set_active = bool(args.set_default)
            if (
                not set_active
                and not bool(args.yes)
                and prev_active_vm != cfg.vm.name
            ):
                set_active = _prompt_set_created_vm_default(cfg.vm.name)
            if not set_active:
                reg.active_vm = prev_active_vm
            save_store(
                reg,
                cfg_path,
                reason=(
                    f'Persist created VM record for {cfg.vm.name} and update '
                    'the active default selection.'
                ),
            )
        return 0


def _render_vm_create_summary(cfg: AgentVMConfig, path: Path) -> str:
    lines = [
        'Create VM from defaults:',
        f'  config_store: {path}',
        f'  vm.name: {cfg.vm.name}',
        f'  vm.user: {cfg.vm.user}',
        f'  vm.cpus: {cfg.vm.cpus}',
        f'  vm.ram_mb: {cfg.vm.ram_mb}',
        f'  vm.disk_gb: {cfg.vm.disk_gb}',
        f'  network.name: {cfg.network.name}',
        f'  network.subnet_cidr: {cfg.network.subnet_cidr}',
        f'  network.gateway_ip: {cfg.network.gateway_ip}',
        f'  network.dhcp_start: {cfg.network.dhcp_start}',
        f'  network.dhcp_end: {cfg.network.dhcp_end}',
    ]
    return '\n'.join(lines)


def _prompt_with_default(prompt: str, default: str) -> str:
    raw = input(f'{prompt} [{default}]: ').strip()
    return raw if raw else default


def _prompt_int_with_default(prompt: str, default: int) -> int:
    while True:
        raw = input(f'{prompt} [{default}]: ').strip()
        if not raw:
            return default
        try:
            value = int(raw)
        except ValueError:
            print('Please enter a valid integer.')
            continue
        if value <= 0:
            print('Please enter a positive integer.')
            continue
        return value


def _prompt_set_created_vm_default(vm_name: str) -> bool:
    while True:
        ans = (
            input(
                f'Set "{vm_name}" as the active default VM for folder-based commands? [y/N]: '
            )
            .strip()
            .lower()
        )
        if ans in {'', 'n', 'no'}:
            return False
        if ans in {'y', 'yes'}:
            return True
        print("Please answer 'y' or 'n'.")


def _review_vm_create_overrides_interactive(
    cfg: AgentVMConfig, path: Path
) -> AgentVMConfig:
    if not sys.stdin.isatty():
        raise RuntimeError(
            'VM create defaults require confirmation in interactive mode. '
            'Re-run with --yes.'
        )
    print(_render_vm_create_summary(cfg, path))
    while True:
        ans = input('Use these values? [Y/e/n] (e=edit): ').strip().lower()
        if ans in {'', 'y', 'yes'}:
            return cfg
        if ans in {'n', 'no'}:
            raise RuntimeError('Aborted by user.')
        if ans in {'e', 'edit'}:
            cfg.vm.name = _prompt_with_default('vm.name', cfg.vm.name)
            cfg.vm.user = _prompt_with_default('vm.user', cfg.vm.user)
            cfg.vm.cpus = _prompt_int_with_default('vm.cpus', cfg.vm.cpus)
            cfg.vm.ram_mb = _prompt_int_with_default('vm.ram_mb', cfg.vm.ram_mb)
            cfg.vm.disk_gb = _prompt_int_with_default(
                'vm.disk_gb', cfg.vm.disk_gb
            )
            cfg.network.name = _prompt_with_default(
                'network.name', cfg.network.name
            )
            cfg.network.subnet_cidr = _prompt_with_default(
                'network.subnet_cidr', cfg.network.subnet_cidr
            )
            cfg.network.gateway_ip = _prompt_with_default(
                'network.gateway_ip', cfg.network.gateway_ip
            )
            cfg.network.dhcp_start = _prompt_with_default(
                'network.dhcp_start', cfg.network.dhcp_start
            )
            cfg.network.dhcp_end = _prompt_with_default(
                'network.dhcp_end', cfg.network.dhcp_end
            )
            print('')
            print(_render_vm_create_summary(cfg, path))
            continue
        print("Please answer 'y', 'e', or 'n'.")


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
        help='Attachment mode override: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
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
        help='Attachment mode override: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
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
        help='Attachment mode: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access: Any = scfg.Value(
        '',
        help='Attachment access: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
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
            elif attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
                if not vm_running:
                    mgr = CommandManager.current()
                    with mgr.intent(
                        'Attach and reconcile shared-root mapping',
                        why='Ensure the requested host folder is exposed to the VM before the next guest session uses it.',
                        role='modify',
                    ):
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
                    attachment.mode == ATTACHMENT_MODE_SHARED_ROOT
                ),
                mirror_home=mirror_home,
            )
        print(
            f'Attached {host_src} to VM {cfg.vm.name} ({attachment.mode} mode, access={attachment.access})'
        )
        if vm_running and attachment.mode in {
            ATTACHMENT_MODE_SHARED,
            ATTACHMENT_MODE_SHARED_ROOT,
        }:
            print(f'Mounted in running VM at {attachment.guest_dst}')
        elif vm_running:
            print(f'Guest clone ready at {attachment.guest_dst}')
        elif vm_defined:
            if attachment.mode in {
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
                        cfg, ip, resolved, dry_run=False,
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
                        cfg, resolved, yes=bool(args.yes), dry_run=False,
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

        if detach_failed:
            log.error(
                'Detach cleanup was incomplete for {} on VM {}; preserving config record so detach can be retried.',
                host_src,
                cfg.vm.name,
            )
            return 2

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


@dataclass(frozen=True)
class VMUpdateDrift:
    cpus: tuple[int, int] | None = None
    ram_mb: tuple[int, int] | None = None
    disk_bytes: tuple[int, int] | None = None
    disk_path: str = ''
    notes: tuple[str, ...] = ()

    def has_changes(self) -> bool:
        return any((self.cpus, self.ram_mb, self.disk_bytes))


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


class CodeCLI(VMCodeCLI):
    """Top-level shortcut for `aivm vm code`."""


class AttachCLI(VMAttachCLI):
    """Top-level shortcut for `aivm vm attach`."""


class DetachCLI(VMDetachCLI):
    """Top-level shortcut for `aivm vm detach`."""


class SSHCLI(VMSSHCLI):
    """Top-level shortcut for `aivm vm ssh`."""


class VMModalCLI(scfg.ModalCLI):
    """VM lifecycle subcommands."""

    list = VMListCLI
    create = VMCreateCLI
    up = VMUpCLI
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


def _parse_sync_paths_arg(paths_arg: str) -> list[str]:
    items = [p.strip() for p in (paths_arg or '').split(',')]
    return [p for p in items if p]
