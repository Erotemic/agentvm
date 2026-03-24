"""Host-focused CLI commands.

Covers host preflight checks, dependency installation, and host-level helpers
that are prerequisites for VM workflows.
"""

from __future__ import annotations

import sys

import scriptconfig as scfg

from ..commands import CommandManager
from ..host import (
    check_commands,
    check_commands_with_sudo,
    host_is_debian_like,
    install_deps_debian,
)
from ..vm import fetch_image
from ._common import (
    _BaseCommand,
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
        mgr = CommandManager.current()
        with mgr.intent(
            'Prepare host dependencies',
            why='Install the host packages required for libvirt-managed VM workflows.',
            role='modify',
        ):
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
        mgr = CommandManager.current()
        with mgr.intent(
            'Fetch base image',
            why='Prepare the Ubuntu cloud image used for later VM creation.',
            role='modify',
        ):
            print(str(fetch_image(cfg, dry_run=args.dry_run)))
        return 0


class HostModalCLI(scfg.ModalCLI):
    """Host preparation and host-level operations."""

    doctor = DoctorCLI
    install_deps = HostInstallDepsCLI
    image_fetch = ImageFetchCLI
    net = NetModalCLI
    fw = FirewallModalCLI
