"""Tests for vm attach live-mount behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aivm.cli.vm import (
    ATTACHMENT_ACCESS_RO,
    ATTACHMENT_ACCESS_RW,
    ATTACHMENT_MODE_GIT,
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    ResolvedAttachment,
    VMAttachCLI,
    _ensure_shared_root_host_bind,
    _ensure_shared_root_guest_bind,
    _git_attachment_remote_name,
    _git_current_branch,
    _record_attachment,
    _resolve_attachment,
    _upsert_host_git_remote,
)
from aivm.config import AgentVMConfig
from aivm.store import (
    AttachmentEntry,
    Store,
    save_store,
    upsert_attachment,
    upsert_network,
    upsert_vm_with_network,
)
from aivm.status import ProbeOutcome
from aivm.util import CmdResult


def test_vm_attach_mounts_share_when_vm_running(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-running'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm._record_vm', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (ProbeOutcome(True, 'vm-running state=running'), True),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_share_mappings', lambda *a, **k: [])

    attached: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.attach_vm_share',
        lambda *a, **k: attached.append((a, k)),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )

    resolved: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: (resolved.append((a, k)) or '10.77.0.55'),
    )

    mounted: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted',
        lambda *a, **k: mounted.append((a, k)),
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
    )
    assert rc == 0
    assert attached
    assert resolved
    assert len(mounted) == 1
    args, kwargs = mounted[0]
    assert args[1] == '10.77.0.55'
    assert kwargs['guest_dst'] == '/workspace/proj'
    assert kwargs['tag'] == 'hostcode-proj'


def test_vm_attach_skips_guest_mount_when_vm_not_running(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-stopped'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm._record_vm', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (
            ProbeOutcome(False, 'vm-stopped state=shut off'),
            True,
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_share_mappings', lambda *a, **k: [])
    monkeypatch.setattr('aivm.cli.vm.attach_vm_share', lambda *a, **k: None)
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('_resolve_ip_for_ssh_ops should not be called')
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('ensure_share_mounted should not be called')
        ),
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
    )
    assert rc == 0


def test_vm_attach_escalates_when_nonsudo_probe_inconclusive(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-needs-sudo'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm._record_vm', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: attachment,
    )
    sudo_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block',
        lambda **kwargs: sudo_calls.append(kwargs),
    )

    states = [
        (ProbeOutcome(None, 'probe inconclusive without sudo'), False),
        (ProbeOutcome(True, 'vm-needs-sudo state=running'), True),
    ]
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: states.pop(0),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_share_mappings', lambda *a, **k: [])

    attached: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.attach_vm_share',
        lambda *a, **k: attached.append((a, k)),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: '10.77.0.77',
    )

    mounted: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted',
        lambda *a, **k: mounted.append((a, k)),
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=False,
    )
    assert rc == 0
    assert sudo_calls
    assert attached
    assert mounted


def test_resolve_attachment_uses_saved_git_mode(
    tmp_path: Path,
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
            mode=ATTACHMENT_MODE_GIT,
            guest_dst='/workspace/repo',
            tag='ignored-for-git',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == ATTACHMENT_MODE_GIT
    assert resolved.guest_dst == '/workspace/repo'
    assert resolved.tag == ''


def test_resolve_attachment_defaults_to_shared_root_for_new_folder(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-default'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == ATTACHMENT_MODE_SHARED_ROOT
    assert resolved.tag


def test_resolve_attachment_reuses_saved_shared_mode_when_mode_omitted(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-existing'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=ATTACHMENT_MODE_SHARED,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == ATTACHMENT_MODE_SHARED
    assert resolved.guest_dst == '/workspace/proj'
    assert resolved.tag == 'hostcode-proj'


def test_resolve_attachment_reuses_saved_access_when_access_omitted(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-access-existing'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=ATTACHMENT_MODE_SHARED,
            access=ATTACHMENT_ACCESS_RO,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == ATTACHMENT_MODE_SHARED
    assert resolved.access == ATTACHMENT_ACCESS_RO


def test_resolve_attachment_rejects_mode_change_for_existing_attachment(
    tmp_path: Path,
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
            mode=ATTACHMENT_MODE_SHARED,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    try:
        _resolve_attachment(cfg, cfg_path, host_src, '', 'git')
    except RuntimeError as ex:
        msg = str(ex)
    else:
        raise AssertionError('Expected mode-mismatch RuntimeError')

    assert 'Attachment mode mismatch' in msg
    assert 'detach + reattach' in msg


def test_resolve_attachment_rejects_access_change_for_existing_attachment(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-access'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=ATTACHMENT_MODE_SHARED,
            access=ATTACHMENT_ACCESS_RW,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    with pytest.raises(RuntimeError, match='Attachment access mismatch'):
        _resolve_attachment(
            cfg,
            cfg_path,
            host_src,
            '',
            '',
            ATTACHMENT_ACCESS_RO,
        )


def test_resolve_attachment_accepts_ro_for_shared_root_mode(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-ro-shared-root'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(
        cfg,
        cfg_path,
        host_src,
        '',
        ATTACHMENT_MODE_SHARED_ROOT,
        ATTACHMENT_ACCESS_RO,
    )
    assert resolved.mode == ATTACHMENT_MODE_SHARED_ROOT
    assert resolved.access == ATTACHMENT_ACCESS_RO


def test_resolve_attachment_ro_not_implemented_for_git_mode(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-ro-mode'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    with pytest.raises(
        NotImplementedError,
        match='Read-only attachments are currently only implemented',
    ):
        _resolve_attachment(
            cfg,
            cfg_path,
            host_src,
            '',
            ATTACHMENT_MODE_GIT,
            ATTACHMENT_ACCESS_RO,
        )


def test_vm_attach_shared_root_running_ensures_guest_ready(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm._record_vm', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (
            ProbeOutcome(True, 'vm-shared-root state=running'),
            True,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )

    host_bind_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_shared_root_host_bind',
        lambda *a, **k: host_bind_calls.append((a, k)) or Path('/tmp/token'),
    )
    vm_mapping_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_shared_root_vm_mapping',
        lambda *a, **k: vm_mapping_calls.append((a, k)) or None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: '10.77.0.99',
    )
    guest_ready_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_attachment_available_in_guest',
        lambda *a, **k: guest_ready_calls.append((a, k)) or None,
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        mode='shared-root',
        yes=True,
    )

    assert rc == 0
    assert len(host_bind_calls) == 1
    assert len(vm_mapping_calls) == 1
    assert len(guest_ready_calls) == 1
    _, guest_kwargs = guest_ready_calls[0]
    assert guest_kwargs['ensure_shared_root_host_side'] is False


def test_shared_root_host_bind_does_not_unmount_when_target_not_mountpoint(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        cmd = [str(part) for part in cmd]
        calls.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mountpoint', '-q']:
            return CmdResult(1, '', '')
        if cmd[:2] == ['mount', '--bind']:
            return CmdResult(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(line.startswith('mountpoint -q') for line in command_text)
    assert any(line.startswith('mount --bind') for line in command_text)
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('findmnt ') for line in command_text)


def test_shared_root_host_bind_accepts_findmnt_bind_subpath_source(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-existing'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        cmd = [str(part) for part in cmd]
        calls.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mountpoint', '-q']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['findmnt', '-n']:
            return CmdResult(0, f'{source_dir}[/sub]\n', '')
        if cmd[:2] == ['umount', str(source_dir)]:
            raise AssertionError('unexpected source-path umount')
        if cmd[:2] == ['umount', '-l']:
            raise AssertionError('unexpected lazy umount')
        if cmd[:2] == ['mount', '--bind']:
            raise AssertionError('unexpected remount for same source')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(line.startswith('mountpoint -q') for line in command_text)
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_accepts_findmnt_device_subpath_source(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-device-subpath'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        cmd = [str(part) for part in cmd]
        calls.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mountpoint', '-q']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['findmnt', '-n']:
            return CmdResult(0, f'/dev/vda1[{source_dir}]\n', '')
        if cmd[:2] == ['umount', str(source_dir)]:
            raise AssertionError('unexpected source-path umount')
        if cmd[:2] == ['umount', '-l']:
            raise AssertionError('unexpected lazy umount')
        if cmd[:2] == ['mount', '--bind']:
            raise AssertionError('unexpected remount for same source')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(line.startswith('mountpoint -q') for line in command_text)
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_lazy_unmounts_busy_target(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-busy'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    calls: list[list[str]] = []
    target = (
        Path(cfg.paths.base_dir)
        / cfg.vm.name
        / 'shared-root'
        / attachment.tag
    )

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        cmd = [str(part) for part in cmd]
        calls.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mountpoint', '-q']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['findmnt', '-n']:
            return CmdResult(0, '/other/source\n', '')
        if cmd == ['umount', str(target)]:
            return CmdResult(32, '', 'umount: target is busy')
        if cmd == ['umount', '-l', str(target)]:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mount', '--bind']:
            return CmdResult(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(line == f'umount {target}' for line in command_text)
    assert any(line == f'umount -l {target}' for line in command_text)
    assert any(line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_refuses_disruptive_rebind_when_disabled(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-safe-restore'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        cmd = [str(part) for part in cmd]
        calls.append(cmd)
        if cmd[:2] == ['mkdir', '-p']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['mountpoint', '-q']:
            return CmdResult(0, '', '')
        if cmd[:2] == ['findmnt', '-n']:
            return CmdResult(0, '/other/source\n', '')
        if cmd[0] == 'umount':
            raise AssertionError('unexpected unmount in non-disruptive mode')
        if cmd[:2] == ['mount', '--bind']:
            raise AssertionError('unexpected bind remount in non-disruptive mode')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    with pytest.raises(RuntimeError, match='Refusing to replace existing'):
        _ensure_shared_root_host_bind(
            cfg,
            attachment,
            yes=True,
            dry_run=False,
            allow_disruptive_rebind=False,
        )

    command_text = [' '.join(c) for c in calls]
    assert any(line.startswith('mountpoint -q') for line in command_text)
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_guest_bind_read_only_sets_bind_remount_ro(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-ro'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_SHARED_ROOT,
        access=ATTACHMENT_ACCESS_RO,
        source_dir=str((tmp_path / 'source').resolve()),
        guest_dst='/workspace/source',
        tag='token-source',
    )

    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted',
        lambda *a, **k: None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )
    cmds: list[list[str]] = []
    run_kwargs: list[dict] = []

    def fake_run_cmd(cmd, **kwargs):
        cmds.append([str(c) for c in cmd])
        run_kwargs.append(dict(kwargs))
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)

    _ensure_shared_root_guest_bind(
        cfg,
        '10.0.0.2',
        attachment,
        dry_run=False,
    )

    assert len(cmds) == 1
    remote_script = cmds[0][-1]
    assert run_kwargs[0]['timeout'] == 20
    assert 'sudo -n mount --bind' in remote_script
    assert 'mount -o remount,bind,ro' in remote_script
    assert 'umount -l' in remote_script
    assert 'findmnt -n -o ROOT --target' in remote_script
    assert 'stat -Lc %d:%i' in remote_script
    assert 'shared-root bind verification failed: unexpected source' in remote_script
    assert 'shared-root bind verification failed: unexpected mount options' in remote_script


def test_resolve_attachment_git_defaults_to_guest_home_path(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', 'git')

    assert resolved.mode == ATTACHMENT_MODE_GIT
    assert resolved.guest_dst.startswith('/home/agent/')
    assert resolved.guest_dst.endswith('/repo')


def test_resolve_attachment_git_migrates_legacy_host_mirror_guest_dst(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    source_abs = str(host_src.resolve())

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=source_abs,
            vm_name=cfg.vm.name,
            mode=ATTACHMENT_MODE_GIT,
            guest_dst=source_abs,
            tag='',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == ATTACHMENT_MODE_GIT
    assert resolved.guest_dst != source_abs
    assert resolved.guest_dst.startswith('/home/agent/')


def test_vm_attach_git_mode_syncs_guest_repo_when_running(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=ATTACHMENT_MODE_GIT,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/repo',
        tag='',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, cfg_path),
    )
    monkeypatch.setattr('aivm.cli.vm._record_vm', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm.probe_vm_state',
        lambda *a, **k: (ProbeOutcome(True, 'vm-git state=running'), True),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: '10.77.0.88',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.vm_share_mappings',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('vm_share_mappings should not be called in git mode')
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.attach_vm_share',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('attach_vm_share should not be called in git mode')
        ),
    )

    sync_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_git_clone_attachment',
        lambda *a, **k: sync_calls.append((a, k)) or (host_src, 'ssh', 'git'),
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        mode='git',
        yes=True,
    )
    assert rc == 0
    assert len(sync_calls) == 1


def test_git_current_branch_returns_named_branch(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()

    monkeypatch.setattr(
        'aivm.cli.vm.run_cmd',
        lambda *a, **k: CmdResult(0, 'feature-x\n', ''),
    )

    branch = _git_current_branch(repo)
    assert branch == 'feature-x'


def test_git_current_branch_raises_on_git_error(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()

    monkeypatch.setattr(
        'aivm.cli.vm.run_cmd',
        lambda *a, **k: CmdResult(128, '', 'fatal: not a git repository'),
    )

    with pytest.raises(
        RuntimeError, match='Could not determine current Git branch'
    ):
        _git_current_branch(repo)


def test_upsert_host_git_remote_adds_remote(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(
        ['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(repo), 'config', 'user.name', 'Test User'],
        check=True,
        capture_output=True,
    )
    (repo / 'README').write_text('hello\n', encoding='utf-8')
    subprocess.run(
        ['git', '-C', str(repo), 'add', 'README'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(repo), 'commit', '-m', 'init'],
        check=True,
        capture_output=True,
    )

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    remote_name = _git_attachment_remote_name(cfg, repo)
    prompts: list[str] = []

    def _capture_prompt(**kwargs) -> None:
        prompts.append(kwargs['purpose'])

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_external_file_update',
        _capture_prompt,
    )
    _, updated = _upsert_host_git_remote(
        repo,
        remote_name=remote_name,
        remote_url='vm-git:/workspace/repo',
        yes=True,
    )

    assert updated is True
    assert prompts == [
        f"Register Git remote '{remote_name}' with URL 'vm-git:/workspace/repo'."
    ]
    probe = subprocess.run(
        ['git', '-C', str(repo), 'remote', 'get-url', remote_name],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == 'vm-git:/workspace/repo'


def test_upsert_host_git_remote_updates_remote_url(
    monkeypatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init', str(repo)], check=True, capture_output=True)
    subprocess.run(
        ['git', '-C', str(repo), 'config', 'user.email', 'test@example.com'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(repo), 'config', 'user.name', 'Test User'],
        check=True,
        capture_output=True,
    )
    (repo / 'README').write_text('hello\n', encoding='utf-8')
    subprocess.run(
        ['git', '-C', str(repo), 'add', 'README'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(repo), 'commit', '-m', 'init'],
        check=True,
        capture_output=True,
    )

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    remote_name = _git_attachment_remote_name(cfg, repo)
    subprocess.run(
        [
            'git',
            '-C',
            str(repo),
            'remote',
            'add',
            remote_name,
            'vm-git:/old/path',
        ],
        check=True,
        capture_output=True,
    )
    prompts: list[str] = []
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_external_file_update',
        lambda **kwargs: prompts.append(kwargs['purpose']),
    )
    _, updated = _upsert_host_git_remote(
        repo,
        remote_name=remote_name,
        remote_url='vm-git:/workspace/repo',
        yes=True,
    )

    assert updated is True
    assert prompts == [
        (
            f"Update Git remote '{remote_name}' URL from 'vm-git:/old/path' "
            "to 'vm-git:/workspace/repo'."
        )
    ]
    probe = subprocess.run(
        ['git', '-C', str(repo), 'remote', 'get-url', remote_name],
        check=True,
        capture_output=True,
        text=True,
    )
    assert probe.stdout.strip() == 'vm-git:/workspace/repo'


def test_upsert_host_git_remote_raises_on_invalid_repo(tmp_path: Path) -> None:
    repo = tmp_path / 'not-a-repo'
    repo.mkdir()

    with pytest.raises(RuntimeError, match='Could not locate Git config'):
        _upsert_host_git_remote(
            repo,
            remote_name='aivm-test',
            remote_url='vm-git:/workspace/repo',
            yes=True,
        )


def test_record_attachment_skips_save_when_unchanged(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    guest_dst = '/workspace/repo'

    reg = Store()
    upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
    upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
    upsert_attachment(
        reg,
        host_path=host_src,
        vm_name=cfg.vm.name,
        mode='git',
        guest_dst=guest_dst,
        tag='',
    )
    save_store(reg, cfg_path)

    save_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.save_store',
        lambda *a, **k: save_calls.append((a, k)) or cfg_path,
    )

    out = _record_attachment(
        cfg,
        cfg_path,
        host_src=host_src,
        mode='git',
        access=ATTACHMENT_ACCESS_RW,
        guest_dst=guest_dst,
        tag='',
    )
    assert out == cfg_path
    assert save_calls == []
