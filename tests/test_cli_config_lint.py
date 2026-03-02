"""Tests for config-store lint command and unknown-key detection."""

from __future__ import annotations

from pathlib import Path

from aivm.cli.config import ConfigLintCLI, _lint_store_file


def test_lint_store_file_detects_unknown_sections(tmp_path: Path) -> None:
    fpath = tmp_path / 'config.toml'
    fpath.write_text(
        '\n'.join(
            [
                'schema_version = 3',
                'active_vm = "aivm-2404"',
                'rogue_top = "x"',
                '',
                '[[networks]]',
                'name = "aivm-net"',
                '[networks.network]',
                'bridge = "virbr-aivm"',
                '[networks.firewall]',
                'enabled = true',
                '',
                '[[vms]]',
                'name = "aivm-2404"',
                'network_name = "aivm-net"',
                '[vms.vm]',
                'name = "aivm-2404"',
                '[vms.share]',
                'host_src = "/tmp/x"',
                '',
                '[[attachments]]',
                'host_path = "/tmp/x"',
                'vm_name = "aivm-2404"',
                'mode = "shared"',
                'guest_dst = "/tmp/x"',
                'tag = "hostcode"',
                'extra = "bad"',
                '',
            ]
        ),
        encoding='utf-8',
    )
    probs = _lint_store_file(fpath)
    text = '\n'.join(probs)
    assert 'unknown top-level key' in text
    assert "vms[0] unknown key/section: 'share'" in text
    assert "attachments[0] unknown key: 'extra'" in text


def test_config_lint_cli_passes_for_clean_store(tmp_path: Path) -> None:
    fpath = tmp_path / 'config.toml'
    fpath.write_text(
        '\n'.join(
            [
                'schema_version = 3',
                'active_vm = "aivm-2404"',
                '',
                '[[networks]]',
                'name = "aivm-net"',
                '[networks.network]',
                'bridge = "virbr-aivm"',
                'subnet_cidr = "10.77.0.0/24"',
                'gateway_ip = "10.77.0.1"',
                'dhcp_start = "10.77.0.100"',
                'dhcp_end = "10.77.0.200"',
                '[networks.firewall]',
                'enabled = true',
                '',
                '[[vms]]',
                'name = "aivm-2404"',
                'network_name = "aivm-net"',
                '[vms.vm]',
                'name = "aivm-2404"',
                'cpus = 2',
                'ram_mb = 2048',
                '',
                '[[attachments]]',
                'host_path = "/tmp/x"',
                'vm_name = "aivm-2404"',
                'mode = "shared"',
                'guest_dst = "/tmp/x"',
                'tag = "hostcode"',
                '',
            ]
        ),
        encoding='utf-8',
    )
    rc = ConfigLintCLI.main(argv=False, config=str(fpath), yes=True)
    assert rc == 0
