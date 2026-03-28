"""Host prerequisite checks and installation helpers.

This module is intentionally narrow: detect required binaries and install common
Debian/Ubuntu dependencies used by VM/network/firewall workflows.
"""

from __future__ import annotations

import shlex
from pathlib import Path

from loguru import logger

from .commands import CommandError, CommandManager
from .util import which

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
    mgr = CommandManager.current()
    sudo_probe = mgr.run(
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
        probe = mgr.run(
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


def _is_apt_lock_error(ex: Exception) -> bool:
    if not isinstance(ex, CommandError):
        return False
    text = f'{ex.result.stderr}\n{ex.result.stdout}\n{ex}'.lower()
    lock_markers = (
        'could not get lock',
        'unable to acquire the dpkg frontend lock',
        'unable to lock the administration directory',
        'is another process using it',
    )
    return any(marker in text for marker in lock_markers)


def install_deps_debian(*, assume_yes: bool = True) -> None:
    # TODO: add alternative ways to install deps for other common systems that
    # can use libvirt.
    if not host_is_debian_like():
        raise RuntimeError(
            'Host is not detected as Debian/Ubuntu; install deps manually.'
        )
    pkgs = [
        # KVM/QEMU runtime used to boot the guest.
        'qemu-kvm',
        'qemu-system-common',
        # libvirt daemon + client tooling used by almost every host operation.
        'libvirt-daemon-system',
        'libvirt-clients',
        # `virt-install` and cloud image helpers used to define new VMs.
        'virtinst',
        'cloud-image-utils',
        # Disk/image inspection and copy-on-write helpers.
        'qemu-utils',
        # Host-side networking, SSH, and firewall tools that the workflows call.
        'curl',
        'openssh-client',
        'iproute2',
        'nftables',
    ]
    del assume_yes
    mgr = CommandManager.current()
    try:
        with mgr.intent(
            'Prepare host libvirt dependencies',
            why=(
                'Fresh-machine VM workflows need libvirt, qemu, cloud-init tools, '
                'and libvirtd available before network or VM setup can succeed.'
            ),
            role='modify',
        ):
            with mgr.step(
                'Install Debian/Ubuntu host dependencies',
                why=(
                    'Refresh apt metadata, install required VM host packages, '
                    'attempt optional virtiofsd installation, and enable libvirtd.'
                ),
                approval_scope='host-install-deps',
            ):
                mgr.submit(
                    _debian_noninteractive_cmd('apt-get', 'update', '-y'),
                    sudo=True,
                    role='modify',
                    check=True,
                    capture=False,
                    summary='Refresh apt package metadata',
                )
                mgr.submit(
                    _debian_noninteractive_cmd(
                        'apt-get', 'install', '-y', *pkgs
                    ),
                    sudo=True,
                    role='modify',
                    check=True,
                    capture=False,
                    summary='Install required qemu/libvirt/cloud-init host packages',
                )
                # Some distros split virtiofsd into a separate package; install
                # best-effort so folder sharing can work when available.
                virtiofsd_install = mgr.submit(
                    _debian_noninteractive_cmd(
                        'apt-get', 'install', '-y', 'virtiofsd'
                    ),
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=False,
                    summary='Try installing optional virtiofsd package',
                    detail='Folder sharing can rely on virtiofsd on some hosts.',
                )
                mgr.submit(
                    ['systemctl', 'enable', '--now', 'libvirtd'],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=False,
                    summary='Enable and start libvirtd service',
                )
    except CommandError as ex:
        if _is_apt_lock_error(ex):
            raise RuntimeError(
                'apt/dpkg appears to be locked by another process. '
                'Close other package managers or wait for unattended upgrades to finish, then retry.'
            ) from ex
        raise
    if virtiofsd_install.code != 0:
        log.warning(
            'Optional package `virtiofsd` was not installed. '
            'Folder sharing may fail if virtiofsd is unavailable on this host.'
        )
