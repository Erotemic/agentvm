"""Runtime helpers for constructing virsh and SSH command arguments."""

from __future__ import annotations

from .errors import MissingSSHIdentityError

LIBVIRT_URI = 'qemu:///system'


def virsh_system_cmd(*args: str) -> list[str]:
    return ['virsh', '-c', LIBVIRT_URI, *args]


def require_ssh_identity(identity: str) -> str:
    ident = (identity or '').strip()
    if not ident:
        raise MissingSSHIdentityError(
            'paths.ssh_identity_file is empty; run aivm config init or set it in config.'
        )
    return ident


def ssh_base_args(
    ident: str,
    *,
    strict_host_key_checking: str = 'accept-new',
    connect_timeout: int | None = None,
    batch_mode: bool = False,
    user_known_hosts_file: str | None = None,
) -> list[str]:
    args: list[str] = []
    if batch_mode:
        args.extend(['-o', 'BatchMode=yes'])
    if connect_timeout is not None:
        args.extend(['-o', f'ConnectTimeout={connect_timeout}'])
    args.extend(['-o', f'StrictHostKeyChecking={strict_host_key_checking}'])
    if user_known_hosts_file:
        args.extend(['-o', f'UserKnownHostsFile={user_known_hosts_file}'])
    args.extend(['-i', ident])
    return args
