"""Tests for test cli helpers."""

from __future__ import annotations

import builtins
import re
from pathlib import Path

import pytest

from aivm.cli._common import _confirm_external_file_update, _confirm_sudo_block
from aivm.cli.help import HelpRawCLI, PlanCLI
from aivm.cli.vm import (
    _auto_share_tag_for_path,
    _parse_sync_paths_arg,
    _upsert_ssh_config_entry,
)
from aivm.config import AgentVMConfig
from aivm.store import Store, save_store, upsert_vm


def test_parse_sync_paths_arg() -> None:
    got = _parse_sync_paths_arg(' ~/.gitconfig, ,~/.bashrc,')
    assert got == ['~/.gitconfig', '~/.bashrc']


def test_auto_share_tag_collision() -> None:
    p = Path('/tmp/my project')
    tag1 = _auto_share_tag_for_path(p, set())
    tag2 = _auto_share_tag_for_path(p, {tag1})
    assert tag1 != ''
    assert tag2 != tag1
    assert len(tag2) <= 36


def test_confirm_external_file_update_yes_noninteractive(
    monkeypatch,
) -> None:
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    _confirm_external_file_update(
        yes=True,
        path=Path('/tmp/ssh-config'),
        purpose='Update SSH entry',
    )


def test_confirm_external_file_update_requires_yes_noninteractive(
    monkeypatch,
) -> None:
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    with pytest.raises(RuntimeError, match='Re-run with --yes'):
        _confirm_external_file_update(
            yes=False,
            path=Path('/tmp/ssh-config'),
            purpose='Update SSH entry',
        )


def test_confirm_external_file_update_abort(monkeypatch) -> None:
    monkeypatch.setattr('sys.stdin.isatty', lambda: True)
    monkeypatch.setattr(builtins, 'input', lambda _: 'n')
    with pytest.raises(RuntimeError, match='Aborted by user'):
        _confirm_external_file_update(
            yes=False,
            path=Path('/tmp/ssh-config'),
            purpose='Update SSH entry',
        )


def test_upsert_ssh_config_no_confirm_when_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv('HOME', str(tmp_path))
    cfg = AgentVMConfig()
    path1, changed1 = _upsert_ssh_config_entry(cfg, dry_run=False, yes=True)
    assert path1.exists()
    assert changed1 is True

    # Should not require --yes when no file update is needed.
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    path2, changed2 = _upsert_ssh_config_entry(cfg, dry_run=False, yes=False)
    assert path2 == path1
    assert changed2 is False


def test_plan_omits_default_config_flag(monkeypatch, capsys) -> None:
    default = Path('/tmp/default-config.toml')
    monkeypatch.setattr(
        'aivm.cli.help._cfg_path',
        lambda p: default if p is None else Path(p),
    )
    PlanCLI.main(argv=False, config=None, yes=True)
    out = capsys.readouterr().out
    assert '--config' not in out
    assert 'aivm config init' in out
    assert f'Config: {default}' in out


def test_plan_includes_nondefault_config_flag(monkeypatch, capsys) -> None:
    default = Path('/tmp/default-config.toml')
    custom = Path('/tmp/custom-config.toml')
    monkeypatch.setattr(
        'aivm.cli.help._cfg_path',
        lambda p: default if p is None else Path(p),
    )
    PlanCLI.main(argv=False, config=str(custom), yes=True)
    out = capsys.readouterr().out
    assert 'aivm config init' in out
    assert f'--config {custom}' in out


def test_confirm_sudo_block_yes_only_checks_passwordless_probe(
    monkeypatch,
) -> None:
    monkeypatch.setattr('aivm.cli._common.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.cli._common.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('aivm.cli._common._SUDO_VALIDATED', False)
    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        from aivm.util import CmdResult

        return CmdResult(0, '', '')

    monkeypatch.setattr(
        'aivm.cli._common.run_cmd',
        fake_run_cmd,
    )
    _confirm_sudo_block(yes=True, purpose='test')
    assert calls == [['sudo', '-n', 'true']]


def test_confirm_sudo_block_confirmed_skips_sudo_validate_when_passwordless(
    monkeypatch,
) -> None:
    monkeypatch.setattr('aivm.cli._common.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.cli._common.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('aivm.cli._common._SUDO_VALIDATED', False)
    monkeypatch.setattr('builtins.input', lambda _: 'y')

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd == ['sudo', '-n', 'true']:
            from aivm.util import CmdResult

            return CmdResult(0, '', '')
        raise AssertionError(f'Unexpected command: {cmd!r}')

    monkeypatch.setattr('aivm.cli._common.run_cmd', fake_run_cmd)
    _confirm_sudo_block(yes=False, purpose='test')
    assert calls == [['sudo', '-n', 'true']]


def test_help_raw_outputs_direct_system_commands(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    cfg_path = tmp_path / 'config.toml'
    store = Store()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-raw'
    cfg.network.name = 'net-raw'
    cfg.firewall.table = 'fw-raw'
    upsert_vm(store, cfg)
    save_store(store, cfg_path)
    monkeypatch.setattr('aivm.cli.help._cfg_path', lambda p: cfg_path)
    rc = HelpRawCLI.main(argv=False, config=str(cfg_path), yes=True)
    assert rc == 0
    out = capsys.readouterr().out
    # strip ansi to remove colors
    clean = re.sub(r'\x1b\[[0-9;]*m', '', out)
    assert 'aivm help raw' in clean
    assert 'sudo virsh dominfo vm-raw' in clean
    assert 'sudo virsh net-info net-raw' in clean
    assert 'sudo nft list table inet fw-raw' in clean
