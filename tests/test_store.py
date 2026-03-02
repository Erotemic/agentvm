"""Tests for test store."""

from __future__ import annotations

from pathlib import Path

import pytest

from aivm.config import AgentVMConfig
from aivm.store import (
    AttachmentEntry,
    Store,
    find_attachment,
    find_vm,
    load_store,
    save_store,
    upsert_attachment,
    upsert_vm,
)


def test_store_roundtrip(tmp_path: Path) -> None:
    store = Store()
    store.defaults = AgentVMConfig()
    store.defaults.vm.cpus = 2
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-b'
    upsert_vm(store, cfg)
    cfg.vm.name = 'vm-a'
    upsert_vm(store, cfg)
    store.attachments.append(
        AttachmentEntry(host_path='/tmp/z', vm_name='vm-b', mode='shared')
    )
    store.attachments.append(
        AttachmentEntry(host_path='/tmp/a', vm_name='vm-a', mode='shared')
    )
    fpath = tmp_path / 'config.toml'
    save_store(store, fpath)

    loaded = load_store(fpath)
    assert loaded.defaults is not None
    assert loaded.defaults.vm.cpus == 2
    assert [v.name for v in loaded.vms] == ['vm-a', 'vm-b']
    assert [a.host_path for a in loaded.attachments] == ['/tmp/a', '/tmp/z']
    assert find_vm(loaded, 'vm-a') is not None
    assert find_vm(loaded, 'missing') is None


def test_upsert_attachment_conflict_and_force(tmp_path: Path) -> None:
    store = Store()
    host = tmp_path / 'project'
    host.mkdir()
    upsert_attachment(store, host_path=host, vm_name='vm1')
    with pytest.raises(RuntimeError):
        upsert_attachment(store, host_path=host, vm_name='vm2')
    upsert_attachment(store, host_path=host, vm_name='vm2', force=True)
    att = find_attachment(store, host)
    assert att is not None
    assert att.vm_name == 'vm2'
