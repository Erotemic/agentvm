"""Tests for test cli status helpers."""

from __future__ import annotations

from aivm.cli.vm import (
    _check_firewall,
    _check_network,
    _check_vm_state,
    _parse_dominfo_hardware,
    _vm_hardware_drift,
)
from aivm.config import AgentVMConfig
from aivm.util import CmdResult


def test_check_network_parsing_and_permission(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.network.name = 'aivm-net'

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'permission denied'),
    )
    ok, detail = _check_network(cfg, use_sudo=False)
    assert ok is None
    assert 'status --sudo' in detail

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(0, 'Active: yes\nAutostart: no\n', ''),
    )
    ok, detail = _check_network(cfg, use_sudo=True)
    assert ok is True
    assert 'autostart=no' in detail


def test_check_firewall_branches(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.enabled = False
    ok, detail = _check_firewall(cfg, use_sudo=False)
    assert ok is None
    assert 'disabled' in detail

    cfg.firewall.enabled = True
    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'operation not permitted'),
    )
    ok, detail = _check_firewall(cfg, use_sudo=False)
    assert ok is None
    assert 'status --sudo' in detail

    monkeypatch.setattr(
        'aivm.status.run_cmd', lambda *a, **k: CmdResult(0, '', '')
    )
    ok, detail = _check_firewall(cfg, use_sudo=True)
    assert ok is True
    assert 'present' in detail


def test_check_vm_state_branches(monkeypatch) -> None:
    cfg = AgentVMConfig()

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'authentication failed'),
    )
    ok, defined, detail = _check_vm_state(cfg, use_sudo=False)
    assert ok is None
    assert defined is False
    assert 'status --sudo' in detail

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[3] == 'dominfo':
            return CmdResult(0, 'ok', '')
        return CmdResult(0, 'running', '')

    monkeypatch.setattr('aivm.status.run_cmd', fake_run_cmd)
    ok, defined, detail = _check_vm_state(cfg, use_sudo=True)
    assert ok is True
    assert defined is True
    assert 'state=running' in detail
    assert len(calls) == 2


def test_parse_dominfo_hardware() -> None:
    text = 'CPU(s):         2\nMax memory:     2097152 KiB\n'
    cpus, mem = _parse_dominfo_hardware(text)
    assert cpus == 2
    assert mem == 2048


def test_vm_hardware_drift(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.cpus = 4
    cfg.vm.ram_mb = 8192
    monkeypatch.setattr(
        'aivm.cli.vm.run_cmd',
        lambda *a, **k: CmdResult(
            0, 'CPU(s): 2\nMax memory: 4194304 KiB\n', ''
        ),
    )
    drift = _vm_hardware_drift(cfg)
    assert drift['cpus'] == (2, 4)
    assert drift['ram_mb'] == (4096, 8192)
