"""Host prerequisite checks and installation helpers.

This module is intentionally narrow: detect required binaries and install common
Debian/Ubuntu dependencies used by VM/network/firewall workflows.
"""

from __future__ import annotations

import shlex

from pathlib import Path

from loguru import logger

from .util import run_cmd, which

log = logger

REQUIRED_CMDS = [
    'virsh',
    'virt-install',
    'qemu-img',
    'cloud-localds',
    'curl',
    'ip',
    'ssh',
]
OPTIONAL_CMDS = ['nft', 'ssh-keyscan']


def check_commands() -> tuple[list[str], list[str]]:
    missing = [c for c in REQUIRED_CMDS if which(c) is None]
    missing_opt = [c for c in OPTIONAL_CMDS if which(c) is None]
    return missing, missing_opt


def check_commands_with_sudo() -> tuple[list[str], str | None]:
    """Check required commands in a non-interactive sudo environment."""
    sudo_probe = run_cmd(
        ['sudo', '-n', 'true'], check=False, capture=True, text=True
    )
    if sudo_probe.code != 0:
        return [], (
            'sudo -n is not available. Configure passwordless sudo for e2e '
            'or run without --sudo checks.'
        )
    missing = []
    for cmd in REQUIRED_CMDS:
        # Match sudo's effective PATH and shell command lookup behavior.
        probe = run_cmd(
            ['sudo', '-n', 'sh', '-lc', f'command -v {shlex.quote(cmd)}'],
            check=False,
            capture=True,
            text=True,
        )
        if probe.code != 0:
            missing.append(cmd)
    return missing, None


def host_is_debian_like() -> bool:
    try:
        data = Path('/etc/os-release').read_text(encoding='utf-8')
        return any(
            k in data for k in ('ID=debian', 'ID=ubuntu', 'ID_LIKE=debian')
        )
    except Exception:
        return False


def _debian_noninteractive_cmd(*args: str) -> list[str]:
    # Keep Debian package operations explicitly non-interactive so bootstrap and
    # e2e flows do not emit debconf frontend warnings or hang on prompts.
    return [
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'NEEDRESTART_MODE=a',
        *args,
    ]


def install_deps_debian(*, assume_yes: bool = True) -> None:
    # TODO: add alternative ways to install deps for other common systems that
    # can use libvirt.

    # TODO: document what each library is and what we use it for here.
    if not host_is_debian_like():
        raise RuntimeError(
            'Host is not detected as Debian/Ubuntu; install deps manually.'
        )

    # TODO: handle dpkg errors because something else has the lock.
    pkgs = [
        'qemu-kvm',
        'qemu-system-common',
        'libvirt-daemon-system',
        'libvirt-clients',
        'virtinst',
        'cloud-image-utils',
        'qemu-utils',
        'curl',
        'openssh-client',
        'iproute2',
        'nftables',
    ]
    run_cmd(
        _debian_noninteractive_cmd('apt-get', 'update', '-y'),
        sudo=True,
        check=True,
        capture=False,
    )
    run_cmd(
        _debian_noninteractive_cmd('apt-get', 'install', '-y', *pkgs),
        sudo=True,
        check=True,
        capture=False,
    )
    # Some distros split virtiofsd into a separate package; install best-effort.
    # TODO: on 22.04 you can get virtiofsd with qemu-system-common, add
    # alternative to use that if virtiofsd is not available.
    virtiofsd_install = run_cmd(
        _debian_noninteractive_cmd('apt-get', 'install', '-y', 'virtiofsd'),
        sudo=True,
        check=False,
        capture=False,
    )
    if virtiofsd_install.code != 0:
        log.warning(
            'Optional package `virtiofsd` was not installed. '
            'Folder sharing may fail if virtiofsd is unavailable on this host.'
        )
    run_cmd(
        ['systemctl', 'enable', '--now', 'libvirtd'],
        sudo=True,
        check=False,
        capture=False,
    )
