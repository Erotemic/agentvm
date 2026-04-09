"""Tests for persistent attachment manifest and reconcile orchestration."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from aivm.attachments.persistent import (
    _persistent_attachment_manifest_text,
    _persistent_host_manifest_path,
    _reconcile_persistent_attachments_in_guest,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.store import Store, save_store


def _activate_manager(
    monkeypatch: pytest.MonkeyPatch, *, yes_sudo: bool = True
) -> None:
    CommandManager.activate(CommandManager(yes_sudo=yes_sudo))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: False)


def test_persistent_manifest_persists_records_and_access_modes(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg_path = tmp_path / 'config.toml'
    store = Store()
    store.attachments.extend(
        [
            dict(
                host_path=str((tmp_path / 'proj-rw').resolve()),
                vm_name=cfg.vm.name,
                mode='persistent',
                access='rw',
                guest_dst='/workspace/rw',
                tag='hostcode-rw',
                host_lexical_path='',
            ),
            dict(
                host_path=str((tmp_path / 'proj-ro').resolve()),
                vm_name=cfg.vm.name,
                mode='persistent',
                access='ro',
                guest_dst='/workspace/ro',
                tag='hostcode-ro',
                host_lexical_path=str(tmp_path / 'link-ro'),
            ),
            dict(
                host_path=str((tmp_path / 'legacy').resolve()),
                vm_name=cfg.vm.name,
                mode='shared-root',
                access='rw',
                guest_dst='/workspace/legacy',
                tag='hostcode-legacy',
                host_lexical_path='',
            ),
        ]
    )
    # Store.attachments is a list of AttachmentEntry instances, but save_store
    # serializes plain dataclass instances; building via load/save keeps the
    # test close to the real store format.
    reg = Store()
    from aivm.store import AttachmentEntry

    reg.attachments = [AttachmentEntry(**item) for item in store.attachments]
    save_store(reg, cfg_path)

    payload = json.loads(_persistent_attachment_manifest_text(cfg, cfg_path))

    assert payload['vm_name'] == cfg.vm.name
    assert payload['shared_root_mount'] == '/mnt/aivm-persistent'
    assert [item['shared_root_token'] for item in payload['records']] == [
        'hostcode-ro',
        'hostcode-rw',
    ]
    assert [item['access'] for item in payload['records']] == ['ro', 'rw']
    assert payload['records'][0]['host_lexical_path'] == str(
        tmp_path / 'link-ro'
    )


def test_persistent_reconcile_syncs_host_manifest_then_replays_guest(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-reconcile'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg_path = tmp_path / 'config.toml'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: calls.append(('sync', a, k))
        or _persistent_host_manifest_path(cfg),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._install_persistent_attachment_replay',
        lambda *a, **k: calls.append(('install', a, k)) or None,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._run_guest_root_script',
        lambda *a, **k: calls.append(('replay', a, k)) or None,
    )

    _reconcile_persistent_attachments_in_guest(
        cfg,
        cfg_path,
        '10.0.0.5',
        dry_run=False,
    )

    assert [item[0] for item in calls] == ['sync', 'install', 'replay']
    assert (
        calls[-1][2]['summary']
        == 'Replay persistent attachment mounts inside guest'
    )
