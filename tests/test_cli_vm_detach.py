"""Tests for explicit vm detach behavior."""

from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from aivm.cli.vm import AttachmentMode, VMDetachCLI
from aivm.config import AgentVMConfig
from aivm.status import ProbeOutcome
from aivm.store import AttachmentEntry, Store, find_attachment_for_vm


def test_vm_detach_shared_removes_store_and_detaches_mapping(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.SHARED,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code',
        lambda **kwargs: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm.load_store', lambda path: store)
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (ProbeOutcome(True, 'running'), True),
    )
    detached: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.detach_vm_share',
        lambda *a, **k: detached.append((a, k)) or True,
    )
    saved: list[Path] = []
    monkeypatch.setattr(
        'aivm.cli.vm.save_store',
        lambda reg, path, **kwargs: (saved.append(path) or path),
    )

    rc = VMDetachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
    )
    assert rc == 0
    assert len(detached) == 1
    assert saved == [cfg_path]
    assert find_attachment_for_vm(store, host_src, cfg.vm.name) is None


def test_vm_detach_git_only_updates_store(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.GIT,
            guest_dst='/workspace/repo',
            tag='',
        )
    )

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code',
        lambda **kwargs: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm.load_store', lambda path: store)
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (ProbeOutcome(False, 'shut off'), True),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.detach_vm_share',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('detach_vm_share should not be called for git mode')
        ),
    )
    saved: list[Path] = []
    monkeypatch.setattr(
        'aivm.cli.vm.save_store',
        lambda reg, path, **kwargs: (saved.append(path) or path),
    )

    rc = VMDetachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
    )
    assert rc == 0
    assert saved == [cfg_path]
    assert find_attachment_for_vm(store, host_src, cfg.vm.name) is None


def test_vm_detach_shared_root_unbinds_guest_and_host(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.SHARED_ROOT,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code',
        lambda **kwargs: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm.load_store', lambda path: store)
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (ProbeOutcome(True, 'running'), True),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: '10.77.0.42',
    )
    guest_detaches: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._detach_shared_root_guest_bind',
        lambda *a, **k: guest_detaches.append((a, k)) or None,
    )
    host_detaches: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._detach_shared_root_host_bind',
        lambda *a, **k: host_detaches.append((a, k)) or None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.detach_vm_share',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError(
                'detach_vm_share should not be called for shared-root mode'
            )
        ),
    )
    saved: list[Path] = []
    monkeypatch.setattr(
        'aivm.cli.vm.save_store',
        lambda reg, path, **kwargs: (saved.append(path) or path),
    )

    rc = VMDetachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
    )
    assert rc == 0
    assert len(guest_detaches) == 1
    assert len(host_detaches) == 1
    assert saved == [cfg_path]
    assert find_attachment_for_vm(store, host_src, cfg.vm.name) is None
