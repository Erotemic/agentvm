"""Tests for top-level status CLI behavior."""

from __future__ import annotations

import importlib
from pathlib import Path

from aivm.cli.main import StatusCLI
from aivm.config import AgentVMConfig
from aivm.status import ProbeOutcome, render_global_status
from aivm.store import Store

main_mod = importlib.import_module('aivm.cli.main')


def test_status_cli_uses_vm_opt_and_sudo(monkeypatch, tmp_path: Path) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'chosen-vm'
    cfg_path = tmp_path / 'config.toml'

    called: dict[str, object] = {}

    def fake_load_cfg_with_path(config, vm_opt=''):
        called['vm_opt'] = vm_opt
        return cfg, cfg_path

    monkeypatch.setattr(
        main_mod,
        '_load_cfg_with_path',
        fake_load_cfg_with_path,
    )
    monkeypatch.setattr(main_mod, '_cfg_path', lambda _: cfg_path)
    monkeypatch.setattr(
        main_mod,
        '_confirm_sudo_block',
        lambda **k: called.setdefault('sudo', k),
    )
    monkeypatch.setattr(
        main_mod,
        'render_status',
        lambda cfg_arg, path_arg, *, detail, use_sudo: (
            called.setdefault(
                'render', (cfg_arg.vm.name, path_arg, detail, use_sudo)
            )
            or 'status'
        ),
    )

    rc = StatusCLI.main(
        argv=False,
        config=str(cfg_path),
        vm='chosen-vm',
        sudo=True,
        detail=False,
        yes=False,
    )
    assert rc == 0
    assert called['vm_opt'] == 'chosen-vm'
    assert called['sudo']['purpose'].startswith(
        'Inspect host/libvirt/firewall/VM state'
    )
    assert called['render'] == ('chosen-vm', cfg_path, False, True)


def test_render_global_status_wording(monkeypatch) -> None:
    monkeypatch.setattr('aivm.status.check_commands', lambda: ([], []))
    monkeypatch.setattr(
        'aivm.status.probe_runtime_environment',
        lambda: ProbeOutcome(True, 'ok', ''),
    )
    monkeypatch.setattr('aivm.status.store_path', lambda: 'dummy.toml')
    monkeypatch.setattr('aivm.status.load_store', lambda _: Store())
    text = render_global_status()
    assert 'No VM context resolved for this directory.' in text
