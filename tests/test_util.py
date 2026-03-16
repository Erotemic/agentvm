"""Tests for test util."""

from __future__ import annotations

import builtins

import pytest

from aivm.util import CmdError, arm_sudo_intent, clear_sudo_intent, shell_join
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
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)
    clear_sudo_intent()
    monkeypatch.setattr(
        'aivm.util.subprocess.run',
        lambda cmd, **kwargs: (calls.append(cmd) or P()),
    )
    _run_cmd(['echo', 'x'], sudo=True, check=True, capture=True)
    assert calls[0][:2] == ['sudo', 'echo']


def test_run_cmd_sudo_uses_armed_intent_yes(monkeypatch) -> None:
    calls = []

    class P:
        def __init__(self, returncode=0, stdout='', stderr=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)
        return P(returncode=0)

    monkeypatch.setattr('aivm.util.subprocess.run', fake_subprocess_run)
    arm_sudo_intent(yes=True, purpose='test intent')
    _run_cmd(['virsh', 'dominfo', 'x'], sudo=True, check=False, capture=True)
    assert calls[0] == ['sudo', 'virsh', 'dominfo', 'x']


def test_run_cmd_sudo_uses_armed_intent_prompt(monkeypatch) -> None:
    calls = []

    class P:
        def __init__(self, returncode=0, stdout='', stderr=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(builtins, 'input', lambda _: 'y')

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)
        return P(returncode=0)

    monkeypatch.setattr('aivm.util.subprocess.run', fake_subprocess_run)
    arm_sudo_intent(yes=False, purpose='test intent')
    _run_cmd(['virsh', 'dominfo', 'x'], sudo=True, check=False, capture=True)
    assert calls[0] == ['sudo', 'virsh', 'dominfo', 'x']


def test_run_cmd_sudo_prompt_all_sticks_for_remaining_ops(
    monkeypatch,
) -> None:
    calls = []
    prompts = []

    class P:
        def __init__(self, returncode=0, stdout='', stderr=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)

    def fake_input(prompt):
        prompts.append(prompt)
        return 'a'

    monkeypatch.setattr(builtins, 'input', fake_input)

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        calls.append(cmd)
        return P(returncode=0)

    monkeypatch.setattr('aivm.util.subprocess.run', fake_subprocess_run)
    arm_sudo_intent(yes=False, purpose='test intent')
    _run_cmd(['virsh', 'dominfo', 'x'], sudo=True, check=False, capture=True)
    # Subsequent sudo command should not ask again.
    _run_cmd(['virsh', 'domstate', 'x'], sudo=True, check=False, capture=True)

    assert calls[0] == ['sudo', 'virsh', 'dominfo', 'x']
    assert calls[1] == ['sudo', 'virsh', 'domstate', 'x']
    assert len(prompts) == 1


def test_run_cmd_sudo_action_override_read(monkeypatch) -> None:
    seen_actions: list[str] = []
    calls = []

    class P:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)

    def fake_ensure(intent, cmd):
        del cmd
        seen_actions.append(intent.action)

    monkeypatch.setattr('aivm.util._ensure_sudo_ready', fake_ensure)
    monkeypatch.setattr(
        'aivm.util.subprocess.run',
        lambda cmd, **kwargs: (calls.append(cmd) or P()),
    )
    arm_sudo_intent(yes=True, purpose='test intent', action='modify')
    _run_cmd(
        ['virsh', 'dominfo', 'x'],
        sudo=True,
        sudo_action='read',
        check=False,
        capture=True,
    )

    assert seen_actions == ['read']
    assert calls[0] == ['sudo', 'virsh', 'dominfo', 'x']


def test_run_cmd_sudo_action_override_modify(monkeypatch) -> None:
    seen_actions: list[str] = []
    calls = []

    class P:
        returncode = 0
        stdout = ''
        stderr = ''

    monkeypatch.setattr('aivm.util.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.util.sys.stdin.isatty', lambda: True)

    def fake_ensure(intent, cmd):
        del cmd
        seen_actions.append(intent.action)

    monkeypatch.setattr('aivm.util._ensure_sudo_ready', fake_ensure)
    monkeypatch.setattr(
        'aivm.util.subprocess.run',
        lambda cmd, **kwargs: (calls.append(cmd) or P()),
    )
    arm_sudo_intent(yes=True, purpose='test intent', action='modify')
    _run_cmd(
        ['virsh', 'destroy', 'x'],
        sudo=True,
        sudo_action='modify',
        check=False,
        capture=True,
    )

    assert seen_actions == ['modify']
    assert calls[0] == ['sudo', 'virsh', 'destroy', 'x']
