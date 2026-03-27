"""Tests for test host."""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from aivm.commands import CommandManager
from aivm.host import (
    check_commands,
    check_commands_with_sudo,
    host_is_debian_like,
    install_deps_debian,
)
from aivm.util import CmdResult


def test_check_commands(
    monkeypatch: MonkeyPatch,
) -> None:
    present = {'virsh', 'qemu-img', 'curl', 'ssh', 'nft'}
    monkeypatch.setattr(
        'aivm.host.which',
        lambda cmd: f'/usr/bin/{cmd}' if cmd in present else None,
    )
    missing, missing_opt = check_commands()
    assert 'virt-install' in missing
    assert 'cloud-localds' in missing
    assert 'nft' not in missing_opt


def test_check_commands_with_sudo(
    monkeypatch: MonkeyPatch,
) -> None:
    calls = []

    def fake_run_cmd(self, cmd, **kwargs : Any):
        calls.append(cmd)
        if cmd[:3] == ['sudo', '-n', 'true']:
            return CmdResult(0, '', '')
        if 'virt-install' in cmd[-1]:
            return CmdResult(1, '', '')
        return CmdResult(0, '/usr/bin/whatever\n', '')

    monkeypatch.setattr('aivm.host.CommandManager.run', fake_run_cmd)
    missing, err = check_commands_with_sudo()
    assert err is None
    assert 'virt-install' in missing
    assert calls[0][:3] == ['sudo', '-n', 'true']


def test_check_commands_with_sudo_no_passwordless(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'aivm.host.CommandManager.run',
        lambda self, cmd, **kwargs: CmdResult(
            1, '', 'sudo: a password is required'
        ),
    )
    missing, err = check_commands_with_sudo()
    assert missing == []
    assert err is not None
    assert 'sudo -n' in err


def test_host_is_debian_like(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        'aivm.host.Path.read_text',
        lambda self, encoding='utf-8': 'ID=ubuntu\nID_LIKE=debian\n',
    )
    assert host_is_debian_like() is True
    monkeypatch.setattr(
        'aivm.host.Path.read_text',
        lambda self, encoding='utf-8': 'ID=fedora\nID_LIKE=rhel\n',
    )
    assert host_is_debian_like() is False


def test_install_deps_debian_behaviors(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.host.host_is_debian_like', lambda: False)
    with pytest.raises(RuntimeError):
        install_deps_debian()

    calls = []
    monkeypatch.setattr('aivm.host.host_is_debian_like', lambda: True)
    CommandManager.activate(CommandManager(yes_sudo=True))

    class P:
        def __init__(self, returncode=0, stdout='', stderr='') -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr(
        'aivm.commands.os.geteuid',
        lambda: 1000,
    )
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (calls.append((cmd, kwargs)) or P()),
    )
    install_deps_debian()
    assert calls[0][0][:5] == [
        'sudo',
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'NEEDRESTART_MODE=a',
        'apt-get',
    ]
    assert calls[0][0][5] == 'update'
    assert calls[1][0][:5] == [
        'sudo',
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'NEEDRESTART_MODE=a',
        'apt-get',
    ]
    assert calls[1][0][5] == 'install'
    assert calls[2][0][:5] == [
        'sudo',
        'env',
        'DEBIAN_FRONTEND=noninteractive',
        'NEEDRESTART_MODE=a',
        'apt-get',
    ]
    assert calls[2][0][5] == 'install'
    assert calls[2][0][-1] == 'virtiofsd'
    assert calls[3][0][:4] == ['sudo', 'systemctl', 'enable', '--now']
    assert calls[0][1]['capture_output'] is False
    assert calls[1][1]['capture_output'] is False


def test_install_deps_debian_reports_apt_lock_cleanly(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.host.host_is_debian_like', lambda: True)
    CommandManager.activate(CommandManager(yes_sudo=True))

    class P:
        def __init__(self, returncode=100, stdout='', stderr='') -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)

    def fake_run(cmd, **kwargs : Any):
        del kwargs
        if cmd[-2:] == ['update', '-y']:
            return P(
                returncode=100,
                stderr='E: Could not get lock /var/lib/dpkg/lock-frontend. '
                'It is held by process 1234',
            )
        return P()

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_run)
    with pytest.raises(RuntimeError, match='apt/dpkg appears to be locked'):
        install_deps_debian()
