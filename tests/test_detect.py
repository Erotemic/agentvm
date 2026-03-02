"""Tests for test detect."""

from __future__ import annotations

import ipaddress
from pathlib import Path

from aivm.config import AgentVMConfig
from aivm.detect import (
    auto_defaults,
    detect_ssh_identity,
    existing_ipv4_routes,
    pick_free_subnet,
)
from aivm.util import CmdResult


def test_existing_ipv4_routes_parsing(monkeypatch) -> None:
    def fake_which(cmd: str):
        return '/usr/sbin/ip' if cmd == 'ip' else None

    def fake_run_cmd(*args, **kwargs):
        return CmdResult(
            0,
            'default via 192.168.1.1 dev wlp2s0\n'
            '10.77.0.0/24 dev virbr-aivm proto kernel scope link src 10.77.0.1\n'
            '172.17.0.0/16 dev docker0 proto kernel scope link src 172.17.0.1\n',
            '',
        )

    monkeypatch.setattr('aivm.detect.which', fake_which)
    monkeypatch.setattr('aivm.detect.run_cmd', fake_run_cmd)
    got = existing_ipv4_routes()
    assert ipaddress.ip_network('10.77.0.0/24') in got
    assert ipaddress.ip_network('172.17.0.0/16') in got


def test_pick_free_subnet(monkeypatch) -> None:
    monkeypatch.setattr(
        'aivm.detect.existing_ipv4_routes',
        lambda: [ipaddress.ip_network('10.77.0.0/24')],
    )
    got = pick_free_subnet(['10.77.0.0/24', '10.78.0.0/24'])
    assert got == '10.78.0.0/24'


def test_auto_defaults_sets_network_and_identity(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.paths.ssh_identity_file = ''
    cfg.paths.ssh_pubkey_path = ''
    cfg.network.bridge = 'this-bridge-name-is-way-too-long-for-linux'

    monkeypatch.setattr(
        'aivm.detect.detect_ssh_identity',
        lambda: ('/tmp/id_a', '/tmp/id_a.pub'),
    )
    monkeypatch.setattr(
        'aivm.detect.pick_free_subnet', lambda preferred: '10.88.0.0/24'
    )

    out = auto_defaults(cfg, project_dir=tmp_path)
    assert out.paths.ssh_identity_file == '/tmp/id_a'
    assert out.paths.ssh_pubkey_path == '/tmp/id_a.pub'
    assert out.network.subnet_cidr == '10.88.0.0/24'
    assert out.network.gateway_ip == '10.88.0.1'
    assert out.network.dhcp_start == '10.88.0.100'
    assert out.network.dhcp_end == '10.88.0.200'
    assert out.network.bridge == 'virbr-aivm'


def test_detect_ssh_identity_prefers_ssh_config_identityfile(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / 'home'
    ssh_dir = home / '.ssh'
    ssh_dir.mkdir(parents=True)
    ident = ssh_dir / 'id_custom'
    pub = ssh_dir / 'id_custom.pub'
    ident.write_text('x', encoding='utf-8')
    pub.write_text('x', encoding='utf-8')
    (ssh_dir / 'config').write_text(
        'Host *\n  IdentityFile ~/.ssh/id_custom\n', encoding='utf-8'
    )
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setattr('aivm.detect.which', lambda cmd: None)
    got_ident, got_pub = detect_ssh_identity()
    assert got_ident == str(ident)
    assert got_pub == str(pub)
