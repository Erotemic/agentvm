"""Tests for test firewall."""

from __future__ import annotations

from pytest import MonkeyPatch

from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.firewall import (
    _effective_bridge_and_gateway,
    _nft_script,
    apply_firewall,
    firewall_status,
)


def test_effective_bridge_and_gateway_prefers_live(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.network.name = 'aivm-net'
    cfg.network.bridge = 'virbr-aivm'
    cfg.network.gateway_ip = '10.77.0.1'

    class P:
        returncode = 0
        stdout = (
            '<network>'
            "<bridge name='virbr-live'/>"
            "<ip address='10.99.0.1'/>"
            '</network>'
        )
        stderr = ''

    CommandManager.activate(CommandManager(yes_sudo=True))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: P(),
    )
    bridge, gateway = _effective_bridge_and_gateway(cfg)
    assert bridge == 'virbr-live'
    assert gateway == '10.99.0.1'


def test_nft_script_deduplicates_blocks(
    monkeypatch: MonkeyPatch,
) -> None:
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


def test_nft_script_allows_configured_ports(
    monkeypatch: MonkeyPatch,
) -> None:
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


def test_nft_script_invalid_port_raises(
    monkeypatch: MonkeyPatch,
) -> None:
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


def test_apply_firewall_disabled_skips(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.enabled = False
    apply_firewall(cfg, dry_run=False)


def test_firewall_status_uses_readonly_step(
    monkeypatch: MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.firewall.table = 'aivm_fw'
    calls = []

    class P:
        returncode = 0
        stdout = 'table inet aivm_fw {}'
        stderr = ''

    CommandManager.activate(CommandManager(yes_sudo=True))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (calls.append((cmd, kwargs)) or P()),
    )

    text = firewall_status(cfg)

    assert text == 'table inet aivm_fw {}'
    assert calls == [
        (
            ['sudo', 'nft', 'list', 'table', 'inet', 'aivm_fw'],
            {
                'input': None,
                'capture_output': True,
                'text': True,
                'env': None,
                'timeout': None,
            },
        )
    ]


def test_apply_firewall_runs_delete_then_apply(
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
    monkeypatch.setattr(
        'aivm.firewall._effective_bridge_and_gateway',
        lambda _cfg: ('virbr-aivm', '10.77.0.1'),
    )
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 0)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (calls.append((cmd, kwargs)) or P()),
    )
    apply_firewall(cfg, dry_run=False)
    assert calls[0][0][:4] == ['nft', 'delete', 'table', 'inet']
    assert calls[1][0] == ['nft', '-f', '-']
