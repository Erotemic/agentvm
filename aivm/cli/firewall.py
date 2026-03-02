"""CLI commands for applying, inspecting, and removing firewall rules."""

from __future__ import annotations

import scriptconfig as scfg

from ..firewall import apply_firewall, firewall_status, remove_firewall
from ._common import (
    _BaseCommand,
    _confirm_sudo_block,
    _resolve_cfg_fallback,
)


class FirewallApplyCLI(_BaseCommand):
    """Apply nftables isolation rules for the VM network."""

    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, _ = _resolve_cfg_fallback(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Apply nftables firewall rules.'
        )
        apply_firewall(cfg, dry_run=args.dry_run)
        return 0


class FirewallStatusCLI(_BaseCommand):
    """Print current nftables status for the configured firewall table."""

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, _ = _resolve_cfg_fallback(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Read nftables firewall status.'
        )
        print(firewall_status(cfg))
        return 0


class FirewallRemoveCLI(_BaseCommand):
    """Remove nftables rules managed by aivm."""

    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, _ = _resolve_cfg_fallback(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes), purpose='Remove nftables firewall rules.'
        )
        remove_firewall(cfg, dry_run=args.dry_run)
        return 0


class FirewallModalCLI(scfg.ModalCLI):
    """Firewall subcommands."""

    apply = FirewallApplyCLI
    status = FirewallStatusCLI
    remove = FirewallRemoveCLI
