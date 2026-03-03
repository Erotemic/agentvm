"""Tests for test config."""

from __future__ import annotations

from pathlib import Path

from aivm.config import AgentVMConfig, dump_toml, load, save


def test_dump_load_roundtrip(tmp_path: Path) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'my "vm"'
    cfg.paths.state_dir = '~/code/${USER}/state'
    cfg.sync.paths = ['~/.gitconfig', '/tmp/"quoted".txt']
    cfg.verbosity = 3
    fpath = tmp_path / '.aivm.toml'
    save(fpath, cfg)

    cfg2 = load(fpath)
    assert cfg2.vm.name == cfg.vm.name
    assert cfg2.paths.state_dir == cfg.paths.state_dir
    assert cfg2.sync.paths == cfg.sync.paths
    assert cfg2.verbosity == 3


def test_dump_toml_verbosity_default_omitted() -> None:
    cfg = AgentVMConfig()
    text = dump_toml(cfg)
    assert 'verbosity =' not in text


def test_expanded_paths_expands_env(monkeypatch) -> None:
    monkeypatch.setenv('AIVM_TEST_DIR', '/tmp/aivm-x')
    cfg = AgentVMConfig()
    cfg.paths.state_dir = '$AIVM_TEST_DIR/state'
    cfg.paths.ssh_identity_file = '$AIVM_TEST_DIR/id_ed25519'
    out = cfg.expanded_paths()
    assert out.paths.state_dir == '/tmp/aivm-x/state'
    assert out.paths.ssh_identity_file == '/tmp/aivm-x/id_ed25519'
