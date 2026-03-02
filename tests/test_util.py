"""Tests for test util."""

from __future__ import annotations

import pytest

from aivm.util import CmdError, shell_join
from aivm.util import run_cmd as _run_cmd


def test_shell_join_quotes() -> None:
    cmd = ['echo', 'a b', "c'd"]
    s = shell_join(cmd)
    assert 'a b' in s
    assert 'echo' in s


def test_run_cmd_success_and_failure() -> None:
    ok = _run_cmd(['bash', '-lc', 'printf ok'], check=True, capture=True)
    assert ok.code == 0
    assert ok.stdout == 'ok'
    bad = _run_cmd(['bash', '-lc', 'exit 7'], check=False, capture=True)
    assert bad.code == 7
    with pytest.raises(CmdError):
        _run_cmd(['bash', '-lc', 'exit 9'], check=True, capture=True)


def test_run_cmd_sudo_prefix_when_non_root(monkeypatch) -> None:
    calls = []

    class P:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr(
        'aivm.util.subprocess.run',
        lambda cmd, **kwargs: (calls.append(cmd) or P()),
    )
    _run_cmd(['echo', 'x'], sudo=True, check=True, capture=True)
    assert calls[0][:3] == ['sudo', '-n', 'echo']
