"""Tests for test cli helpers."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import aivm.cli._common as common_mod
from aivm.cli._common import (
    _maybe_offer_create_ssh_identity,
)
from aivm.cli.help import HelpCompletionCLI, HelpRawCLI, PlanCLI
from aivm.cli.vm import (
    _auto_share_tag_for_path,
    _parse_sync_paths_arg,
    _upsert_ssh_config_entry,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.store import Store, save_store, upsert_attachment, upsert_vm


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


def test_cli_yes_sudo_defaults_from_config(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    store = Store()
    store.defaults = AgentVMConfig()
    store.behavior.yes_sudo = True
    save_store(store, cfg_path)
    monkeypatch.setattr('aivm.cli.help._cfg_path', lambda p: cfg_path)
    parsed = PlanCLI.cli(
        argv=False,
        data={'config': str(cfg_path), 'yes': False, 'yes_sudo': False},
    )
    assert bool(parsed.yes_sudo) is True


def test_cli_auto_approve_readonly_sudo_defaults_from_config(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    store = Store()
    store.defaults = AgentVMConfig()
    store.behavior.auto_approve_readonly_sudo = False
    save_store(store, cfg_path)
    monkeypatch.setattr('aivm.cli.help._cfg_path', lambda p: cfg_path)
    PlanCLI.cli(
        argv=False,
        data={'config': str(cfg_path), 'yes': False, 'yes_sudo': False},
    )
    assert common_mod._CURRENT_AUTO_APPROVE_READONLY_SUDO.get(True) is False


def test_cli_verbose_defaults_from_behavior_config(tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    store = Store()
    store.defaults = AgentVMConfig()
    store.defaults.verbosity = 2
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-verbose'
    cfg.verbosity = 2
    upsert_vm(store, cfg)
    store.active_vm = cfg.vm.name
    store.behavior.verbose = 4
    save_store(store, cfg_path)
    assert common_mod._resolve_cfg_verbosity(str(cfg_path)) == 4


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


def test_help_completion_outputs_bash_setup(capsys) -> None:
    rc = HelpCompletionCLI.main(argv=False, shell='bash', yes=True)
    assert rc == 0
    out = capsys.readouterr().out
    clean = re.sub(r'\x1b\[[0-9;]*m', '', out)
    assert 'aivm help completion' in clean
    assert 'python -m pip install argcomplete' in clean
    assert 'register-python-argcomplete aivm' in clean
    assert 'activate-global-python-argcomplete' in clean


def test_help_completion_rejects_unknown_shell() -> None:
    with pytest.raises(RuntimeError, match='--shell must be one of'):
        HelpCompletionCLI.main(argv=False, shell='tcsh', yes=True)


def test_hydrate_runtime_defaults_skips_detection_when_paths_already_set(
    monkeypatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.paths.ssh_identity_file = '/tmp/id_existing'
    cfg.paths.ssh_pubkey_path = '/tmp/id_existing.pub'
    monkeypatch.setattr(
        'aivm.cli._common.detect_ssh_identity',
        lambda: (_ for _ in ()).throw(
            AssertionError('detect_ssh_identity should not be called')
        ),
    )
    assert common_mod._hydrate_runtime_defaults(cfg) is False


def test_resolve_vm_name_prefers_active_vm_for_multi_attached_folder(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    vm1 = AgentVMConfig()
    vm1.vm.name = 'vm-a'
    vm2 = AgentVMConfig()
    vm2.vm.name = 'vm-b'
    upsert_vm(store, vm1)
    upsert_vm(store, vm2)
    store.active_vm = 'vm-b'
    upsert_attachment(store, host_path=host_src, vm_name='vm-a')
    upsert_attachment(store, host_path=host_src, vm_name='vm-b')
    save_store(store, cfg_path)

    monkeypatch.setattr('aivm.cli._common._cfg_path', lambda p: cfg_path)
    vm_name, resolved = common_mod._resolve_vm_name(
        config_opt=str(cfg_path),
        vm_opt='',
        host_src=host_src,
    )
    assert vm_name == 'vm-b'
    assert resolved == cfg_path


def test_resolve_vm_name_errors_noninteractive_for_multi_attached_folder(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    vm1 = AgentVMConfig()
    vm1.vm.name = 'vm-a'
    vm2 = AgentVMConfig()
    vm2.vm.name = 'vm-b'
    upsert_vm(store, vm1)
    upsert_vm(store, vm2)
    store.active_vm = ''
    upsert_attachment(store, host_path=host_src, vm_name='vm-a')
    upsert_attachment(store, host_path=host_src, vm_name='vm-b')
    save_store(store, cfg_path)

    monkeypatch.setattr('aivm.cli._common._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr('sys.stdin.isatty', lambda: False)
    with pytest.raises(RuntimeError, match='attached to multiple VMs'):
        common_mod._resolve_vm_name(
            config_opt=str(cfg_path),
            vm_opt='',
            host_src=host_src,
        )


def test_maybe_offer_create_ssh_identity_generates_distinct_aivm_key(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    fake_home = tmp_path / 'home'
    ssh_dir = fake_home / '.ssh'
    calls: list[list[str]] = []

    CommandManager.activate(CommandManager(yes=True))
    monkeypatch.setattr(
        common_mod.Path, 'home', staticmethod(lambda: fake_home)
    )
    monkeypatch.setattr(
        'aivm.cli._common.which', lambda cmd: '/usr/bin/ssh-keygen'
    )

    class Proc:
        def __init__(self):
            self.returncode = 0
            self.stdout = ''
            self.stderr = ''

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(c) for c in cmd]
        calls.append(normalized)
        if normalized[:2] == ['mkdir', '-p']:
            ssh_dir.mkdir(parents=True, exist_ok=True)
            return Proc()
        if normalized[:2] == ['chmod', '700']:
            return Proc()
        if normalized[:4] == ['ssh-keygen', '-q', '-t', 'ed25519']:
            key_path = Path(normalized[5])
            key_path.parent.mkdir(parents=True, exist_ok=True)
            key_path.write_text('PRIVATE', encoding='utf-8')
            Path(str(key_path) + '.pub').write_text('PUBLIC', encoding='utf-8')
            return Proc()
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    changed = _maybe_offer_create_ssh_identity(
        cfg,
        yes=True,
        prompt_reason='test prompt',
    )

    assert changed is True
    assert cfg.paths.ssh_identity_file == str(ssh_dir / 'id_aivm_ed25519')
    assert cfg.paths.ssh_pubkey_path == str(ssh_dir / 'id_aivm_ed25519.pub')
    assert any(
        cmd[:4] == ['ssh-keygen', '-q', '-t', 'ed25519'] for cmd in calls
    )
