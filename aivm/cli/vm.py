"""CLI commands for VM lifecycle, attach/code/ssh workflows, and sync/provision."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import scriptconfig as scfg

from ..config import AgentVMConfig
from ..firewall import apply_firewall
from ..host import check_commands, host_is_debian_like, install_deps_debian
from ..net import ensure_network
from ..resource_checks import (
    vm_resource_impossible_lines,
    vm_resource_warning_lines,
)
from ..runtime import require_ssh_identity, ssh_base_args, virsh_system_cmd
from ..status import (
    probe_firewall,
    probe_network,
    probe_ssh_ready,
    probe_vm_state,
)
from ..store import (
    find_attachment,
    find_network,
    find_vm,
    load_store,
    network_users,
    remove_vm,
    save_store,
    upsert_attachment,
    upsert_network,
    upsert_vm_with_network,
)
from ..util import CmdError, ensure_dir, run_cmd, which
from ..vm import (
    attach_vm_share,
    create_or_start_vm,
    destroy_vm,
    ensure_share_mounted,
    get_ip_cached,
    provision,
    sync_settings,
    vm_has_virtiofs_shared_memory,
    vm_share_mappings,
    vm_status,
    wait_for_ip,
    wait_for_ssh,
)
from ..vm import (
    ssh_config as mk_ssh_config,
)
from ._common import (
    PreparedSession,
    _BaseCommand,
    _cfg_path,
    _confirm_external_file_update,
    _confirm_sudo_block,
    _load_cfg,
    _load_cfg_with_path,
    _record_vm,
    _resolve_cfg_for_code,
    log,
)


class VMUpCLI(_BaseCommand):
    """Create the VM if needed, or start it if already defined."""

    recreate = scfg.Value(
        False, isflag=True, help='Destroy and recreate if it exists.'
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config)
        _maybe_install_missing_host_deps(
            yes=bool(args.yes), dry_run=bool(args.dry_run)
        )
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=f"Create/start/redefine VM '{cfg.vm.name}' and libvirt resources.",
        )
        create_or_start_vm(cfg, dry_run=args.dry_run, recreate=args.recreate)
        if not args.dry_run and not args.recreate:
            _maybe_warn_hardware_drift(cfg)
        if not args.dry_run:
            _record_vm(cfg, cfg_path)
        return 0


class VMCreateCLI(_BaseCommand):
    """Create a managed VM from config-store defaults and start it."""

    vm = scfg.Value('', help='Optional VM name override.')
    force = scfg.Value(
        False,
        isflag=True,
        help='Overwrite existing VM entry and recreate VM definition if present.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMCreateCLI.main vm={} force={} dry_run={} yes={}',
            args.vm,
            bool(args.force),
            bool(args.dry_run),
            bool(args.yes),
        )
        cfg_path = _cfg_path(args.config)
        reg = load_store(cfg_path)
        if reg.defaults is None:
            log.error(
                f'No config defaults found in store: {cfg_path}. '
                'Run `aivm config init` first.'
            )
            return 1
        cfg = reg.defaults.expanded_paths()
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
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=f"Create/start VM '{cfg.vm.name}' from config defaults.",
        )
        ensure_network(cfg, recreate=False, dry_run=bool(args.dry_run))
        if cfg.firewall.enabled:
            apply_firewall(cfg, dry_run=bool(args.dry_run))
        create_or_start_vm(
            cfg,
            dry_run=bool(args.dry_run),
            recreate=bool(args.force and existing is not None),
        )
        if not args.dry_run:
            upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
            save_store(reg, cfg_path)
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

    timeout = scfg.Value(360, type=int, help='Timeout seconds.')
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _load_cfg(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Query VM networking state via virsh to resolve VM IP.',
        )
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
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _load_cfg(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Inspect VM state via virsh.',
        )
        print(vm_status(cfg))
        return 0


class VMDestroyCLI(_BaseCommand):
    """Destroy and undefine the VM (shared host directories are not deleted)."""

    vm = scfg.Value(
        '',
        position=1,
        help='Optional VM name override (positional).',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config, vm_opt=args.vm)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=(
                'Destroy/undefine VM domain and detach its libvirt disks/share mappings '
                '(host shared directories are not deleted).'
            ),
        )
        destroy_vm(cfg, dry_run=args.dry_run)
        if not args.dry_run:
            reg = load_store(cfg_path)
            remove_vm(reg, cfg.vm.name, remove_attachments=True)
            save_store(reg, cfg_path)
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
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        print(mk_ssh_config(_load_cfg(args.config)))
        return 0


class VMProvisionCLI(_BaseCommand):
    """Provision the VM with optional developer packages."""

    vm = scfg.Value(
        '',
        help='Optional VM name override.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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

    paths = scfg.Value(
        '',
        help=(
            'Optional comma-separated host paths to sync. '
            'Defaults to [sync].paths from config.'
        ),
    )
    overwrite = scfg.Value(
        True,
        isflag=True,
        help='Overwrite existing files in VM (default true).',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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

    host_src = scfg.Value(
        '.',
        position=1,
        help='Host project directory to share and open (default: current directory).',
    )
    vm = scfg.Value(
        '',
        help='VM name override.',
    )
    guest_dst = scfg.Value(
        '',
        help='Guest mount path override (default: mirrors host_src path).',
    )
    recreate_if_needed = scfg.Value(
        False,
        isflag=True,
        help='Recreate VM if existing definition lacks the requested share mapping.',
    )
    ensure_firewall = scfg.Value(
        True,
        isflag=True,
        help='Apply firewall rules when firewall.enabled=true.',
    )
    sync_settings = scfg.Value(
        False,
        isflag=True,
        help='Sync host settings files into VM before launching VS Code.',
    )
    sync_paths = scfg.Value(
        '',
        help=(
            'Optional comma-separated paths used when --sync_settings is set. '
            'Defaults to [sync].paths.'
        ),
    )
    force = scfg.Value(
        False,
        isflag=True,
        help='Force attaching folder even if already attached to a different VM.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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
                host_src=Path(args.host_src).resolve(),
                guest_dst_opt=args.guest_dst,
                recreate_if_needed=bool(args.recreate_if_needed),
                ensure_firewall_opt=bool(args.ensure_firewall),
                force=bool(args.force),
                dry_run=bool(args.dry_run),
                yes=bool(args.yes),
            )
        except RuntimeError as ex:
            # TODO: log the traceback to a trace call
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
        run_cmd(
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

    host_src = scfg.Value(
        '.',
        position=1,
        help='Host project directory to share and open (default: current directory).',
    )
    vm = scfg.Value(
        '',
        help='VM name override.',
    )
    guest_dst = scfg.Value(
        '',
        help='Guest mount path override (default: mirrors host_src path).',
    )
    recreate_if_needed = scfg.Value(
        False,
        isflag=True,
        help='Recreate VM if existing definition lacks the requested share mapping.',
    )
    ensure_firewall = scfg.Value(
        True,
        isflag=True,
        help='Apply firewall rules when firewall.enabled=true.',
    )
    force = scfg.Value(
        False,
        isflag=True,
        help='Force attaching folder even if already attached to a different VM.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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
                host_src=Path(args.host_src).resolve(),
                guest_dst_opt=args.guest_dst,
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
        run_cmd(
            [
                'ssh',
                '-t',
                *ssh_base_args(ident, strict_host_key_checking='accept-new'),
                f'{cfg.vm.user}@{ip}',
                remote_cmd,
            ],
            sudo=False,
            check=True,
            capture=False,
        )
        # FIXME: these messages don't make sense after a user completes a
        # successful ssh session, we should either say exited from whatever
        # context we entered if the ssh worked, or detect if ssh failed and
        # handle that case.
        print(f'Connected to {cfg.vm.user}@{ip} in {session.share_guest_dst}')
        if ssh_cfg_updated:
            print(f'SSH entry updated in {ssh_cfg}')
        print(f'Folder registered in {session.reg_path}')
        return 0


class VMAttachCLI(_BaseCommand):
    """Attach/register a host directory to an existing managed VM."""

    vm = scfg.Value('', help='Optional VM name override.')
    host_src = scfg.Value('.', position=1, help='Host directory to attach.')
    guest_dst = scfg.Value('', help='Guest mount path override.')
    force = scfg.Value(
        False,
        isflag=True,
        help='Allow attaching folder that is already attached to a different VM.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        log.trace(
            'VMAttachCLI.main host_src={} vm={} guest_dst={} force={} dry_run={} yes={}',
            args.host_src,
            args.vm,
            args.guest_dst,
            bool(args.force),
            bool(args.dry_run),
            bool(args.yes),
        )
        host_src = Path(args.host_src).resolve()
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
            cfg, cfg_path, host_src, args.guest_dst
        )

        if args.dry_run:
            print(
                f'DRYRUN: would attach {host_src} to VM {cfg.vm.name} at {attachment.guest_dst}'
            )
            return 0

        _record_vm(cfg, cfg_path)
        vm_running = False
        vm_defined = False
        sudo_confirmed = False
        vm_out, vm_defined_probe = probe_vm_state(cfg, use_sudo=False)
        vm_running_probe = vm_out.ok
        vm_defined = vm_defined_probe
        if not vm_defined:
            _confirm_sudo_block(
                yes=bool(args.yes),
                purpose=f"Inspect VM '{cfg.vm.name}' share mappings and attach folder if needed.",
            )
            sudo_confirmed = True
            vm_out, vm_defined_probe = probe_vm_state(cfg, use_sudo=True)
            vm_running_probe = vm_out.ok
            vm_defined = vm_defined_probe
        if vm_defined:
            if not sudo_confirmed:
                _confirm_sudo_block(
                    yes=bool(args.yes),
                    purpose=f"Inspect VM '{cfg.vm.name}' share mappings and attach folder if needed.",
                )
                sudo_confirmed = True
            vm_running = vm_running_probe is True
            mappings = vm_share_mappings(cfg)
            attachment = _align_attachment_tag_with_mappings(
                attachment, host_src, mappings
            )
            if not _attachment_has_mapping(attachment, mappings):
                attach_vm_share(
                    cfg,
                    attachment.source_dir,
                    attachment.tag,
                    dry_run=False,
                )
        reg_path = _record_attachment(
            cfg,
            cfg_path,
            host_src=host_src,
            guest_dst=attachment.guest_dst,
            tag=attachment.tag,
            force=bool(args.force),
        )
        if vm_running:
            ip = _resolve_ip_for_ssh_ops(
                cfg,
                yes=bool(args.yes),
                purpose='Query VM networking state before mounting attached share.',
            )
            ensure_share_mounted(
                cfg,
                ip,
                guest_dst=attachment.guest_dst,
                tag=attachment.tag,
                dry_run=False,
            )
        print(f'Attached {host_src} to VM {cfg.vm.name} (shared mode)')
        if vm_running:
            print(f'Mounted in running VM at {attachment.guest_dst}')
        elif vm_defined:
            print(
                f'VM {cfg.vm.name} is not running; share will mount when VM is running and attach/ssh/code is used.'
            )
        print(f'Updated config store: {cfg_path}')
        print(f'Updated attachments: {reg_path}')
        return 0


class VMListCLI(_BaseCommand):
    """List managed VM records (VM-focused view)."""

    section = scfg.Value(
        'vms',
        help='One of: all, vms, networks, folders (default: vms).',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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

    vm = scfg.Value('', help='Optional VM name override.')
    restart = scfg.Value(
        'auto',
        help='Restart policy when changes require reboot to take effect: auto, always, never.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        restart_policy = str(args.restart or 'auto').strip().lower()
        if restart_policy not in {'auto', 'always', 'never'}:
            raise RuntimeError(
                '--restart must be one of: auto, always, never'
            )
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
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=f"Update VM '{cfg.vm.name}' to match config drift.",
        )
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
    code = VMCodeCLI


@dataclass(frozen=True)
class ResolvedAttachment:
    vm_name: str
    source_dir: str
    guest_dst: str
    tag: str


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


def _bytes_to_gib(size_bytes: int) -> float:
    return float(size_bytes) / float(1024**3)


def _maybe_install_missing_host_deps(*, yes: bool, dry_run: bool) -> None:
    missing, _ = check_commands()
    if not missing:
        return
    missing_txt = ', '.join(missing)
    print(f'Missing required host dependencies: {missing_txt}')
    print('Suggested command: aivm host install_deps')
    if yes:
        print(
            '--yes was provided; skipping interactive dependency install prompt.'
        )
        return
    if dry_run:
        print(
            'DRYRUN: would prompt to install missing dependencies before VM setup.'
        )
        return
    if not host_is_debian_like():
        raise RuntimeError(
            'Host is not detected as Debian/Ubuntu. Install dependencies manually, then retry.'
        )
    if not sys.stdin.isatty():
        raise RuntimeError(
            'Missing required host dependencies in non-interactive mode. '
            'Run `aivm host install_deps` first.'
        )
    ans = input('Install missing dependencies now with apt? [Y/n]: ').strip().lower()
    do_install = ans in {'', 'y', 'yes'}
    if not do_install:
        raise RuntimeError('Aborted by user.')
    _confirm_sudo_block(
        yes=bool(yes),
        purpose='Install host dependencies with apt/libvirt tooling.',
    )
    install_deps_debian(assume_yes=True)
    missing_after, _ = check_commands()
    if missing_after:
        raise RuntimeError(
            'Required dependencies are still missing after install attempt: '
            + ', '.join(missing_after)
        )


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
        Path(cfg.paths.base_dir) / cfg.vm.name / 'images' / f'{cfg.vm.name}.qcow2'
    )
    res = run_cmd(
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
    res = run_cmd(
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
    res = run_cmd(
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
    notes: list[str] = []
    dominfo = run_cmd(
        virsh_system_cmd('dominfo', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
    )
    if dominfo.code != 0:
        _confirm_sudo_block(
            yes=bool(yes),
            purpose=f"Inspect VM '{cfg.vm.name}' state/config for update planning.",
        )
        dominfo = run_cmd(
            virsh_system_cmd('dominfo', cfg.vm.name),
            sudo=True,
            check=False,
            capture=True,
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

    state_res = run_cmd(
        virsh_system_cmd('domstate', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
    )
    if state_res.code != 0:
        state_res = run_cmd(
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
        _confirm_sudo_block(
            yes=bool(yes),
            purpose=f"Inspect VM '{cfg.vm.name}' disk/network details via libvirt.",
        )
        sudo_confirmed = True
        disk_path, disk_notes = _resolve_vm_disk_path(cfg, use_sudo=True)
    notes.extend(disk_notes)
    cur_disk, qemu_img_err = _qemu_img_virtual_size_bytes(
        disk_path, use_sudo=False
    )
    if cur_disk is None:
        if not sudo_confirmed:
            _confirm_sudo_block(
                yes=bool(yes),
                purpose=f"Inspect VM '{cfg.vm.name}' disk image size via qemu-img.",
            )
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
            _confirm_sudo_block(
                yes=bool(yes),
                purpose=f"Inspect VM '{cfg.vm.name}' disk capacity via virsh domblkinfo.",
            )
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

    xml = run_cmd(
        virsh_system_cmd('dumpxml', cfg.vm.name),
        sudo=False,
        check=False,
        capture=True,
    )
    if xml.code != 0:
        if not sudo_confirmed:
            _confirm_sudo_block(
                yes=bool(yes),
                purpose=f"Inspect VM '{cfg.vm.name}' network details via libvirt.",
            )
            sudo_confirmed = True
        xml = run_cmd(
            virsh_system_cmd('dumpxml', cfg.vm.name),
            sudo=True,
            check=False,
            capture=True,
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
            run_cmd(cmd, sudo=True, check=True, capture=True)
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
            run_cmd(max_cmd, sudo=True, check=True, capture=True)
            run_cmd(mem_cmd, sudo=True, check=True, capture=True)
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
                run_cmd(cmd, sudo=True, check=True, capture=True)
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
            ans = input(
                'A restart is needed for CPU/RAM changes to take effect now. Restart VM now? [y/N]: '
            ).strip().lower()
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
        run_cmd(cmd, sudo=True, check=True, capture=True)
        print(f'Restarted VM {cfg.vm.name}.')


def _resolve_guest_dst(host_src: Path, guest_dst_opt: str) -> str:
    guest_dst_opt = (guest_dst_opt or '').strip()
    if guest_dst_opt:
        return guest_dst_opt
    return str(host_src)


def _auto_share_tag_for_path(host_src: Path, existing_tags: set[str]) -> str:
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
    tag = (tag or '').strip()
    if tag and len(tag) <= 36:
        return tag
    return _auto_share_tag_for_path(host_src, existing_tags)


def _probe_vm_running_nonsudo(vm_name: str) -> bool | None:
    res = run_cmd(
        virsh_system_cmd('domstate', vm_name),
        sudo=False,
        check=False,
        capture=True,
    )
    if res.code != 0:
        return None
    state = (res.stdout or '').strip().lower()
    return 'running' in state


def _upsert_ssh_config_entry(
    cfg: AgentVMConfig, *, dry_run: bool = False, yes: bool = False
) -> tuple[Path, bool]:
    cfg = cfg.expanded_paths()
    ssh_dir = Path.home() / '.ssh'
    ssh_cfg = ssh_dir / 'config'
    block_name = cfg.vm.name
    new_block = (
        f'# >>> aivm:{block_name} >>>\n'
        f'{mk_ssh_config(cfg).rstrip()}\n'
        f'# <<< aivm:{block_name} <<<\n'
    )
    if dry_run:
        log.info(
            'DRYRUN: update SSH config block for host {} in {}',
            block_name,
            ssh_cfg,
        )
        return ssh_cfg, False
    ensure_dir(ssh_dir)
    existing = ssh_cfg.read_text(encoding='utf-8') if ssh_cfg.exists() else ''
    pattern = re.compile(
        rf'(?ms)^# >>> aivm:{re.escape(block_name)} >>>\n.*?^# <<< aivm:{re.escape(block_name)} <<<\n?'
    )
    if pattern.search(existing):
        updated = pattern.sub(new_block, existing)
    else:
        sep = '' if not existing or existing.endswith('\n') else '\n'
        updated = f'{existing}{sep}{new_block}'
    if updated == existing:
        log.debug(
            "SSH config entry for host '{}' already up to date in {}",
            block_name,
            ssh_cfg,
        )
        return ssh_cfg, False
    _confirm_external_file_update(
        yes=bool(yes),
        path=ssh_cfg,
        purpose=f"Update SSH config entry for host '{block_name}'.",
    )
    log.info('Writing SSH config entry to {}', ssh_cfg)
    ssh_cfg.write_text(updated, encoding='utf-8')
    return ssh_cfg, True


def _parse_sync_paths_arg(paths_arg: str) -> list[str]:
    items = [p.strip() for p in (paths_arg or '').split(',')]
    return [p for p in items if p]


def _missing_virtiofs_dir_from_error(ex: Exception) -> str | None:
    text = str(ex)
    if isinstance(ex, CmdError):
        text = f'{ex.result.stderr}\n{ex.result.stdout}\n{text}'
    m = re.search(r"virtiofs export directory '([^']+)' does not exist", text)
    return m.group(1) if m else None


def _parse_dominfo_hardware(dominfo_text: str) -> tuple[int | None, int | None]:
    cpus = None
    max_mem_mib = None
    for line in (dominfo_text or '').splitlines():
        if ':' not in line:
            continue
        key, val = [x.strip() for x in line.split(':', 1)]
        low = key.lower()
        if low in {'cpu(s)', 'cpus'}:
            m = re.search(r'(\d+)', val)
            if m:
                cpus = int(m.group(1))
        elif low.startswith('max memory'):
            m = re.search(r'(\d+)', val)
            if m:
                max_mem_mib = int(m.group(1)) // 1024
    return cpus, max_mem_mib


def _vm_hardware_drift(cfg: AgentVMConfig) -> dict[str, tuple[int, int]]:
    res = run_cmd(
        virsh_system_cmd('dominfo', cfg.vm.name),
        sudo=True,
        check=False,
        capture=True,
    )
    if res.code != 0:
        return {}
    cur_cpus, cur_mem_mib = _parse_dominfo_hardware(res.stdout)
    drift: dict[str, tuple[int, int]] = {}
    if cur_cpus is not None and cur_cpus != int(cfg.vm.cpus):
        drift['cpus'] = (cur_cpus, int(cfg.vm.cpus))
    if cur_mem_mib is not None and cur_mem_mib != int(cfg.vm.ram_mb):
        drift['ram_mb'] = (cur_mem_mib, int(cfg.vm.ram_mb))
    return drift


def _maybe_warn_hardware_drift(cfg: AgentVMConfig) -> None:
    drift = _vm_hardware_drift(cfg)
    if not drift:
        return
    print(
        f'⚠️ VM {cfg.vm.name} is already defined and differs from config for hardware settings.'
    )
    if 'cpus' in drift:
        cur, want = drift['cpus']
        print(f'  - cpus: current={cur} desired={want}')
    if 'ram_mb' in drift:
        cur, want = drift['ram_mb']
        print(f'  - ram_mb: current={cur} desired={want}')
    print('Suggested non-destructive apply commands:')
    print(f'  sudo virsh shutdown {cfg.vm.name}   # if VM is running')
    if 'cpus' in drift:
        _, want = drift['cpus']
        print(f'  sudo virsh setvcpus {cfg.vm.name} {want} --config')
    if 'ram_mb' in drift:
        _, want = drift['ram_mb']
        kib = int(want) * 1024
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
    _confirm_sudo_block(
        yes=bool(yes),
        purpose=purpose,
    )
    ip = wait_for_ip(cfg, timeout_s=360, dry_run=False)
    wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
    return ip


def _record_attachment(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    host_src: Path,
    guest_dst: str,
    tag: str,
    force: bool = False,
) -> Path:
    reg = load_store(cfg_path)
    upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
    upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
    upsert_attachment(
        reg,
        host_path=host_src,
        vm_name=cfg.vm.name,
        mode='shared',
        guest_dst=guest_dst,
        tag=tag,
        force=force,
    )
    return save_store(reg, cfg_path)


def _resolve_attachment(
    cfg: AgentVMConfig,
    cfg_path: Path,
    host_src: Path,
    guest_dst_opt: str,
) -> ResolvedAttachment:
    source_dir = str(host_src.resolve())
    guest_dst = _resolve_guest_dst(host_src, guest_dst_opt)
    tag = _ensure_share_tag_len('', host_src, set())
    reg = load_store(cfg_path)
    att = find_attachment(reg, host_src)
    if att is not None and att.vm_name == cfg.vm.name:
        if not guest_dst_opt and att.guest_dst:
            guest_dst = att.guest_dst
        if att.tag:
            tag = att.tag
    return ResolvedAttachment(
        vm_name=cfg.vm.name,
        source_dir=source_dir,
        guest_dst=guest_dst,
        tag=tag,
    )


def _attachment_has_mapping(
    att: ResolvedAttachment, mappings: list[tuple[str, str]]
) -> bool:
    return any(
        src == att.source_dir and tag == att.tag for src, tag in mappings
    )


def _align_attachment_tag_with_mappings(
    att: ResolvedAttachment, host_src: Path, mappings: list[tuple[str, str]]
) -> ResolvedAttachment:
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


def _reconcile_attached_vm(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    *,
    policy: ReconcilePolicy,
) -> ReconcileResult:
    cached_ip = get_ip_cached(cfg) if not policy.dry_run else None
    cached_ssh_ok = False
    if cached_ip:
        cached_ssh_ok = bool(probe_ssh_ready(cfg, cached_ip).ok)
    vm_running_probe = (
        _probe_vm_running_nonsudo(cfg.vm.name) if not policy.dry_run else None
    )

    net_probe = probe_network(cfg, use_sudo=False).ok
    need_network_ensure = (net_probe is False) and (not cached_ssh_ok)
    if need_network_ensure:
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Ensure libvirt network '{cfg.network.name}'.",
        )
        ensure_network(cfg, recreate=False, dry_run=policy.dry_run)

    need_firewall_apply = False
    if (
        cfg.firewall.enabled
        and policy.ensure_firewall_opt
        and (not cached_ssh_ok)
    ):
        fw_probe = probe_firewall(cfg, use_sudo=False).ok
        if fw_probe is None:
            _confirm_sudo_block(
                yes=bool(policy.yes),
                purpose=f"Inspect firewall table '{cfg.firewall.table}'.",
            )
            fw_probe = probe_firewall(cfg, use_sudo=True).ok
        need_firewall_apply = fw_probe is not True
    if need_firewall_apply:
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Apply/update firewall table '{cfg.firewall.table}'.",
        )
        apply_firewall(cfg, dry_run=policy.dry_run)

    recreate = False
    vm_running = vm_running_probe
    mappings: list[tuple[str, str]] = []
    has_share = False
    if vm_running is None and cached_ssh_ok:
        vm_running = True
    if not policy.dry_run and vm_running is True:
        mappings = vm_share_mappings(cfg, use_sudo=False)
        attachment = _align_attachment_tag_with_mappings(
            attachment, host_src, mappings
        )
        has_share = _attachment_has_mapping(attachment, mappings)

    need_vm_start_or_create = policy.dry_run or (vm_running is not True)
    if need_vm_start_or_create:
        _maybe_install_missing_host_deps(
            yes=bool(policy.yes), dry_run=bool(policy.dry_run)
        )
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Create/start VM '{cfg.vm.name}' or update VM definition.",
        )
        try:
            create_or_start_vm(
                cfg,
                dry_run=policy.dry_run,
                recreate=False,
                share_source_dir=attachment.source_dir,
                share_tag=attachment.tag,
            )
        except Exception as ex:
            missing_virtiofs_dir = _missing_virtiofs_dir_from_error(ex)
            if not policy.dry_run and missing_virtiofs_dir is not None:
                log.warning(
                    'VM {} has stale virtiofs source {}; recreating VM definition',
                    cfg.vm.name,
                    missing_virtiofs_dir,
                )
                _confirm_sudo_block(
                    yes=bool(policy.yes),
                    purpose=f"Recreate VM '{cfg.vm.name}' to repair stale virtiofs mapping.",
                )
                create_or_start_vm(
                    cfg,
                    dry_run=False,
                    recreate=True,
                    share_source_dir=attachment.source_dir,
                    share_tag=attachment.tag,
                )
            else:
                raise
        vm_running = (
            True if policy.dry_run else _probe_vm_running_nonsudo(cfg.vm.name)
        )
        if not policy.dry_run and vm_running is True:
            mappings = vm_share_mappings(cfg, use_sudo=False)
            attachment = _align_attachment_tag_with_mappings(
                attachment, host_src, mappings
            )
            has_share = _attachment_has_mapping(attachment, mappings)

    if not policy.dry_run and vm_running is True and not has_share:
        vm_has_shared_mem = vm_has_virtiofs_shared_memory(cfg, use_sudo=False)
        if vm_has_shared_mem is False and not policy.recreate_if_needed:
            raise RuntimeError(
                'Existing VM cannot accept virtiofs attachments because its domain '
                'definition lacks required shared-memory backing (memfd/shared).\n'
                f'VM: {cfg.vm.name}\n'
                f'Requested: source={attachment.source_dir} tag={attachment.tag} '
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
                _confirm_sudo_block(
                    yes=bool(policy.yes),
                    purpose=f"Attach this folder to existing VM '{cfg.vm.name}'.",
                )
                attach_vm_share(
                    cfg,
                    attachment.source_dir,
                    attachment.tag,
                    dry_run=False,
                )
                has_share = True
            except Exception as ex:
                current_maps = mappings or vm_share_mappings(
                    cfg, use_sudo=False
                )
                requested_tag = attachment.tag
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
                    f'Requested: source={attachment.source_dir} tag={requested_tag} guest_dst={attachment.guest_dst}\n'
                    'Current VM filesystem mappings:\n'
                    f'{found}\n'
                    f'Live attach error: {ex}\n'
                    'Next steps:\n'
                    '  - Re-run with --recreate_if_needed to rebuild the VM definition with the new share.\n'
                    '  - Or use a VM already defined with this share mapping.'
                )

    if recreate:
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Recreate VM '{cfg.vm.name}' to apply new share mapping.",
        )
        create_or_start_vm(
            cfg,
            dry_run=policy.dry_run,
            recreate=True,
            share_source_dir=attachment.source_dir,
            share_tag=attachment.tag,
        )

    return ReconcileResult(
        attachment=attachment,
        cached_ip=cached_ip,
        cached_ssh_ok=cached_ssh_ok,
    )


def _prepare_attached_session(
    *,
    config_opt: str | None,
    vm_opt: str,
    host_src: Path,
    guest_dst_opt: str,
    recreate_if_needed: bool,
    ensure_firewall_opt: bool,
    force: bool,
    dry_run: bool,
    yes: bool,
) -> PreparedSession:
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
        if not yes:
            if not sys.stdin.isatty():
                raise RuntimeError(
                    'No managed VM found for this folder. Re-run with --yes to initialize defaults and create one automatically.'
                ) from ex
            print('No managed VM found for this folder.')
            ans = input(
                'Run `aivm config init` and `aivm vm create` now? [Y/n]: '
            ).strip().lower()
            if ans not in {'', 'y', 'yes'}:
                raise RuntimeError('Aborted by user.') from ex
        from .config import InitCLI

        InitCLI.main(
            argv=False,
            config=config_opt,
            yes=True,
            defaults=True,
            force=False,
        )
        VMCreateCLI.main(
            argv=False,
            config=config_opt,
            vm=vm_opt,
            yes=True,
            dry_run=bool(dry_run),
            force=False,
        )
        cfg, cfg_path = _resolve_cfg_for_code(
            config_opt=config_opt,
            vm_opt=vm_opt,
            host_src=host_src,
        )

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

    if dry_run:
        return PreparedSession(
            cfg=cfg,
            cfg_path=cfg_path,
            host_src=host_src,
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
        _confirm_sudo_block(
            yes=bool(yes),
            purpose='Query VM network state via virsh to discover VM IP.',
        )
        ip = wait_for_ip(cfg, timeout_s=360, dry_run=False)
        wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
    if not ip:
        raise RuntimeError('Could not resolve VM IP address.')
    ensure_share_mounted(
        cfg,
        ip,
        guest_dst=attachment.guest_dst,
        tag=attachment.tag,
        dry_run=False,
    )
    return PreparedSession(
        cfg=cfg,
        cfg_path=cfg_path,
        host_src=host_src,
        share_source_dir=attachment.source_dir,
        share_tag=attachment.tag,
        share_guest_dst=attachment.guest_dst,
        ip=ip,
        reg_path=reg_path,
        meta_path=None,
    )
