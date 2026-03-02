"""Host-to-guest settings file synchronization helpers."""

from __future__ import annotations

import shlex
from pathlib import Path

from loguru import logger

from ..config import AgentVMConfig
from ..results import SyncSettingsResult
from ..runtime import require_ssh_identity, ssh_base_args
from ..util import run_cmd

log = logger


def sync_settings(
    cfg: AgentVMConfig,
    ip: str,
    *,
    paths: list[str] | None = None,
    overwrite: bool = True,
    dry_run: bool = False,
) -> SyncSettingsResult:
    """Copy selected host user settings into the VM user home over SSH/SCP."""
    cfg = cfg.expanded_paths()
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    wanted = list(paths if paths is not None else cfg.sync.paths)
    host_home = Path.home()
    result = SyncSettingsResult()

    for raw in wanted:
        src_abs = Path(raw).expanduser()
        if not src_abs.is_absolute():
            src_abs = Path.cwd() / src_abs
        if not src_abs.exists():
            result.skipped_missing.append(str(src_abs))
            continue
        try:
            rel = src_abs.relative_to(host_home)
            remote_path = f'$HOME/{rel.as_posix()}'
        except ValueError:
            remote_path = f'$HOME/.aivm-sync/{src_abs.name}'

        remote_parent = (
            f'$HOME/{Path(remote_path.replace("$HOME/", "")).parent.as_posix()}'
        )
        check_cmd = [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            f'{cfg.vm.user}@{ip}',
            f'test -e {shlex.quote(remote_path)}',
        ]
        if not overwrite and not dry_run:
            exists = (
                run_cmd(check_cmd, sudo=False, check=False, capture=True).code
                == 0
            )
            if exists:
                result.skipped_exists.append(f'{src_abs} -> {remote_path}')
                continue

        mkdir_cmd = [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            f'{cfg.vm.user}@{ip}',
            f'mkdir -p {shlex.quote(remote_parent)}',
        ]
        scp_cmd = [
            'scp',
            '-r',
            '-o',
            'StrictHostKeyChecking=accept-new',
            '-i',
            ident,
            str(src_abs),
            f'{cfg.vm.user}@{ip}:{remote_parent}/',
        ]
        if dry_run:
            log.info('DRYRUN: {}', ' '.join(mkdir_cmd))
            log.info('DRYRUN: {}', ' '.join(scp_cmd))
            result.copied.append(f'{src_abs} -> {remote_path}')
            continue
        run_cmd(mkdir_cmd, sudo=False, check=True, capture=True)
        res = run_cmd(scp_cmd, sudo=False, check=False, capture=True)
        if res.code == 0:
            result.copied.append(f'{src_abs} -> {remote_path}')
        else:
            result.failed.append(
                f'{src_abs} -> {remote_path}: {res.stderr.strip()}'
            )

    return result
