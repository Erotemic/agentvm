"""Shared utility helpers for command execution and CLI safety boundaries.

This module is intentionally central: most host/VM operations eventually call
``run_cmd``. Keeping sudo-confirmation semantics here ensures the command shown
to users is the command actually executed.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from contextvars import ContextVar
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


@dataclass(frozen=True)
class SudoIntent:
    yes: bool
    purpose: str


_SUDO_INTENT: ContextVar[SudoIntent | None] = ContextVar(
    'aivm_sudo_intent', default=None
)


def shell_join(cmd: Sequence[str]) -> str:
    return ' '.join(shlex.quote(c) for c in cmd)


def arm_sudo_intent(*, yes: bool, purpose: str) -> None:
    _SUDO_INTENT.set(SudoIntent(yes=bool(yes), purpose=str(purpose)))


def clear_sudo_intent() -> None:
    _SUDO_INTENT.set(None)


def _consume_sudo_intent() -> SudoIntent | None:
    # Intentionally *not* one-shot. We keep the intent armed so each sudo call
    # in the current flow can require explicit confirmation unless --yes-sudo.
    return _SUDO_INTENT.get()


def _ensure_sudo_ready(intent: SudoIntent, cmd: Sequence[str]) -> None:
    cmd_line = shell_join(cmd)
    log.opt(depth=2).info('Planned privileged command(s):')
    log.opt(depth=2).info(f'  {cmd_line}')
    if os.geteuid() == 0:
        return
    if intent.yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(
            'Privileged host operations require confirmation, but stdin is not interactive. '
            'Re-run with --yes.'
        )
    log.opt(depth=2).info('About to run privileged host operations via sudo:')
    log.opt(depth=2).info(f'  {intent.purpose}')
    ans = input('Continue? [y/N]: ').strip().lower()
    if ans not in {'y', 'yes'}:
        raise RuntimeError('Aborted by user.')


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
    """Execute a command with consistent logging, sudo policy, and error handling.

    Design notes:
    * ``check=True`` is treated as an imperative/change action and logged at
      INFO so users can follow setup steps.
    * ``check=False`` is usually probe/introspection and logged at DEBUG.
    * sudo prompts are driven by intent armed from CLI orchestration code, so
      users see/approve the real privileged command instead of a probe command.
    """
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
        intent = _consume_sudo_intent()
        if intent is not None:
            _ensure_sudo_ready(intent, original_cmd)
        # Use interactive sudo when stdin is a TTY so the user sees/authenticates
        # on the actual command. In non-interactive mode, fail fast.
        cmd = ['sudo', *cmd] if sys.stdin.isatty() else ['sudo', '-n', *cmd]
        log.opt(depth=1).debug(
            'Running with sudo: {}', shell_join(original_cmd)
        )
    run_line = shell_join(cmd)
    if check:
        # check=True generally corresponds to imperative setup/change steps.
        log.opt(depth=1).info('RUN: {}', run_line)
    else:
        # check=False is commonly used for probes/introspection.
        log.opt(depth=1).debug('RUN: {}', run_line)
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
