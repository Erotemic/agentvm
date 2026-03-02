"""Tests for test firewall."""

from __future__ import annotations

from aivm.config import AgentVMConfig
from aivm.firewall import (
    _effective_bridge_and_gateway,
    _nft_script,
    apply_firewall,
)
from aivm.util import CmdResult


def test_effective_bridge_and_gateway_prefers_live(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.network.name = 'aivm-net'
    cfg.network.bridge = 'virbr-aivm'
    cfg.network.gateway_ip = '10.77.0.1'

    def fake_run_cmd(*args, **kwargs):
        xml = (
            '<network>'
            "<bridge name='virbr-live'/>"
            "<ip address='10.99.0.1'/>"
            '</network>'
        )
        return CmdResult(0, xml, '')

    monkeypatch.setattr('aivm.firewall.run_cmd', fake_run_cmd)
    bridge, gateway = _effective_bridge_and_gateway(cfg)
    assert bridge == 'virbr-live'
    assert gateway == '10.99.0.1'


def test_nft_script_deduplicates_blocks(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.block_cidrs = ['10.0.0.0/8', '10.0.0.0/8']
    cfg.firewall.extra_block_cidrs = ['192.168.0.0/16', ' 192.168.0.0/16 ']
    monkeypatch.setattr(
        'aivm.firewall._effective_bridge_and_gateway',
        lambda _cfg: ('virbr-aivm', '10.77.0.1'),
    )
    script = _nft_script(cfg)
    assert script.count('10.0.0.0/8') == 1
    assert script.count('192.168.0.0/16') == 1


def test_nft_script_allows_configured_ports(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.allow_tcp_ports = [22, 2222, 22]
    cfg.firewall.allow_udp_ports = [53]
    monkeypatch.setattr(
        'aivm.firewall._effective_bridge_and_gateway',
        lambda _cfg: ('virbr-aivm', '10.77.0.1'),
    )
    script = _nft_script(cfg)
    assert 'iifname "virbr-aivm" tcp dport {22, 2222} accept' in script
    assert 'iifname "virbr-aivm" udp dport {53} accept' in script
    assert ('iifname "virbr-aivm" ip daddr {' in script) and (
        'tcp dport {22, 2222} accept' in script
    )


def test_nft_script_invalid_port_raises(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.allow_tcp_ports = [0]
    monkeypatch.setattr(
        'aivm.firewall._effective_bridge_and_gateway',
        lambda _cfg: ('virbr-aivm', '10.77.0.1'),
    )
    try:
        _nft_script(cfg)
    except RuntimeError as ex:
        assert 'range 1..65535' in str(ex)
    else:
        raise AssertionError('Expected RuntimeError for invalid firewall port')


def test_apply_firewall_disabled_skips(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.enabled = False
    called = []
    monkeypatch.setattr(
        'aivm.firewall.run_cmd', lambda *a, **k: called.append((a, k))
    )
    apply_firewall(cfg, dry_run=False)
    assert called == []


def test_apply_firewall_runs_delete_then_apply(monkeypatch) -> None:
    cfg = AgentVMConfig()
    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append((cmd, kwargs))
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.firewall.run_cmd', fake_run_cmd)
    monkeypatch.setattr(
        'aivm.firewall._effective_bridge_and_gateway',
        lambda _cfg: ('virbr-aivm', '10.77.0.1'),
    )
    apply_firewall(cfg, dry_run=False)
    assert calls[0][0][:4] == ['nft', 'delete', 'table', 'inet']
    assert calls[1][0] == ['nft', '-f', '-']
