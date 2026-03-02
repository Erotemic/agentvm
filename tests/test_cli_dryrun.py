"""Tests for test cli dryrun."""

from __future__ import annotations

from pathlib import Path

from aivm.cli import AgentVMModalCLI
from aivm.config import AgentVMConfig
from aivm.store import Store, save_store, upsert_vm


def _write_cfg(tmp_path: Path) -> Path:
    cfg_path = tmp_path / 'config.toml'
    cfg = AgentVMConfig()
    cfg.paths.base_dir = str(tmp_path / 'libvirt')
    cfg.paths.state_dir = str(tmp_path / 'state')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.paths.ssh_pubkey_path = str(tmp_path / 'id_ed25519.pub')
    store = Store()
    upsert_vm(store, cfg)
    save_store(store, cfg_path)
    return cfg_path


def _run(argv: list[str]) -> int:
    rc = AgentVMModalCLI.main(argv=argv, _noexit=True)
    return 0 if rc is None else int(rc)


def test_dryrun_commands_with_yes(tmp_path: Path) -> None:
    cfg_path = _write_cfg(tmp_path)
    commands = [
        ['help', 'plan', '--yes', '--config', str(cfg_path)],
        ['help', 'tree', '--yes', '--config', str(cfg_path)],
        [
            'host',
            'net',
            'create',
            '--yes',
            '--dry_run',
            '--config',
            str(cfg_path),
        ],
        [
            'host',
            'net',
            'destroy',
            '--yes',
            '--dry_run',
            '--config',
            str(cfg_path),
        ],
        [
            'host',
            'fw',
            'apply',
            '--yes',
            '--dry_run',
            '--config',
            str(cfg_path),
        ],
        [
            'host',
            'fw',
            'remove',
            '--yes',
            '--dry_run',
            '--config',
            str(cfg_path),
        ],
        ['vm', 'wait_ip', '--yes', '--dry_run', '--config', str(cfg_path)],
        ['vm', 'destroy', '--yes', '--dry_run', '--config', str(cfg_path)],
        ['vm', 'provision', '--yes', '--dry_run', '--config', str(cfg_path)],
        [
            'vm',
            'sync_settings',
            '--yes',
            '--dry_run',
            '--config',
            str(cfg_path),
        ],
    ]
    for argv in commands:
        assert _run(argv) == 0


def test_help_tree_includes_one_line_descriptions(
    tmp_path: Path, capsys
) -> None:
    cfg_path = _write_cfg(tmp_path)
    assert _run(['help', 'tree', '--yes', '--config', str(cfg_path)]) == 0
    out = capsys.readouterr().out
    assert 'aivm help tree - Print the expanded aivm command tree.' in out
    assert (
        'aivm help raw - Print direct system-tool commands equivalent to common aivm checks.'
        in out
    )
    assert (
        'aivm vm ssh - SSH into the VM and start a shell in the mapped guest directory.'
        in out
    )
