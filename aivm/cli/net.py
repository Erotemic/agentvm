"""CLI commands for libvirt network lifecycle management."""

from __future__ import annotations

import scriptconfig as scfg

from ..config import AgentVMConfig, FirewallConfig, NetworkConfig
from ..net import destroy_network, ensure_network, network_status
from ..store import (
    find_network,
    load_store,
    network_users,
    remove_network,
    save_store,
)
from ._common import (
    _BaseCommand,
    _cfg_path,
    _confirm_sudo_block,
)


class NetCreateCLI(_BaseCommand):
    """Create or recreate the configured libvirt network."""

    network = scfg.Value(
        '',
        position=1,
        help='Optional managed network name (positional).',
    )
    recreate = scfg.Value(
        False, isflag=True, help='Destroy and recreate if it exists.'
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _resolve_network_cfg(args.config, network_opt=args.network)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose=f"Create/update libvirt network '{cfg.network.name}'.",
        )
        ensure_network(cfg, recreate=args.recreate, dry_run=args.dry_run)
        return 0


class NetStatusCLI(_BaseCommand):
    """Print detailed status of the configured libvirt network."""

    network = scfg.Value(
        '',
        position=1,
        help='Optional managed network name (positional).',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg = _resolve_network_cfg(args.config, network_opt=args.network)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Inspect libvirt network status via virsh.',
        )
        print(network_status(cfg))
        return 0


class NetDestroyCLI(_BaseCommand):
    """Destroy and undefine the configured libvirt network."""

    network = scfg.Value(
        '',
        position=1,
        help='Optional managed network name (positional).',
    )
    force = scfg.Value(
        False,
        isflag=True,
        help='Allow destroying network even if referenced by managed VMs.',
    )
    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg_path = _cfg_path(args.config)
        reg = load_store(cfg_path)
        cfg = _resolve_network_cfg(
            args.config, network_opt=args.network, reg=reg
        )
        users = network_users(reg, cfg.network.name)
        if users and not args.force and not args.dry_run:
            names = ', '.join(users)
            raise RuntimeError(
                f"Network '{cfg.network.name}' is referenced by managed VMs: {names}. "
                'Detach or destroy those VMs first, or use --force.'
            )
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Destroy/undefine libvirt network.'
        )
        destroy_network(cfg, dry_run=args.dry_run)
        if not args.dry_run:
            remove_network(reg, cfg.network.name)
            save_store(reg, _cfg_path(args.config))
        return 0


class NetModalCLI(scfg.ModalCLI):
    """Network subcommands."""

    create = NetCreateCLI
    status = NetStatusCLI
    destroy = NetDestroyCLI


def _resolve_network_cfg(
    config_opt: str | None,
    *,
    network_opt: str = '',
    reg=None,
) -> AgentVMConfig:
    reg = reg if reg is not None else load_store(_cfg_path(config_opt))
    net_name = str(network_opt or '').strip()
    if not net_name:
        if reg.active_vm:
            vm = next((v for v in reg.vms if v.name == reg.active_vm), None)
            if vm is not None:
                net_name = vm.network_name
        if not net_name and len(reg.networks) == 1:
            net_name = reg.networks[0].name
        if not net_name and len(reg.vms) == 1:
            net_name = reg.vms[0].network_name
    if not net_name:
        raise RuntimeError(
            'Unable to resolve a managed network. Pass a network name explicitly.'
        )
    net = find_network(reg, net_name)
    if net is None:
        raise RuntimeError(
            f'Managed network not found in config store: {net_name}'
        )
    cfg = AgentVMConfig()
    cfg.network = NetworkConfig(**net.network.__dict__)
    cfg.firewall = FirewallConfig(**net.firewall.__dict__)
    cfg.network.name = net.name
    return cfg
