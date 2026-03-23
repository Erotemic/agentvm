"""Tests for test cli status helpers."""

from __future__ import annotations

from aivm.vm.drift import (
    parse_dominfo_hardware as _parse_dominfo_hardware,
    hardware_drift_report,
    saved_vm_drift_report,
)
from aivm.config import AgentVMConfig
from aivm.status import (
    ProbeOutcome,
    probe_firewall,
    probe_network,
    probe_vm_state,
    render_status,
)
from aivm.util import CmdResult


def test_check_network_parsing_and_permission(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.network.name = 'aivm-net'

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'permission denied'),
    )
    out = probe_network(cfg, use_sudo=False)
    assert out.ok is None
    assert 'status --sudo' in out.detail

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(0, 'Active: yes\nAutostart: no\n', ''),
    )
    out = probe_network(cfg, use_sudo=True)
    assert out.ok is True
    assert 'autostart=no' in out.detail


def test_check_firewall_branches(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.enabled = False
    out = probe_firewall(cfg, use_sudo=False)
    assert out.ok is None
    assert 'disabled' in out.detail

    cfg.firewall.enabled = True
    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'operation not permitted'),
    )
    out = probe_firewall(cfg, use_sudo=False)
    assert out.ok is None
    assert 'status --sudo' in out.detail

    monkeypatch.setattr(
        'aivm.status.run_cmd', lambda *a, **k: CmdResult(0, '', '')
    )
    out = probe_firewall(cfg, use_sudo=True)
    assert out.ok is True
    assert 'present' in out.detail


def test_check_vm_state_branches(monkeypatch) -> None:
    cfg = AgentVMConfig()

    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(1, '', 'authentication failed'),
    )
    out, defined = probe_vm_state(cfg, use_sudo=False)
    assert out.ok is None
    assert defined is None
    assert 'status --sudo' in out.detail

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[3] == 'dominfo':
            return CmdResult(0, 'ok', '')
        return CmdResult(0, 'running', '')

    monkeypatch.setattr('aivm.status.run_cmd', fake_run_cmd)
    out, defined = probe_vm_state(cfg, use_sudo=True)
    assert out.ok is True
    assert defined is True
    assert 'state=running' in out.detail
    assert len(calls) == 2


def test_render_status_non_sudo_keeps_vm_unknown_distinct_from_missing(
    monkeypatch, tmp_path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'aivm-2404'
    cfg.vm.user = 'agent'
    cfg.firewall.enabled = True
    cfg.provision.enabled = True
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'

    monkeypatch.setattr(
        'aivm.status.check_commands',
        lambda: ([], []),
    )
    monkeypatch.setattr(
        'aivm.status.probe_runtime_environment',
        lambda: ProbeOutcome(None, 'unable to determine host vs guest', ''),
    )
    monkeypatch.setattr(
        'aivm.status.probe_network',
        lambda *a, **k: ProbeOutcome(
            None,
            'aivm-net unavailable (run status --sudo for privileged checks)',
            '',
        ),
    )
    monkeypatch.setattr(
        'aivm.status.probe_firewall',
        lambda *a, **k: ProbeOutcome(
            None,
            'requires privileges (run status --sudo for firewall checks)',
            '',
        ),
    )

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        if cmd[:2] == ['test', '-f']:
            return CmdResult(1, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.status.run_cmd', fake_run_cmd)
    monkeypatch.setattr(
        'aivm.status.probe_vm_state',
        lambda *a, **k: (
            ProbeOutcome(
                None,
                'aivm-2404 unavailable (run status --sudo for privileged checks)',
                '',
            ),
            None,
        ),
    )
    monkeypatch.setattr('aivm.status.get_ip_cached', lambda *_a, **_k: '10.77.0.166')
    monkeypatch.setattr(
        'aivm.status.probe_ssh_ready',
        lambda *_a, **_k: ProbeOutcome(True, 'ready', ''),
    )
    monkeypatch.setattr(
        'aivm.status.probe_provisioned',
        lambda *_a, **_k: ProbeOutcome(
            False, 'one or more configured packages missing', ''
        ),
    )

    text = render_status(cfg, tmp_path / 'config.toml', use_sudo=False)
    assert (
        'VM state - aivm-2404 reachable over SSH (libvirt state unavailable without --sudo)'
        in text
    )
    assert (
        'VM shared folders - guest is reachable, but host mappings need privileged VM checks'
        in text
    )
    assert 'Cached VM IP - 10.77.0.166' in text
    assert 'SSH readiness - ready' in text
    assert 'Provisioning - one or more configured packages missing' in text
    assert 'stale: VM not defined' not in text


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
        'aivm.vm.drift.run_cmd',
        lambda *a, **k: CmdResult(
            0, 'CPU(s): 2\nMax memory: 4194304 KiB\n', ''
        ),
    )
    report = hardware_drift_report(cfg, use_sudo=False)
    assert report.ok is False
    assert len(report.items) == 2
    # Check that both CPU and RAM drift are detected
    keys = {item.key for item in report.items}
    assert 'cpus' in keys
    assert 'ram_mb' in keys
