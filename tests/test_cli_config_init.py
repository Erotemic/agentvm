"""Tests for interactive and non-interactive behavior of `aivm config init`."""

from __future__ import annotations

from pathlib import Path

import pytest

from aivm.cli.config import InitCLI
from aivm.config import AgentVMConfig
from typing import Any

def _fake_defaults_cfg(tmp_path: Path) -> AgentVMConfig:
    cfg = AgentVMConfig()
    cfg.vm.name = 'aivm-init-test'
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.paths.ssh_pubkey_path = str(tmp_path / 'id_ed25519.pub')
    return cfg


def test_config_init_noninteractive_requires_yes_or_defaults(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: False)
    with pytest.raises(RuntimeError, match='--yes or --defaults'):
        InitCLI.main(
            argv=False, config=str(cfg_path), yes=False, defaults=False
        )


def test_config_init_noninteractive_defaults_flag_bypasses_prompt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: False)
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=True
    )
    assert rc == 0
    assert cfg_path.exists()
    text = cfg_path.read_text(encoding='utf-8')
    assert '[defaults.vm]' in text
    assert 'name = "aivm-init-test"' in text
    assert '[[vms]]' not in text


def test_config_init_interactive_shows_summary_and_accepts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys : pytest.CaptureFixture[str]
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: '')
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=False
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert 'Detected defaults for `aivm config init`' in out
    assert 'vm.name:' not in out
    assert 'ssh-keygen -t ed25519' in out


def test_config_init_interactive_can_create_dedicated_aivm_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    fake_home = tmp_path / 'home'
    ssh_dir = fake_home / '.ssh'

    def fake_defaults_missing_keys(
        cfg: AgentVMConfig, project_dir: Path
    ) -> AgentVMConfig:
        del cfg, project_dir
        out = AgentVMConfig()
        out.vm.name = 'aivm-init-test'
        out.paths.ssh_identity_file = ''
        out.paths.ssh_pubkey_path = ''
        return out

    class Proc:
        def __init__(self) -> None:
            self.returncode = 0
            self.stdout = ''
            self.stderr = ''

    def fake_subprocess_run(cmd, **kwargs : Any) -> Proc:
        del kwargs
        normalized = [str(c) for c in cmd]
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

    answers = iter(['', ''])
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults', fake_defaults_missing_keys
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('aivm.cli._common.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    monkeypatch.setattr(
        'aivm.cli._common.which', lambda cmd: '/usr/bin/ssh-keygen'
    )
    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    monkeypatch.setattr(
        'aivm.cli._common.Path.home', staticmethod(lambda: fake_home)
    )

    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=False
    )

    assert rc == 0
    text = cfg_path.read_text(encoding='utf-8')
    assert 'id_aivm_ed25519' in text


def test_config_init_defaults_warns_when_ssh_keys_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: False)
    log_calls = []
    monkeypatch.setattr(
        'aivm.cli.config.log.warning',
        lambda *a, **k: log_calls.append((a, k)),
    )
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=True
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert 'SSH keypair not detected' in out
    assert 'ssh-keygen -t ed25519' in out
    assert log_calls


def test_config_init_prompt_mentions_edit_shortcut(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: True)
    prompts = []

    def fake_input(prompt: str) -> str:
        prompts.append(prompt)
        return ''

    monkeypatch.setattr('builtins.input', fake_input)
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=False
    )
    assert rc == 0
    assert any('(e=edit)' in p for p in prompts)


def test_config_init_interactive_edit_updates_hardware(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr('aivm.cli.config.sys.stdin.isatty', lambda: True)
    answers = iter(
        [
            'e',  # use edit flow
            '',  # vm.user
            '2',  # vm.cpus
            '3072',  # vm.ram_mb
            '24',  # vm.disk_gb
            '',  # network.name
            '',  # network.subnet_cidr
            '',  # network.gateway_ip
            '',  # network.dhcp_start
            '',  # network.dhcp_end
            '',  # paths.ssh_identity_file
            '',  # paths.ssh_pubkey_path
            'y',  # confirm
        ]
    )
    monkeypatch.setattr('builtins.input', lambda _: next(answers))
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=False
    )
    assert rc == 0
    text = cfg_path.read_text(encoding='utf-8')
    assert 'cpus = 2' in text
    assert 'ram_mb = 3072' in text
    assert 'disk_gb = 24' in text


def test_config_init_logs_resource_warnings_from_shared_checker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    monkeypatch.setattr('aivm.cli.config._cfg_path', lambda p: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.config.auto_defaults',
        lambda cfg, project_dir: _fake_defaults_cfg(tmp_path),
    )
    monkeypatch.setattr(
        'aivm.cli.config.vm_resource_warning_lines',
        lambda cfg: ['resource warning test'],
    )
    logged = []
    monkeypatch.setattr(
        'aivm.cli.config.log.warning',
        lambda *a, **k: logged.append((a, k)),
    )
    rc = InitCLI.main(
        argv=False, config=str(cfg_path), yes=False, defaults=True
    )
    assert rc == 0
    assert any('resource warning test' in str(args) for args, _ in logged)
