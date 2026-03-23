"""Shared utility helpers and compatibility wrappers.

``run_cmd`` remains the compatibility seam for older call sites, but real
subprocess orchestration now lives in :mod:`aivm.commands`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional, Sequence

from . import commands as _commands
from .commands import (
    CommandManager,
    shell_join,
)
from .commands import CommandError as CmdError  # used for backwards compat. TODO: remove # NOQA
from .commands import (
    CommandResult as CmdResult,
)

# Keep these module aliases for compatibility with older tests/helpers that
# monkeypatch ``aivm.util.os`` / ``sys`` / ``subprocess`` directly.
os = _commands.os
sys = _commands.sys
subprocess = _commands.subprocess


def arm_sudo_intent(
    *,
    yes: bool,
    purpose: str,
    action: str = 'modify',
    sticky: bool = False,
) -> None:
    CommandManager.current().compat_arm_sudo_intent(
        yes=bool(yes),
        purpose=str(purpose),
        action=action,
        sticky=bool(sticky),
    )


def clear_sudo_intent() -> None:
    CommandManager.current().compat_clear_sudo_intent()


def sudo_intent_auto_yes() -> bool:
    return CommandManager.current().compat_auto_yes()


def run_cmd(
    cmd: Sequence[str],
    *,
    sudo: bool = False,
    sudo_action: str | None = None,
    check: bool = True,
    capture: bool = True,
    text: bool = True,
    input_text: Optional[str] = None,
    env: Optional[dict[str, str]] = None,
    timeout: float | None = None,
) -> CmdResult:
    """Compatibility wrapper around the centralized command manager."""
    return (
        CommandManager.current()
        .submit(
            cmd,
            sudo=sudo,
            role=sudo_action,  # type: ignore[arg-type]
            check=check,
            capture=capture,
            text=text,
            input_text=input_text,
            env=env,
            timeout=timeout,
            eager=True,
            summary=shell_join(cmd),
        )
        .result()
    )


def which(cmd: str) -> Optional[str]:
    from shutil import which as _which

    return _which(cmd)


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def expand(path: str) -> str:
    return os.path.expandvars(os.path.expanduser(path))
