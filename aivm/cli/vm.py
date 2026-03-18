"""CLI commands for VM lifecycle, attach/code/ssh workflows, and sync/provision."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import sys
import xml.etree.ElementTree as ET
from copy import deepcopy
from dataclasses import asdict, dataclass, replace
from pathlib import Path, PurePosixPath

import scriptconfig as scfg

from ..commands import CommandManager, IntentScope, PlanScope
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
    find_attachment_for_vm,
    find_attachments_for_vm,
    find_network,
    find_vm,
    load_store,
    materialize_vm_cfg,
    network_users,
    remove_attachment,
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
    detach_vm_share,
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

ATTACHMENT_MODE_SHARED = 'shared'
ATTACHMENT_MODE_SHARED_ROOT = 'shared-root'
ATTACHMENT_MODE_GIT = 'git'
ATTACHMENT_MODES = {
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    ATTACHMENT_MODE_GIT,
}
ATTACHMENT_ACCESS_RW = 'rw'
ATTACHMENT_ACCESS_RO = 'ro'
ATTACHMENT_ACCESS_MODES = {
    ATTACHMENT_ACCESS_RW,
    ATTACHMENT_ACCESS_RO,
}
SHARED_ROOT_VIRTIOFS_TAG = 'aivm-shared-root'
SHARED_ROOT_GUEST_MOUNT_ROOT = '/mnt/aivm-shared'


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
    set_default = scfg.Value(
        False,
        isflag=True,
        help='Set the created VM as the active default VM.',
    )
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
            action='read',
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
            action='read',
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
    mode = scfg.Value(
        '',
        help='Attachment mode override: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
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
        help='Deprecated no-op; multiple VMs may attach the same folder.',
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
    mode = scfg.Value(
        '',
        help='Attachment mode override: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access = scfg.Value(
        '',
        help='Attachment access override: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
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
        help='Deprecated no-op; multiple VMs may attach the same folder.',
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
        ssh_result = run_cmd(
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

    vm = scfg.Value('', help='Optional VM name override.')
    host_src = scfg.Value('.', position=1, help='Host directory to attach.')
    guest_dst = scfg.Value('', help='Guest mount path override.')
    mode = scfg.Value(
        '',
        help='Attachment mode: shared, shared-root, or git (default: saved mode or shared-root; mode changes require detach+reattach).',
    )
    access = scfg.Value(
        '',
        help='Attachment access: rw or ro (default: saved access or rw). ro is currently supported only for shared mode.',
    )
    force = scfg.Value(
        False,
        isflag=True,
        help='Deprecated no-op; multiple VMs may attach the same folder.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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
            cfg, cfg_path, host_src, args.guest_dst, args.mode, args.access
        )

        if args.dry_run:
            print(
                f'DRYRUN: would attach {host_src} to VM {cfg.vm.name} at {attachment.guest_dst} ({attachment.mode} mode, access={attachment.access})'
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
                action='read',
            )
            sudo_confirmed = True
            vm_out, vm_defined_probe = probe_vm_state(cfg, use_sudo=True)
            vm_running_probe = vm_out.ok
            vm_defined = vm_defined_probe
        if vm_defined:
            vm_running = vm_running_probe is True
            if attachment.mode == ATTACHMENT_MODE_SHARED:
                if not sudo_confirmed:
                    _confirm_sudo_block(
                        yes=bool(args.yes),
                        purpose=f"Inspect VM '{cfg.vm.name}' share mappings and attach folder if needed.",
                        action='read',
                    )
                    sudo_confirmed = True
                mappings = vm_share_mappings(cfg)
                attachment = _align_attachment_tag_with_mappings(
                    attachment, host_src, mappings
                )
                if not _attachment_has_mapping(attachment, mappings):
                    _confirm_sudo_block(
                        yes=bool(args.yes),
                        purpose=f"Attach this folder to existing VM '{cfg.vm.name}'.",
                    )
                    attach_vm_share(
                        cfg,
                        attachment.source_dir,
                        attachment.tag,
                        dry_run=False,
                    )
            elif attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
                if not vm_running:
                    with IntentScope(
                        CommandManager.current(),
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

    vm = scfg.Value('', help='Optional VM name override.')
    host_src = scfg.Value('.', position=1, help='Host directory to detach.')
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
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
                f'No attachment found for {host_src} on VM {cfg.vm.name}. Nothing to do.'
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
            _confirm_sudo_block(
                yes=bool(args.yes),
                purpose=f"Inspect VM '{cfg.vm.name}' share mappings for detach.",
                action='read',
            )
            vm_out, vm_defined = probe_vm_state(cfg, use_sudo=True)
            vm_defined_probe = vm_defined
        vm_running = bool(vm_out.ok)
        mode = _normalize_attachment_mode(att.mode)
        resolved = ResolvedAttachment(
            vm_name=cfg.vm.name,
            mode=mode,
            access=_normalize_attachment_access(getattr(att, 'access', '')),
            source_dir=str(host_src),
            guest_dst=att.guest_dst or str(host_src),
            tag=att.tag,
        )

        detached_share = False
        detached_shared_root_host_bind = False
        detached_shared_root_guest_bind = False
        if (
            mode == ATTACHMENT_MODE_SHARED
            and vm_defined_probe is True
            and att.tag
        ):
            _confirm_sudo_block(
                yes=bool(args.yes),
                purpose=f"Detach shared folder mapping from VM '{cfg.vm.name}'.",
            )
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
                    log.warning(
                        'Could not detach shared-root host bind mount for VM {} source={} guest_dst={} token={}: {}',
                        cfg.vm.name,
                        resolved.source_dir,
                        resolved.guest_dst,
                        resolved.tag,
                        ex,
                    )
            else:
                log.warning(
                    'Skipping shared-root host bind cleanup for VM {} source={} because attachment token is missing.',
                    cfg.vm.name,
                    resolved.source_dir,
                )

        removed = remove_attachment(
            reg, host_path=host_src, vm_name=cfg.vm.name
        )
        if removed:
            save_store(reg, cfg_path)

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


@dataclass(frozen=True)
class ResolvedAttachment:
    vm_name: str
    mode: str = ATTACHMENT_MODE_SHARED
    access: str = ATTACHMENT_ACCESS_RW
    source_dir: str = ''
    guest_dst: str = ''
    tag: str = ''


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


def _bytes_to_gib(size_bytes: int) -> float:
    return float(size_bytes) / float(1024**3)


def _maybe_install_missing_host_deps(*, yes: bool, dry_run: bool) -> None:
    """Best-effort host dependency gate before VM lifecycle operations.

    We keep this prompt local to workflows that actively create/start/reconcile
    VMs so users see missing prerequisites at the point of need.
    """
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
    ans = (
        input('Install missing dependencies now with apt? [Y/n]: ')
        .strip()
        .lower()
    )
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
        Path(cfg.paths.base_dir)
        / cfg.vm.name
        / 'images'
        / f'{cfg.vm.name}.qcow2'
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
    """Compute editable drift between config and live libvirt VM state.

    The update flow is intentionally conservative:
    * prefer non-sudo probes first,
    * escalate to sudo only when required,
    * gather diagnostics in ``notes`` instead of failing hard when a probe is
      inconclusive (for example qemu-img lock contention on running VMs).
    """
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
            action='read',
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
            action='read',
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
                action='read',
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
                action='read',
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
                action='read',
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
        run_cmd(cmd, sudo=True, check=True, capture=True)
        print(f'Restarted VM {cfg.vm.name}.')


def _resolve_guest_dst(host_src: Path, guest_dst_opt: str) -> str:
    guest_dst_opt = (guest_dst_opt or '').strip()
    if guest_dst_opt:
        return guest_dst_opt
    return str(host_src)


def _default_git_guest_dst(cfg: AgentVMConfig, host_src: Path) -> str:
    """Choose a writable guest path for git-mode attachments.

    Shared mode can mirror host absolute paths because mount setup runs with
    guest sudo. Git mode operates as the VM user, so default under /home/<user>.
    """
    guest_home = PurePosixPath('/home') / cfg.vm.user
    host_abs = host_src.resolve()
    rel: Path
    try:
        rel = host_abs.relative_to(Path.home().resolve())
    except Exception:
        rel = Path('workspaces') / (host_abs.name or 'project')
    if not rel.parts:
        rel = Path('workspaces') / (host_abs.name or 'project')
    return str(guest_home.joinpath(*rel.parts))


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
    with IntentScope(
        mgr,
        'Prepare shared-root mapping',
        why='libvirt needs the shared-root export directory to exist before the VM definition can use it.',
        role='modify',
    ):
        with PlanScope(
            mgr,
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


def _ensure_shared_root_host_bind(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    yes: bool,
    dry_run: bool,
    allow_disruptive_rebind: bool = True,
) -> Path:
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
    with PlanScope(
        mgr,
        'Inspect shared-root host bind state',
        why='Determine whether the VM-specific bind target already points at the requested host folder.',
        approval_scope=f'shared-root-host-inspect:{cfg.vm.name}:{attachment.tag}',
    ):
        probe = mgr.submit(
            ['findmnt', '-n', '-o', 'SOURCE', '--target', str(target)],
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary='Inspect current source for host bind target',
            detail=f'target={target}',
        ).result()
    mounted_source = (probe.stdout or '').strip().splitlines()
    current = mounted_source[0] if mounted_source else ''
    is_mountpoint = probe.code == 0 and bool(current)
    if is_mountpoint:
        # findmnt SOURCE for bind mounts may be:
        # 1) "/src/path[/subpath]" or
        # 2) "/dev/sdXN[/src/path]".
        # Accept either the raw SOURCE, bracket suffix, or prefix path.
        for candidate in _mount_source_compare_candidates(current):
            try:
                candidate_abs = str(Path(candidate).resolve())
            except Exception:
                candidate_abs = candidate
            if candidate_abs == source_dir:
                return target
        if not allow_disruptive_rebind:
            raise RuntimeError(
                'Refusing to replace existing shared-root host bind mount during automatic restore '
                f'(target={target}, expected_source={source_dir}, actual_source={current or "unknown"}). '
                'Use an explicit attach/detach command to reconcile this mount.'
            )
    with PlanScope(
        mgr,
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
                f'msg="$(umount {shlex.quote(str(target))} 2>&1 || true)"; '
                'if [ -n "$msg" ]; then '
                'msg_lc="$(printf "%s" "$msg" | tr "[:upper:]" "[:lower:]")"; '
                'case "$msg_lc" in '
                '*"target is busy"*|*"transport endpoint is not connected"*) '
                f'umount -l {shlex.quote(str(target))} ;; '
                '*) printf "%s\\n" "$msg" >&2; exit 1 ;; '
                'esac; '
                'fi; '
                f'mount --bind {shlex.quote(source_dir)} {shlex.quote(str(target))}'
            )
            mgr.submit(
                ['bash', '-lc', repair_script],
                sudo=True,
                role='modify',
                summary='Replace stale host bind target with requested source',
                detail=(
                    f'target={target} expected_source={source_dir} '
                    f'actual_source={current or "unknown"}'
                ),
            )
        else:
            mgr.submit(
                ['mount', '--bind', source_dir, str(target)],
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
    del yes
    mgr = CommandManager.current()
    source = str(_shared_root_host_dir(cfg))
    tag = SHARED_ROOT_VIRTIOFS_TAG
    with PlanScope(
        mgr,
        'Inspect shared-root VM mapping',
        why='Check whether the current VM definition already includes the shared-root virtiofs device.',
        approval_scope=f'shared-root-vm-inspect:{cfg.vm.name}',
    ):
        mappings = vm_share_mappings(cfg, use_sudo=False)
    if any(src == source and t == tag for src, t in mappings):
        return
    with PlanScope(
        mgr,
        'Inspect shared-root VM mapping with libvirt privileges',
        why='Some hosts require privileged libvirt access to read the effective filesystem mapping state.',
        approval_scope=f'shared-root-vm-inspect-sudo:{cfg.vm.name}',
    ):
        mappings = vm_share_mappings(cfg, use_sudo=True)
    if any(src == source and t == tag for src, t in mappings):
        return
    with PlanScope(
        mgr,
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
        log.info('DRYRUN: {}', ' '.join(shlex.quote(c) for c in cmd))
        return
    mount_cmd = _shared_root_guest_mount_cmd(
        cfg,
        ip,
        read_only=(attachment.access == ATTACHMENT_ACCESS_RO),
    )
    with PlanScope(
        mgr,
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
            'Failed to bind-mount shared-root attachment inside guest.\n'
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
    _confirm_sudo_block(
        yes=bool(yes),
        purpose=f"Detach host bind mount for shared-root attachment on VM '{cfg.vm.name}' (target={target}).",
    )
    if dry_run:
        print(f'DRYRUN: would unmount shared-root host bind target {target}')
        return
    mounted = (
        run_cmd(
            ['mountpoint', '-q', str(target)],
            sudo=True,
            sudo_action='read',
            check=False,
            capture=True,
        ).code
        == 0
    )
    if mounted:
        run_cmd(['umount', str(target)], sudo=True, check=True, capture=True)
    run_cmd(
        ['rmdir', str(target)],
        sudo=True,
        sudo_action='modify',
        check=False,
        capture=True,
    )


def _detach_shared_root_guest_bind(
    cfg: AgentVMConfig,
    ip: str,
    attachment: ResolvedAttachment,
    *,
    dry_run: bool,
) -> None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    script = (
        'set -euo pipefail; '
        f'if mountpoint -q {shlex.quote(attachment.guest_dst)}; then '
        f'sudo umount {shlex.quote(attachment.guest_dst)}; '
        'fi'
    )
    cmd = [
        'ssh',
        *ssh_base_args(ident, strict_host_key_checking='accept-new'),
        f'{cfg.vm.user}@{ip}',
        script,
    ]
    if dry_run:
        log.info('DRYRUN: {}', ' '.join(shlex.quote(c) for c in cmd))
        return
    run_cmd(cmd, sudo=False, check=False, capture=True)


def _ensure_attachment_available_in_guest(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    ip: str,
    *,
    yes: bool,
    dry_run: bool,
    ensure_shared_root_host_side: bool,
    allow_disruptive_shared_root_rebind: bool = True,
) -> None:
    """Make an attachment available at its guest destination for a running VM."""
    mgr = CommandManager.current()
    if attachment.mode == ATTACHMENT_MODE_SHARED:
        ensure_share_mounted(
            cfg,
            ip,
            guest_dst=attachment.guest_dst,
            tag=attachment.tag,
            read_only=(attachment.access == ATTACHMENT_ACCESS_RO),
            dry_run=dry_run,
        )
        return
    if attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
        with IntentScope(
            mgr,
            'Attach and reconcile shared-root mapping',
            why='Ensure the requested host folder is exposed to the VM and bound to the requested guest destination.',
            role='modify',
        ):
            if ensure_shared_root_host_side:
                _ensure_shared_root_host_bind(
                    cfg,
                    attachment,
                    yes=bool(yes),
                    dry_run=dry_run,
                    allow_disruptive_rebind=allow_disruptive_shared_root_rebind,
                )
                _ensure_shared_root_vm_mapping(
                    cfg,
                    yes=bool(yes),
                    dry_run=dry_run,
                    vm_running=True,
                )
            _ensure_shared_root_guest_bind(
                cfg,
                ip,
                attachment,
                dry_run=dry_run,
            )
        return
    _ensure_git_clone_attachment(
        cfg,
        host_src,
        attachment,
        ip,
        yes=bool(yes),
        dry_run=dry_run,
    )


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
        action='read',
    )
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
    return save_store(reg, cfg_path)


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
        saved_access = _normalize_attachment_access(
            getattr(att, 'access', '')
        )
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
    # Git mode should default to a guest-user writable path rather than host
    # absolute path mirroring, which may point to an unwritable guest location.
    if mode == ATTACHMENT_MODE_GIT and not guest_dst_opt:
        source_abs = str(host_src.resolve())
        if att is None or not att.guest_dst:
            guest_dst = _default_git_guest_dst(cfg, host_src)
        elif att.guest_dst.strip() == source_abs:
            migrated = _default_git_guest_dst(cfg, host_src)
            if migrated != att.guest_dst.strip():
                log.info(
                    'Auto-migrating git attachment guest destination from {} to {} for VM {}',
                    att.guest_dst,
                    migrated,
                    cfg.vm.name,
                )
                guest_dst = migrated
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


def _saved_vm_attachments(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    primary_attachment: ResolvedAttachment | None = None,
) -> list[ResolvedAttachment]:
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
) -> None:
    saved_attachments = _saved_vm_attachments(
        cfg,
        cfg_path,
        primary_attachment=primary_attachment,
    )
    if len(saved_attachments) <= 1:
        return

    secondary_attachments = saved_attachments[1:]
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
            aligned = _align_attachment_tag_with_mappings(
                att, Path(att.source_dir), mappings
            )
            if not _attachment_has_mapping(aligned, mappings):
                needs_privileged_probe = True
                break

        if needs_privileged_probe:
            _confirm_sudo_block(
                yes=bool(yes),
                purpose=f"Inspect and restore saved folder attachments for VM '{cfg.vm.name}'.",
                action='read',
            )
            mappings = vm_share_mappings(cfg, use_sudo=True)

    restored = 0
    for att in secondary_attachments:
        if att.mode == ATTACHMENT_MODE_SHARED_ROOT:
            aligned = att
            try:
                _ensure_attachment_available_in_guest(
                    cfg,
                    Path(aligned.source_dir),
                    aligned,
                    ip,
                    yes=bool(yes),
                    dry_run=False,
                    ensure_shared_root_host_side=True,
                    allow_disruptive_shared_root_rebind=False,
                )
                _record_attachment(
                    cfg,
                    cfg_path,
                    host_src=Path(aligned.source_dir),
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

        aligned = _align_attachment_tag_with_mappings(
            att, Path(att.source_dir), mappings
        )
        if not _attachment_has_mapping(aligned, mappings):
            _confirm_sudo_block(
                yes=bool(yes),
                purpose=f"Restore saved shared folder attachment on VM '{cfg.vm.name}'.",
            )
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
            aligned = _align_attachment_tag_with_mappings(
                aligned, Path(aligned.source_dir), mappings
            )
            if not _attachment_has_mapping(aligned, mappings):
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
            _record_attachment(
                cfg,
                cfg_path,
                host_src=Path(aligned.source_dir),
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


def _virtiofs_mapping_for_attachment(
    cfg: AgentVMConfig, attachment: ResolvedAttachment
) -> tuple[str, str] | None:
    if attachment.mode == ATTACHMENT_MODE_SHARED:
        return attachment.source_dir, attachment.tag
    if attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
        return str(_shared_root_host_dir(cfg)), SHARED_ROOT_VIRTIOFS_TAG
    return None


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
                action='read',
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
            attachment = _align_attachment_tag_with_mappings(
                attachment, host_src, mappings
            )
            virtiofs_mapping = _virtiofs_mapping_for_attachment(cfg, attachment)
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
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Create/start VM '{cfg.vm.name}' or update VM definition.",
        )
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
                _confirm_sudo_block(
                    yes=bool(policy.yes),
                    purpose=f"Recreate VM '{cfg.vm.name}' to repair stale virtiofs mapping.",
                )
                create_or_start_vm(
                    cfg,
                    dry_run=False,
                    recreate=True,
                    share_source_dir=(
                        virtiofs_mapping[0] if virtiofs_mapping else ''
                    ),
                    share_tag=(virtiofs_mapping[1] if virtiofs_mapping else ''),
                )
            else:
                raise
        vm_running = (
            True if policy.dry_run else _probe_vm_running_nonsudo(cfg.vm.name)
        )
        if (
            virtiofs_mapping is not None
            and not policy.dry_run
            and vm_running is True
        ):
            mappings = vm_share_mappings(cfg, use_sudo=False)
            if attachment.mode == ATTACHMENT_MODE_SHARED:
                attachment = _align_attachment_tag_with_mappings(
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

    if (
        virtiofs_mapping is not None
        and not policy.dry_run
        and vm_running is True
        and not has_share
    ):
        vm_has_shared_mem = vm_has_virtiofs_shared_memory(cfg, use_sudo=False)
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
                    with IntentScope(
                        CommandManager.current(),
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
                    _confirm_sudo_block(
                        yes=bool(policy.yes),
                        purpose=(
                            f"Attach this folder to existing VM '{cfg.vm.name}'."
                        ),
                    )
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
        _confirm_sudo_block(
            yes=bool(policy.yes),
            purpose=f"Recreate VM '{cfg.vm.name}' to apply new share mapping.",
        )
        create_or_start_vm(
            cfg,
            dry_run=policy.dry_run,
            recreate=True,
            share_source_dir=(virtiofs_mapping[0] if virtiofs_mapping else ''),
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
            tail = msg[len(prefix) :]
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
            from .config import InitCLI

            InitCLI.main(
                argv=False,
                config=config_opt,
                yes=bool(yes),
                defaults=bool(yes),
                force=False,
            )
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
        _confirm_sudo_block(
            yes=bool(yes),
            purpose='Query VM network state via virsh to discover VM IP.',
            action='read',
        )
        ip = wait_for_ip(cfg, timeout_s=360, dry_run=False)
        wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
    if not ip:
        raise RuntimeError('Could not resolve VM IP address.')
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
        )
        _restore_saved_vm_attachments(
            cfg,
            cfg_path,
            ip=ip,
            primary_attachment=attachment,
            yes=bool(yes),
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
        _restore_saved_vm_attachments(
            cfg,
            cfg_path,
            ip=ip,
            primary_attachment=None,
            yes=bool(yes),
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


def _normalize_attachment_mode(mode: str) -> str:
    raw = str(mode or '').strip().lower()
    if not raw:
        return ATTACHMENT_MODE_SHARED_ROOT
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
    return resolved


def _normalize_attachment_access(access: str) -> str:
    raw = str(access or '').strip().lower()
    if not raw:
        return ATTACHMENT_ACCESS_RW
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
    return resolved


def _git_repo_context(host_src: Path) -> tuple[Path, Path]:
    probe = run_cmd(
        ['git', '-C', str(host_src), 'rev-parse', '--show-toplevel'],
        sudo=False,
        check=False,
        capture=True,
    )
    if probe.code != 0:
        raise RuntimeError(
            f'Git attachment mode requires a Git worktree: {host_src}'
        )
    repo_root = Path((probe.stdout or '').strip()).resolve()
    rel = host_src.resolve().relative_to(repo_root)
    return repo_root, rel


def _guest_repo_root_for_attachment(
    attachment: ResolvedAttachment, repo_rel: Path
) -> str:
    guest_target = PurePosixPath(attachment.guest_dst)
    guest_root = guest_target
    for _ in repo_rel.parts:
        guest_root = guest_root.parent
    return str(guest_root)


def _git_attachment_remote_name(cfg: AgentVMConfig, repo_root: Path) -> str:
    stem = re.sub(r'[^a-z0-9]+', '-', cfg.vm.name.lower()).strip('-') or 'vm'
    digest = hashlib.sha1(str(repo_root).encode('utf-8')).hexdigest()[:8]
    return f'aivm-{stem}-{digest}'


def _upsert_host_git_remote(
    repo_root: Path,
    *,
    remote_name: str,
    remote_url: str,
    yes: bool,
) -> tuple[Path, bool]:
    """Ensure a host Git remote exists with the requested URL.

    "Upsert" means insert+update: update if the remote already exists,
    otherwise register it.  Returns ``(git_config_path, changed)`` where
    ``changed`` is ``True`` only when this function adds or updates the remote
    entry.
    """
    git_dir_probe = run_cmd(
        [
            'git',
            '-C',
            str(repo_root),
            'rev-parse',
            '--path-format=absolute',
            '--git-common-dir',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if git_dir_probe.code != 0:
        msg = (git_dir_probe.stderr or git_dir_probe.stdout).strip()
        raise RuntimeError(
            'Could not locate Git config for host repository.\n'
            f'Repo: {repo_root}\n'
            f'Git said: {msg}'
        )
    git_cfg = Path((git_dir_probe.stdout or '').strip()) / 'config'
    probe = run_cmd(
        ['git', '-C', str(repo_root), 'remote', 'get-url', remote_name],
        sudo=False,
        check=False,
        capture=True,
    )
    existing_url = (probe.stdout or '').strip() if probe.code == 0 else ''
    if existing_url == remote_url:
        return git_cfg, False
    if existing_url:
        purpose = (
            f"Update Git remote '{remote_name}' URL from '{existing_url}' to "
            f"'{remote_url}'."
        )
        cmd = [
            'git',
            '-C',
            str(repo_root),
            'remote',
            'set-url',
            remote_name,
            remote_url,
        ]
    else:
        purpose = (
            f"Register Git remote '{remote_name}' with URL '{remote_url}'."
        )
        cmd = [
            'git',
            '-C',
            str(repo_root),
            'remote',
            'add',
            remote_name,
            remote_url,
        ]
    _confirm_external_file_update(
        yes=bool(yes),
        path=git_cfg,
        purpose=purpose,
    )
    run_cmd(cmd, sudo=False, check=True, capture=True)
    return git_cfg, True


def _warn_if_git_repo_dirty(repo_root: Path) -> None:
    dirty = run_cmd(
        ['git', '-C', str(repo_root), 'status', '--porcelain'],
        sudo=False,
        check=False,
        capture=True,
    )
    if (dirty.stdout or '').strip():
        print(
            f'Warning: host repo {repo_root} has uncommitted changes; Git attachment sync only transfers committed branch state.'
        )


def _git_current_branch(repo_root: Path) -> str:
    branch = run_cmd(
        ['git', '-C', str(repo_root), 'rev-parse', '--abbrev-ref', 'HEAD'],
        sudo=False,
        check=False,
        capture=True,
    )
    if branch.code != 0:
        msg = (branch.stderr or branch.stdout).strip()
        raise RuntimeError(
            'Could not determine current Git branch for attachment sync.\n'
            f'Repo: {repo_root}\n'
            f'Git said: {msg}'
        )
    name = (branch.stdout or '').strip()
    if not name or name == 'HEAD':
        raise RuntimeError(
            f'Git attachment mode requires a named branch in {repo_root}; detached HEAD is not supported.'
        )
    return name


def _ensure_guest_git_repo(
    cfg: AgentVMConfig,
    guest_repo_root: str,
    branch: str,
) -> None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    script = (
        f'mkdir -p {shlex.quote(guest_repo_root)} && '
        f'if [ ! -d {shlex.quote(guest_repo_root + "/.git")} ]; then '
        f'if [ -n "$(find {shlex.quote(guest_repo_root)} -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then '
        f'echo "guest target directory is not empty and is not a git repo: {guest_repo_root}" >&2; '
        f'exit 2; '
        f'fi; '
        f'git init {shlex.quote(guest_repo_root)} >/dev/null; '
        f'fi && '
        f'git -C {shlex.quote(guest_repo_root)} config receive.denyCurrentBranch updateInstead && '
        f'git -C {shlex.quote(guest_repo_root)} symbolic-ref HEAD refs/heads/{shlex.quote(branch)}'
    )
    res = run_cmd(
        [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            cfg.vm.name,
            script,
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if res.code != 0:
        raise RuntimeError(
            'Failed to prepare guest Git repo for attachment sync.\n'
            f'Guest repo: {guest_repo_root}\n'
            f'Error: {(res.stderr or res.stdout).strip()}'
        )


def _push_host_repo_to_guest(
    repo_root: Path,
    *,
    remote_name: str,
    branch: str,
) -> None:
    push = run_cmd(
        [
            'git',
            '-C',
            str(repo_root),
            'push',
            remote_name,
            f'HEAD:refs/heads/{branch}',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if push.code != 0:
        msg = (push.stderr or push.stdout).strip()
        if 'working tree has unstaged or staged changes' in msg.lower():
            raise RuntimeError(
                'Guest Git repo rejected host push because its working tree is not clean.\n'
                f'Remote: {remote_name}\n'
                f'Branch: {branch}\n'
                f'Git said: {msg}'
            )
        raise RuntimeError(
            'Failed to push host branch into guest Git repo.\n'
            f'Remote: {remote_name}\n'
            f'Branch: {branch}\n'
            f'Git said: {msg}'
        )


def _ensure_git_clone_attachment(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    ip: str,
    *,
    yes: bool,
    dry_run: bool,
) -> tuple[Path, str, str]:
    del ip
    repo_root, repo_rel = _git_repo_context(host_src)
    branch = _git_current_branch(repo_root)
    guest_repo_root = _guest_repo_root_for_attachment(attachment, repo_rel)
    remote_name = _git_attachment_remote_name(cfg, repo_root)
    remote_url = f'{cfg.vm.name}:{guest_repo_root}'
    ssh_cfg, _ = _upsert_ssh_config_entry(cfg, dry_run=dry_run, yes=yes)
    git_cfg, _ = _upsert_host_git_remote(
        repo_root,
        remote_name=remote_name,
        remote_url=remote_url,
        yes=yes,
    )
    if dry_run:
        return repo_root, ssh_cfg.as_posix(), git_cfg.as_posix()

    _warn_if_git_repo_dirty(repo_root)
    _ensure_guest_git_repo(cfg, guest_repo_root, branch)
    _push_host_repo_to_guest(
        repo_root,
        remote_name=remote_name,
        branch=branch,
    )
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    final_probe = run_cmd(
        [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            cfg.vm.name,
            f'test -e {shlex.quote(attachment.guest_dst)}',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if final_probe.code != 0:
        raise RuntimeError(
            'Guest Git sync completed, but the requested path is missing inside the guest repo.\n'
            f'Host source: {host_src}\n'
            f'Guest path: {attachment.guest_dst}\n'
            'If this path only exists in uncommitted host changes, commit them before using git attachment mode.'
        )
    return repo_root, str(ssh_cfg), str(git_cfg)
