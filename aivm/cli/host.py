"""CLI commands for host setup, dependency checks, and host operations."""

from __future__ import annotations

import sys

import scriptconfig as scfg

from ..host import check_commands, host_is_debian_like, install_deps_debian
from ..vm import fetch_image
from ._common import (
    _BaseCommand,
    _confirm_sudo_block,
    _resolve_cfg_fallback,
)
from .firewall import FirewallModalCLI
from .net import NetModalCLI


class DoctorCLI(_BaseCommand):
    """Check host prerequisites and list missing required tools."""

    @classmethod
    def main(cls, argv=True, **kwargs):
        cls.cli(argv=argv, data=kwargs)
        missing, missing_opt = check_commands()
        if missing:
            print('❌ Missing required commands:', ', '.join(missing))
            print('💡 On Debian/Ubuntu you can run: aivm host install_deps')
            return 2
        if missing_opt:
            print('➖ Missing optional commands:', ', '.join(missing_opt))
        print('✅ Required host commands are present.')
        return 0


class HostInstallDepsCLI(_BaseCommand):
    """Install required host dependencies on Debian/Ubuntu."""

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        if not host_is_debian_like():
            print(
                '❌ Host not detected as Debian/Ubuntu. Install dependencies manually.',
                file=sys.stderr,
            )
            return 2
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Install host dependencies with apt/libvirt tooling.',
        )
        install_deps_debian(assume_yes=True)
        print('✅ Installed host dependencies (best effort).')
        return 0


class ImageFetchCLI(_BaseCommand):
    """Download/cache the configured Ubuntu base image."""

    dry_run = scfg.Value(
        False, isflag=True, help='Print actions without running.'
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        cfg, _ = _resolve_cfg_fallback(args.config)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Download/cache base image under libvirt-managed storage.',
        )
        print(str(fetch_image(cfg, dry_run=args.dry_run)))
        return 0


class HostModalCLI(scfg.ModalCLI):
    """Host preparation and host-level operations."""

    doctor = DoctorCLI
    install_deps = HostInstallDepsCLI
    image_fetch = ImageFetchCLI
    net = NetModalCLI
    fw = FirewallModalCLI
