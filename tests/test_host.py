"""Tests for test host."""

from __future__ import annotations

import pytest

from aivm.host import check_commands, host_is_debian_like, install_deps_debian
from aivm.util import CmdResult


def test_check_commands(monkeypatch) -> None:
    present = {'virsh', 'qemu-img', 'curl', 'ssh', 'nft'}
    monkeypatch.setattr(
        'aivm.host.which',
        lambda cmd: f'/usr/bin/{cmd}' if cmd in present else None,
    )
    missing, missing_opt = check_commands()
    assert 'virt-install' in missing
    assert 'cloud-localds' in missing
    assert 'nft' not in missing_opt


def test_host_is_debian_like(monkeypatch) -> None:
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


def test_install_deps_debian_behaviors(monkeypatch) -> None:
    monkeypatch.setattr('aivm.host.host_is_debian_like', lambda: False)
    with pytest.raises(RuntimeError):
        install_deps_debian()

    calls = []
    monkeypatch.setattr('aivm.host.host_is_debian_like', lambda: True)

    def fake_run_cmd(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return CmdResult(0, '', '')

    monkeypatch.setattr(
        'aivm.host.run_cmd',
        fake_run_cmd,
    )
    install_deps_debian()
    assert calls[0][0][:3] == ['apt-get', 'update', '-y']
    assert calls[1][0][:3] == ['apt-get', 'install', '-y']
    assert calls[2][0][:3] == ['apt-get', 'install', '-y']
    assert calls[2][0][-1] == 'virtiofsd'
    assert calls[3][0][:3] == ['systemctl', 'enable', '--now']
    assert calls[0][1]['capture'] is False
    assert calls[1][1]['capture'] is False
