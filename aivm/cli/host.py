"""Host-focused CLI commands.

Covers host preflight checks, dependency installation, and host-level helpers
that are prerequisites for VM workflows.
"""

from __future__ import annotations

import sys

import scriptconfig as scfg

from ..host import (
    check_commands,
    check_commands_with_sudo,
    host_is_debian_like,
    install_deps_debian,
)
from ..pci import assess_device_readiness, render_readiness_report
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

    sudo = scfg.Value(
        False,
        isflag=True,
        help='Also verify required commands are available under sudo -n.',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        missing, missing_opt = check_commands()
        if missing:
            print('❌ Missing required commands:', ', '.join(missing))
            print('💡 On Debian/Ubuntu you can run: aivm host install_deps')
            return 2
        if args.sudo:
            missing_sudo, sudo_err = check_commands_with_sudo()
            if sudo_err:
                print(f'❌ Sudo preflight failed: {sudo_err}')
                return 2
            if missing_sudo:
                print(
                    '❌ Missing required commands under sudo PATH:',
                    ', '.join(missing_sudo),
                )
                print(
                    '💡 Ensure required tools are installed in locations '
                    'available to sudo secure_path.'
                )
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


class HostPCICheckCLI(_BaseCommand):
    """Inspect one PCI device for conservative passthrough readiness."""

    bdf = scfg.Value(
        '',
        position=1,
        help='PCI BDF to inspect (for example 0000:65:00.0).',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        _confirm_sudo_block(
            yes=bool(args.yes),
            purpose='Inspect PCI and libvirt node-device state for passthrough readiness.',
            action='read',
        )
        report = assess_device_readiness(args.bdf)
        print(render_readiness_report(report))
        return 1 if report.status == 'manual_steps_required' else 0


class HostPCIModalCLI(scfg.ModalCLI):
    """Host PCI passthrough inspection commands."""

    check = HostPCICheckCLI


class HostModalCLI(scfg.ModalCLI):
    """Host preparation and host-level operations."""

    doctor = DoctorCLI
    install_deps = HostInstallDepsCLI
    image_fetch = ImageFetchCLI
    pci = HostPCIModalCLI
    net = NetModalCLI
    fw = FirewallModalCLI
