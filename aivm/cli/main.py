"""Top-level modal CLI wiring, argv normalization, and logging setup."""

from __future__ import annotations

import sys
from pathlib import Path

import scriptconfig as scfg

from ..config import AgentVMConfig
from ..firewall import apply_firewall
from ..net import ensure_network
from ..status import (
    clip as _clip_text,
)
from ..status import (
    render_global_status,
    render_status,
    status_line,
)
from ..store import load_store
from ..vm import (
    create_or_start_vm,
    fetch_image,
    provision,
    wait_for_ip,
)
from ..vm import (
    ssh_config as mk_ssh_config,
)
from ._common import (
    _BaseCommand,
    _cfg_path,
    _confirm_sudo_block,
    _load_cfg_with_path,
    _record_vm,
    _resolve_cfg_for_code,
    log,
)
from .config import ConfigModalCLI
from .help import HelpModalCLI
from .host import HostModalCLI
from .vm import SSHCLI, AttachCLI, CodeCLI, VMModalCLI


class ApplyCLI(_BaseCommand):
    """Run the full setup workflow from network to provisioning."""

    interactive = scfg.Value(
        False, isflag=True, help='Print plan and SSH config at the end.'
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, cfg_path = _load_cfg_with_path(args.config)
        if args.interactive:
            from .help import PlanCLI

            PlanCLI.main(argv=False, config=args.config, verbose=args.verbose)
            print()
        log.debug('Ensuring network is set up')
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=f"Create/update libvirt network '{cfg.network.name}'.",
        )
        ensure_network(cfg, recreate=False, dry_run=args.dry_run)
        if cfg.firewall.enabled:
            log.debug('Applying firewall rules')
            _confirm_sudo_block(
                yes=bool(args.yes), purpose='Apply nftables firewall rules.'
            )
            apply_firewall(cfg, dry_run=args.dry_run)
        log.debug('Fetching Ubuntu image')
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Download/cache VM base image.'
        )
        fetch_image(cfg, dry_run=args.dry_run)
        log.debug('Creating or starting VM')
        _confirm_sudo_block(
            yes=bool(args.yes), purpose=f"Create/start VM '{cfg.vm.name}'."
        )
        create_or_start_vm(cfg, dry_run=args.dry_run, recreate=False)
        if not args.dry_run:
            _record_vm(cfg, cfg_path)
        log.debug('Waiting for VM IP address')
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Query VM networking state via virsh.'
        )
        wait_for_ip(cfg, timeout_s=360, dry_run=args.dry_run)
        if cfg.provision.enabled:
            log.debug('Provisioning VM with tools')
            provision(cfg, dry_run=args.dry_run)
        if args.interactive:
            print('\nSSH config for VS Code:')
            print(mk_ssh_config(cfg))
        return 0


class ListCLI(_BaseCommand):
    """List managed VMs, managed networks, and attached host folders."""

    section = scfg.Value(
        'all',
        help='One of: all, vms, networks, folders.',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        want = str(args.section or 'all').strip().lower()
        allowed = {'all', 'vms', 'networks', 'folders'}
        if want not in allowed:
            raise RuntimeError(
                f'--section must be one of: {", ".join(sorted(allowed))}'
            )

        reg_path = _cfg_path(args.config)
        reg = load_store(reg_path)

        if want in {'all', 'vms'}:
            print('Managed VMs')
            if not reg.vms:
                print('  (none)')
            else:
                by_net = {n.name: n for n in reg.networks}
                for vm in sorted(reg.vms, key=lambda x: x.name):
                    strict = (
                        bool(by_net[vm.network_name].firewall.enabled)
                        if vm.network_name in by_net
                        else False
                    )
                    print(
                        f'  - {vm.name} | network={vm.network_name} '
                        f'| strict_firewall={"yes" if strict else "no"} '
                        f'| store={reg_path}'
                    )

        if want in {'all', 'networks'}:
            if want == 'all':
                print('')
            print('Managed Networks')
            if not reg.networks:
                print('  (none)')
            else:
                usage: dict[str, int] = {n.name: 0 for n in reg.networks}
                for vm in reg.vms:
                    usage[vm.network_name] = usage.get(vm.network_name, 0) + 1
                for net in sorted(reg.networks, key=lambda n: n.name):
                    print(
                        f'  - {net.name} | strict_firewall={"yes" if net.firewall.enabled else "no"} '
                        f'| vm_count={usage.get(net.name, 0)}'
                    )

        if want in {'all', 'folders'}:
            if want == 'all':
                print('')
            print('Attached Folders')
            if not reg.attachments:
                print('  (none)')
            else:
                for att in sorted(
                    reg.attachments, key=lambda x: (x.vm_name, x.host_path)
                ):
                    print(
                        f'  - {att.host_path} | vm={att.vm_name} '
                        f'| mode={att.mode} | guest_dst={att.guest_dst or "(default)"}'
                    )
        print('')
        print(f'Config store: {reg_path}')
        return 0


class StatusCLI(_BaseCommand):
    """Report setup progress across host, network, VM, SSH, and provisioning."""

    sudo = scfg.Value(
        False,
        isflag=True,
        help='Run privileged status checks (virsh/nft/image) with sudo.',
    )
    vm = scfg.Value(
        '',
        help='Optional VM name override.',
    )
    detail = scfg.Value(
        False,
        isflag=True,
        help='Include raw diagnostics (virsh/nft/ssh probe outputs).',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg = None
        path = None
        try:
            if args.config is not None or _cfg_path(None).exists():
                cfg, path = _load_cfg_with_path(args.config)
            else:
                cfg, path = _resolve_cfg_for_code(
                    config_opt=None,
                    vm_opt=args.vm,
                    host_src=Path.cwd(),
                )
        except Exception:
            cfg = None
            path = None
        if cfg is None or path is None:
            print(render_global_status())
            return 0
        if args.sudo:
            _confirm_sudo_block(
                yes=bool(args.yes),
                purpose=f"Inspect host/libvirt/firewall/VM state for status of '{cfg.vm.name}'.",
            )
        print(
            render_status(cfg, path, detail=args.detail, use_sudo=bool(args.sudo))
        )
        return 0


class AgentVMModalCLI(scfg.ModalCLI):
    """Local libvirt/KVM sandbox VM manager for coding agents."""

    config = ConfigModalCLI
    help = HelpModalCLI
    host = HostModalCLI
    code = CodeCLI
    ssh = SSHCLI
    attach = AttachCLI
    vm = VMModalCLI
    apply = ApplyCLI
    list = ListCLI
    status = StatusCLI


def main(argv: list[str] | None = None) -> None:
    if argv is None:
        argv = sys.argv[1:]

    try:
        rc = AgentVMModalCLI.main(argv=argv, _noexit=True)
    except Exception as ex:
        print(f'ERROR: {ex}', file=sys.stderr)
        log.error('Unhandled aivm error: {}', ex)
        raise
        sys.exit(2)

    if any(flag in argv for flag in ('-h', '--help')):
        sys.exit(0)
    if isinstance(rc, int):
        sys.exit(rc)
    sys.exit(0)