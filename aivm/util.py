"""Shared utility helpers for subprocess execution, paths, and command formatting."""

from __future__ import annotations

import os
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Sequence

from loguru import logger

log = logger


@dataclass(frozen=True)
class CmdResult:
    code: int
    stdout: str
    stderr: str


class CmdError(RuntimeError):
    def __init__(self, cmd: Sequence[str] | str, result: CmdResult):
        self.cmd = cmd
        self.result = result
        super().__init__(
            f'Command failed (code={result.code}): {cmd}\n{result.stderr}'.strip()
        )


def shell_join(cmd: Sequence[str]) -> str:
    return ' '.join(shlex.quote(c) for c in cmd)


def run_cmd(
    cmd: Sequence[str],
    *,
    sudo: bool = False,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    input_text: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
) -> CmdResult:
    original_cmd = cmd
    log.opt(depth=1).trace(
        'run_cmd entry sudo={} check={} capture={} text={} cmd={}',
        sudo,
        check,
        capture,
        text,
        shell_join(cmd),
    )
    if sudo and os.geteuid() != 0:
        # Non-interactive sudo: fail fast if password/TTY is required.
        cmd = ['sudo', '-n', *cmd]
        log.opt(depth=1).debug(
            'Running with sudo: {}', shell_join(original_cmd)
        )
    log.opt(depth=1).debug('RUN: {}', shell_join(cmd))
    p = subprocess.run(
        cmd,
        input=input_text if input_text is not None else None,
        capture_output=capture,
        text=text,
        env=env,
    )
    res = CmdResult(p.returncode, p.stdout or '', p.stderr or '')
    log.opt(depth=1).trace(
        'run_cmd result code={} stdout_len={} stderr_len={} cmd={}',
        res.code,
        len(res.stdout),
        len(res.stderr),
        shell_join(cmd),
    )
    if check and p.returncode != 0:
        log.opt(depth=1).error(
            'Command failed code={} cmd={} stderr={} stdout={}',
            p.returncode,
            shell_join(cmd),
            res.stderr.strip(),
            res.stdout.strip(),
        )
        raise CmdError(cmd, res)
    if p.returncode == 0:
        log.opt(depth=1).debug('Command ok code=0 cmd={}', shell_join(cmd))
    return res


def which(cmd: str) -> Optional[str]:
    from shutil import which as _which

    return _which(cmd)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))
