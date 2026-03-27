# PYTHON_ARGCOMPLETE_OK
"""Top-level CLI wiring and status entry points.

This module defines cross-group aliases and shared behaviors that should feel
consistent regardless of whether users enter through ``aivm vm ...`` or
top-level convenience commands.
"""

from __future__ import annotations

import sys
from pathlib import Path

import scriptconfig as scfg

from ..commands import CommandManager
from ..status import (
    anticipated_status_sudo_commands,
    render_global_status,
    render_status,
)
from ..store import load_store
from ._common import (
    _BaseCommand,
    _cfg_path,
    _load_cfg_with_path,
    _resolve_cfg_for_code,
    log,
)
from .config import ConfigModalCLI
from .help import HelpModalCLI
from .host import HostModalCLI
from .vm import SSHCLI, AttachCLI, CodeCLI, DetachCLI, VMModalCLI


class ListCLI(_BaseCommand):
    """List managed VMs, managed networks, and attached host folders."""

    section = scfg.Value(
        'all',
        help='One of: all, vms, networks, folders.',
    )

    @classmethod
    def main(cls, argv=True, **kwargs) -> int:
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
                        f'| mode={att.mode} | access={getattr(att, "access", "rw")} '
                        f'| guest_dst={att.guest_dst or "(default)"}'
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
        alias=['details'],
    )

    @classmethod
    def main(cls, argv=True, **kwargs) -> int:
        args = cls.cli(argv=argv, data=kwargs)
        cfg = None
        path = None
        try:
            if args.config is not None or _cfg_path(None).exists():
                cfg, path = _load_cfg_with_path(args.config, vm_opt=args.vm)
            else:
                cfg, path = _resolve_cfg_for_code(
                    config_opt=None,
                    vm_opt=args.vm,
                    host_src=Path.cwd(),
                )
        except RuntimeError as ex:
            log.debug('Status VM-resolution fallback: {}', ex)
            cfg = None
            path = None
        if cfg is None or path is None:
            print(render_global_status())
            return 0
        mgr = CommandManager.current()
        with mgr.intent(
            f"Inspect status for {cfg.vm.name}",
            why='Summarize host, network, firewall, VM, and SSH readiness for this managed VM.',
            role='read',
        ):
            if args.sudo:
                mgr.confirm_sudo_scope(
                    yes=bool(args.yes),
                    purpose=(
                        f"Inspect host/libvirt/firewall/VM state for status of '{cfg.vm.name}'."
                    ),
                    role='read',
                    preview_cmds=anticipated_status_sudo_commands(
                        cfg, detail=bool(args.detail)
                    ),
                )
            print(
                render_status(
                    cfg, path, detail=args.detail, use_sudo=bool(args.sudo)
                )
            )
        return 0


class AgentVMModalCLI(scfg.ModalCLI):
    """Local libvirt/KVM sandbox VM manager for coding agents."""

    help = HelpModalCLI
    status = StatusCLI
    list = ListCLI
    code = CodeCLI
    ssh = SSHCLI
    attach = AttachCLI
    detach = DetachCLI
    config = ConfigModalCLI
    vm = VMModalCLI
    host = HostModalCLI


def main(argv: list[str] | None = None) -> None:
    # if argv is None:
    #     argv = sys.argv[1:]

    try:
        rc = AgentVMModalCLI.main(argv=argv, _noexit=True)
    except Exception as ex:
        print(f'ERROR: {ex}', file=sys.stderr)
        log.error('Unhandled aivm error: {}', ex)
        raise
        sys.exit(2)

    # if any(flag in argv for flag in ('-h', '--help')):
    #     sys.exit(0)
    assert isinstance(rc, int)
    sys.exit(rc)
