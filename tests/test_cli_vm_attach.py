"""Tests for vm attach live-mount behavior."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from aivm.cli.vm import (
    ATTACHMENT_MODE_GIT,
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    ResolvedAttachment,
    VMAttachCLI,
    _ensure_shared_root_host_bind,
    _git_attachment_remote_name,
    _git_current_branch,
    _resolve_attachment,
    _upsert_host_git_remote,
)
from aivm.config import AgentVMConfig
from aivm.store import AttachmentEntry, Store, save_store
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
