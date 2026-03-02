"""Host dependency checks and host package installation routines."""

from __future__ import annotations

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


def host_is_debian_like() -> bool:
    try:
        data = Path('/etc/os-release').read_text(encoding='utf-8')
        return any(
            k in data for k in ('ID=debian', 'ID=ubuntu', 'ID_LIKE=debian')
        )
    except Exception:
        return False


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
    run_cmd(['apt-get', 'update', '-y'], sudo=True, check=True, capture=False)
    run_cmd(
        ['apt-get', 'install', '-y', *pkgs],
        sudo=True,
        check=True,
        capture=False,
    )
    # Some distros split virtiofsd into a separate package; install best-effort.
    # TODO: on 22.04 you can get virtiofsd with qemu-system-common, add
    # alternative to use that if virtiofsd is not available.
    virtiofsd_install = run_cmd(
        ['apt-get', 'install', '-y', 'virtiofsd'],
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
