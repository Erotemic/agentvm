"""Tests for test net."""

from __future__ import annotations

from typing import Any

import pytest
from pytest import MonkeyPatch

from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.net import (
    _route_overlap,
    destroy_network,
    ensure_network,
    network_status,
)
from aivm.util import CmdResult


def test_route_overlap_none_without_ip(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.net.which', lambda cmd: None)
    assert _route_overlap('10.77.0.0/24') is None


def test_route_overlap_detects_conflict(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.net.which', lambda cmd: '/usr/sbin/ip')
    monkeypatch.setattr(
        'aivm.net.CommandManager.run',
        lambda self, *a, **k: CmdResult(
            0,
            '10.77.0.0/24 dev virbr0\n10.78.0.0/24 dev virbr1\n',
            '',
        ),
    )
    assert _route_overlap('10.77.0.7/24') is None
    assert _route_overlap('10.77.0.0/23') == '10.77.0.0/24'


def test_ensure_network_bridge_len_and_overlap_errors(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.network.bridge = 'this-bridge-name-is-too-long'
    with pytest.raises(RuntimeError):
        ensure_network(cfg)
    cfg.network.bridge = 'virbr-aivm'
    monkeypatch.setattr('aivm.net._route_overlap', lambda _s: '10.1.0.0/16')
    with pytest.raises(RuntimeError):
        ensure_network(cfg)


def test_ensure_network_existing_not_recreate(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    calls = []

    class P:
        def __init__(
            self, returncode: int = 0, stdout: str = '', stderr: str = ''
        ) -> None:
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    CommandManager.activate(CommandManager())
    monkeypatch.setattr('aivm.net._route_overlap', lambda _s: None)
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 0)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: calls.append(cmd) or P(),
    )
    ensure_network(cfg, recreate=False, dry_run=False)
    assert calls == [['virsh', 'net-info', cfg.network.name]]


def test_network_status_and_destroy(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    calls = []

    def fake_run_cmd(self, cmd: list[str], **kwargs: Any):  # type: ignore[no-untyped-def]
        calls.append(cmd)
        if cmd[1] == 'net-info':
            return CmdResult(0, 'INFO', '')
        if cmd[1] == 'net-dumpxml':
            return CmdResult(0, '<network/>', '')
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.net.CommandManager.run', fake_run_cmd)
    out = network_status(cfg)
    assert 'INFO' in out
    assert '<network/>' in out
    destroy_network(cfg, dry_run=False)
    assert ['virsh', 'net-destroy', cfg.network.name] in calls
    assert ['virsh', 'net-undefine', cfg.network.name] in calls
