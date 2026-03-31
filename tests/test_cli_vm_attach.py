"""Tests for vm attach live-mount behavior."""

from __future__ import annotations

import builtins
import subprocess
from pathlib import Path
from typing import Any

import pytest

from aivm.cli.vm import (
    VMSSHCLI,
    VMAttachCLI,
    VMCodeCLI,
    AttachmentAccess,
    AttachmentMode,
    ResolvedAttachment,
    _apply_guest_derived_symlinks,
    _compute_mirror_home_symlink,
    _default_primary_guest_dst,
    _ensure_guest_symlink,
    _ensure_shared_root_guest_bind,
    _ensure_shared_root_host_bind,
    _git_attachment_remote_name,
    _git_current_branch,
    _host_symlink_lexical_path,
    _record_attachment,
    _resolve_attachment,
    _upsert_host_git_remote,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.status import ProbeOutcome
from aivm.store import (
    AttachmentEntry,
    Store,
    load_store,
    save_store,
    upsert_attachment,
    upsert_network,
    upsert_vm_with_network,
)
from aivm.util import CmdResult


def _activate_manager(
    monkeypatch: pytest.MonkeyPatch, *, yes_sudo: bool = True
) -> None:
    CommandManager.activate(CommandManager(yes_sudo=yes_sudo))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: False)


class _Proc:
    def __init__(
        self, returncode: int = 0, stdout: str = '', stderr: str = ''
    ) -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _capture_command_logs(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    messages: list[str] = []

    class _FakeLog:
        def info(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args))

        def debug(self, fmt: str, *args: Any) -> None:
            return None

        def trace(self, fmt: str, *args: Any) -> None:
            return None

        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args))

        def error(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args))

    monkeypatch.setattr('aivm.commands.log.opt', lambda **kwargs: _FakeLog())
    return messages


def test_vm_attach_mounts_share_when_vm_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-running'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-stopped'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-needs-sudo'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
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
            mode=AttachmentMode.GIT,
            guest_dst='/workspace/repo',
            tag='ignored-for-git',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == AttachmentMode.GIT
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

    assert resolved.mode == AttachmentMode.SHARED_ROOT
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
            mode=AttachmentMode.SHARED,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == AttachmentMode.SHARED
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
            mode=AttachmentMode.SHARED,
            access=AttachmentAccess.RO,
            guest_dst='/workspace/proj',
            tag='hostcode-proj',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == AttachmentMode.SHARED
    assert resolved.access == AttachmentAccess.RO


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
            mode=AttachmentMode.SHARED,
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
            mode=AttachmentMode.SHARED,
            access=AttachmentAccess.RW,
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
            AttachmentAccess.RO,
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
        AttachmentMode.SHARED_ROOT,
        AttachmentAccess.RO,
    )
    assert resolved.mode == AttachmentMode.SHARED_ROOT
    assert resolved.access == AttachmentAccess.RO


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
            AttachmentMode.GIT,
            AttachmentAccess.RO,
        )


def test_vm_attach_shared_root_running_ensures_guest_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
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
    assert len(host_bind_calls) == 0
    assert len(vm_mapping_calls) == 0
    assert len(guest_ready_calls) == 1
    _, guest_kwargs = guest_ready_calls[0]
    assert guest_kwargs['ensure_shared_root_host_side'] is True


def test_shared_root_host_bind_does_not_unmount_when_target_not_mountpoint(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        cmd = [str(part) for part in cmd]
        normalized = cmd[2:] if cmd[:2] == ['sudo', '-n'] else cmd
        calls.append(normalized)
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['mount', '--bind']:
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert any(line.startswith('mount --bind') for line in command_text)
    assert all(not line.startswith('umount ') for line in command_text)


def test_shared_root_host_bind_accepts_findmnt_bind_subpath_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-existing'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        cmd = [str(part) for part in cmd]
        normalized = cmd[2:] if cmd[:2] == ['sudo', '-n'] else cmd
        calls.append(normalized)
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(0, f'{source_dir}[/sub]\n', '')
        if normalized[:2] == ['umount', str(source_dir)]:
            raise AssertionError('unexpected source-path umount')
        if normalized[:2] == ['umount', '-l']:
            raise AssertionError('unexpected lazy umount')
        if normalized[:2] == ['mount', '--bind']:
            raise AssertionError('unexpected remount for same source')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_accepts_findmnt_device_subpath_source(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-device-subpath'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        cmd = [str(part) for part in cmd]
        normalized = cmd[2:] if cmd[:2] == ['sudo', '-n'] else cmd
        calls.append(normalized)
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(0, f'/dev/vda1[{source_dir}]\n', '')
        if normalized[:2] == ['umount', str(source_dir)]:
            raise AssertionError('unexpected source-path umount')
        if normalized[:2] == ['umount', '-l']:
            raise AssertionError('unexpected lazy umount')
        if normalized[:2] == ['mount', '--bind']:
            raise AssertionError('unexpected remount for same source')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_lazy_unmounts_busy_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-bind-busy'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []
    target = (
        Path(cfg.paths.base_dir) / cfg.vm.name / 'shared-root' / attachment.tag
    )

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        cmd = [str(part) for part in cmd]
        normalized = cmd[2:] if cmd[:2] == ['sudo', '-n'] else cmd
        calls.append(normalized)
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(0, '/other/source\n', '')
        if normalized[:2] == ['bash', '-lc']:
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    command_text = [' '.join(c) for c in calls]
    repair_cmd = next(
        line for line in command_text if line.startswith('bash -lc ')
    )
    assert f'umount {target}' in repair_cmd
    assert f'umount -l {target}' in repair_cmd
    assert f'mount --bind {source_dir}' in repair_cmd


def test_shared_root_host_bind_refuses_disruptive_rebind_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-safe-restore'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        cmd = [str(part) for part in cmd]
        normalized = cmd[2:] if cmd[:2] == ['sudo', '-n'] else cmd
        calls.append(normalized)
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(0, '/other/source\n', '')
        if normalized[0] == 'umount':
            raise AssertionError('unexpected unmount in non-disruptive mode')
        if normalized[:2] == ['mount', '--bind']:
            raise AssertionError(
                'unexpected bind remount in non-disruptive mode'
            )
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    with pytest.raises(RuntimeError, match='Refusing to replace existing'):
        _ensure_shared_root_host_bind(
            cfg,
            attachment,
            yes=True,
            dry_run=False,
            allow_disruptive_rebind=False,
        )

    command_text = [' '.join(c) for c in calls]
    assert any(
        line.startswith('findmnt -n -o SOURCE --target')
        for line in command_text
    )
    assert all(not line.startswith('umount ') for line in command_text)
    assert all(not line.startswith('mount --bind') for line in command_text)


def test_shared_root_host_bind_tolerates_not_mounted_during_repair(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-not-mounted'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch)
    calls: list[list[str]] = []

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        parts = [str(part) for part in cmd]
        normalized = parts[2:] if parts[:2] == ['sudo', '-n'] else parts
        calls.append(normalized)
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(0, '/dev/nvme0n1p1\n', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['bash', '-lc']:
            script = normalized[2]
            assert '"not mounted"' in script
            assert 'mount --bind' in script
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    target = _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )

    assert target.name == attachment.tag
    command_text = [' '.join(c) for c in calls]
    assert any(line.startswith('bash -lc ') for line in command_text)


def test_shared_root_guest_bind_read_only_sets_bind_remount_ro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-ro'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        access=AttachmentAccess.RO,
        source_dir=str((tmp_path / 'source').resolve()),
        guest_dst='/workspace/source',
        tag='token-source',
    )

    _activate_manager(monkeypatch)
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

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        cmds.append([str(c) for c in cmd])
        run_kwargs.append(dict(kwargs))
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_guest_bind(
        cfg,
        '10.0.0.2',
        attachment,
        dry_run=False,
    )

    assert len(cmds) == 2
    mount_script = cmds[0][-1]
    remote_script = cmds[1][-1]
    assert run_kwargs[0]['timeout'] == 20
    assert 'sudo -n mount -t virtiofs -o ro' in mount_script
    assert 'sudo -n mount --bind' in remote_script
    assert 'mount -o remount,bind,ro' in remote_script
    assert 'umount -l' in remote_script
    assert 'findmnt -n -o ROOT --target' in remote_script
    assert 'stat -Lc %d:%i' in remote_script
    assert '[ "$cur" = \'aivm-shared-root[/token-source]\' ]' in remote_script
    assert (
        '[ "$final_src" = \'aivm-shared-root[/token-source]\' ]'
        in remote_script
    )
    assert (
        'shared-root bind verification failed: unexpected source'
        in remote_script
    )
    assert (
        'shared-root bind verification failed: unexpected mount options'
        in remote_script
    )


def test_shared_root_host_bind_prompts_once_per_privileged_step(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-plan'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch, yes_sudo=False)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    messages = _capture_command_logs(monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(
        builtins,
        'input',
        lambda prompt: (prompts.append(prompt) or 'y'),
    )

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        parts = [str(part) for part in cmd]
        if parts[:3] == ['sudo', '-n', 'true']:
            return _Proc(1, '', 'sudo: a password is required')
        if parts[:2] == ['sudo', '-v']:
            return _Proc(0, '', '')
        normalized = parts[1:] if parts[:1] == ['sudo'] else parts
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['mount', '--bind']:
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=False,
        dry_run=False,
    )

    assert prompts == ['Approve this step? [y]es/[a]ll/[s]how/[N]o: ']
    assert 'Step: Inspect shared-root host bind state' in messages
    assert 'Step: Prepare host bind targets' in messages
    assert '  1. Create shared-root parent directory' in messages
    assert '  2. Create project-specific host bind target' in messages
    assert '  3. Bind requested host folder to shared-root target' in messages
    assert any(
        msg.startswith('     command: sudo mkdir -p ') for msg in messages
    )
    assert any(
        msg.startswith('     command: sudo mount --bind ') for msg in messages
    )


def test_shared_root_host_bind_autoapproves_readonly_findmnt_when_auth_cached(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-readonly'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )

    _activate_manager(monkeypatch, yes_sudo=False)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    messages = _capture_command_logs(monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(
        builtins,
        'input',
        lambda prompt: (prompts.append(prompt) or 'y'),
    )

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        parts = [str(part) for part in cmd]
        if parts[:3] == ['sudo', '-n', 'true']:
            return _Proc(0, '', '')
        normalized = parts[1:] if parts[:1] == ['sudo'] else parts
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['mount', '--bind']:
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=False,
        dry_run=False,
    )

    assert prompts == ['Approve this step? [y]es/[a]ll/[s]how/[N]o: ']
    assert 'Step: Inspect shared-root host bind state' in messages
    assert any(
        msg.startswith(
            '     command (read-only): sudo findmnt -n -o SOURCE --target '
        )
        for msg in messages
    )
    assert 'Step: Prepare host bind targets' in messages


def test_shared_root_vm_mapping_uses_named_steps_and_per_step_prompts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-map'
    cfg.paths.base_dir = str(tmp_path / 'base')

    _activate_manager(monkeypatch, yes_sudo=False)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    messages = _capture_command_logs(monkeypatch)
    prompts: list[str] = []
    monkeypatch.setattr(
        builtins,
        'input',
        lambda prompt: (prompts.append(prompt) or 'y'),
    )

    def fake_subprocess_run(cmd: list[str], **kwargs: Any) -> _Proc:
        del kwargs
        parts = [str(part) for part in cmd]
        if parts[:3] == ['sudo', '-n', 'true']:
            return _Proc(1, '', 'sudo: a password is required')
        if parts[:2] == ['sudo', '-v']:
            return _Proc(0, '', '')
        normalized = parts[1:] if parts[:1] == ['sudo'] else parts
        if normalized[:4] == ['virsh', '-c', 'qemu:///system', 'dumpxml']:
            return _Proc(1, '', 'domain not visible')
        if normalized[:2] == ['virsh', 'attach-device']:
            return _Proc(0, '', '')
        raise AssertionError(f'unexpected command: {cmd}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    from aivm.cli.vm import _ensure_shared_root_vm_mapping

    _ensure_shared_root_vm_mapping(
        cfg,
        yes=False,
        dry_run=False,
        vm_running=True,
    )

    assert prompts == ['Approve this step? [y]es/[a]ll/[s]how/[N]o: ']
    assert 'Step: Inspect shared-root VM mapping' in messages
    assert (
        'Step: Inspect shared-root VM mapping with libvirt privileges'
        in messages
    )
    assert 'Step: Ensure VM virtiofs mapping' in messages
    assert (
        '  1. Attach virtiofs device to running VM vm-shared-root-map'
        in messages
    )
    assert any(
        msg.startswith('     command: sudo virsh attach-device ')
        for msg in messages
    )


def test_shared_root_guest_bind_preview_uses_semantic_summaries(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-root-preview'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        access=AttachmentAccess.RW,
        source_dir=str((tmp_path / 'source').resolve()),
        guest_dst='/workspace/source',
        tag='token-source',
    )

    _activate_manager(monkeypatch)
    messages = _capture_command_logs(monkeypatch)
    monkeypatch.setattr(
        'aivm.cli.vm.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: _Proc(0, '', ''),
    )

    _ensure_shared_root_guest_bind(
        cfg,
        '10.0.0.2',
        attachment,
        dry_run=False,
    )

    assert 'Step: Mount and verify inside guest' in messages
    assert '  1. Mount shared-root inside guest' in messages
    assert (
        '  2. Bind guest destination to shared source and verify source/options'
        in messages
    )
    assert any(
        msg.startswith('     command: ssh -i /tmp/id_ed25519 agent@10.0.0.2 ')
        for msg in messages
    )
    assert all('set -euo pipefail; if [ ! -d' not in msg for msg in messages)


def test_resolve_attachment_git_defaults_to_exact_host_path(
    tmp_path: Path,
) -> None:
    """New behaviour: git mode defaults to the exact lexical host path, not guest-home-relative."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', 'git')

    assert resolved.mode == AttachmentMode.GIT
    # Default is now the exact (lexical absolute) host path, not guest-home-relative
    assert resolved.guest_dst == str(host_src.expanduser().absolute())


def test_resolve_attachment_git_preserves_saved_guest_dst(
    tmp_path: Path,
) -> None:
    """Existing saved guest_dst is preserved unchanged — no auto-migration occurs."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    saved_guest_dst = '/home/agent/code/repo'

    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.GIT,
            guest_dst=saved_guest_dst,
            tag='',
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')

    assert resolved.mode == AttachmentMode.GIT
    # Saved guest_dst is preserved; no migration to exact host path
    assert resolved.guest_dst == saved_guest_dst


def test_vm_attach_git_mode_syncs_guest_repo_when_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.GIT,
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()

    monkeypatch.setattr(
        'aivm.cli.vm.CommandManager.run',
        lambda self, *a, **k: CmdResult(0, 'feature-x\n', ''),
    )

    branch = _git_current_branch(repo)
    assert branch == 'feature-x'


def test_git_current_branch_raises_on_git_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / 'repo'
    repo.mkdir()

    monkeypatch.setattr(
        'aivm.cli.vm.CommandManager.run',
        lambda self, *a, **k: CmdResult(128, '', 'fatal: not a git repository'),
    )

    with pytest.raises(
        RuntimeError, match='Could not determine current Git branch'
    ):
        _git_current_branch(repo)


def test_upsert_host_git_remote_adds_remote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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

    def _capture_prompt(**kwargs: Any) -> None:
        prompts.append(kwargs['purpose'])

    monkeypatch.setattr(
        'aivm.cli.vm.CommandManager.confirm_file_update',
        lambda self, **kwargs: _capture_prompt(**kwargs),
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
        'aivm.cli.vm.CommandManager.confirm_file_update',
        lambda self, **kwargs: prompts.append(kwargs['purpose']),
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
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
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
        access=AttachmentAccess.RW,
        guest_dst=guest_dst,
        tag='',
    )
    assert out == cfg_path
    assert save_calls == []


def test_record_attachment_passes_reason_to_save_store(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'repo'
    host_src.mkdir()

    save_kwargs: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm.save_store',
        lambda *a, **k: save_kwargs.append(dict(k)) or cfg_path,
    )

    out = _record_attachment(
        cfg,
        cfg_path,
        host_src=host_src,
        mode='git',
        access=AttachmentAccess.RW,
        guest_dst='/workspace/repo',
        tag='',
    )

    assert out == cfg_path
    assert save_kwargs == [
        {
            'reason': (
                f'Persist attachment record for {host_src} on VM vm-git '
                '(mode=git, access=rw, guest_dst=/workspace/repo).'
            )
        }
    ]


# ---------------------------------------------------------------------------
# New tests for unified default guest destination, symlink helpers,
# mirror-home behavior, and tag generation.
# ---------------------------------------------------------------------------


# --- Default guest destination ---


def test_default_primary_guest_dst_non_symlink(tmp_path: Path) -> None:
    """Non-symlink path returns its lexical absolute form."""
    d = tmp_path / 'mydir'
    d.mkdir()
    result = _default_primary_guest_dst(d)
    assert result == str(d.expanduser().absolute())


def test_default_primary_guest_dst_symlink(tmp_path: Path) -> None:
    """Symlinked source returns the resolved real path."""
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    result = _default_primary_guest_dst(link)
    assert result == str(real.resolve())
    assert result != str(link)


def test_host_symlink_lexical_path_non_symlink(tmp_path: Path) -> None:
    d = tmp_path / 'dir'
    d.mkdir()
    assert _host_symlink_lexical_path(d) is None


def test_host_symlink_lexical_path_symlink(tmp_path: Path) -> None:
    real = tmp_path / 'real'
    real.mkdir()
    link = tmp_path / 'link'
    link.symlink_to(real)
    result = _host_symlink_lexical_path(link)
    assert result == str(link.expanduser().absolute())


def test_resolve_attachment_shared_defaults_to_exact_host_path(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-shared-exact'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(
        cfg, cfg_path, host_src, '', AttachmentMode.SHARED
    )

    assert resolved.mode == AttachmentMode.SHARED
    assert resolved.guest_dst == str(host_src.expanduser().absolute())


def test_resolve_attachment_shared_root_defaults_to_exact_host_path(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-sr-exact'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    resolved = _resolve_attachment(
        cfg, cfg_path, host_src, '', AttachmentMode.SHARED_ROOT
    )

    assert resolved.mode == AttachmentMode.SHARED_ROOT
    assert resolved.guest_dst == str(host_src.expanduser().absolute())


def test_resolve_attachment_explicit_guest_dst_is_preserved(
    tmp_path: Path,
) -> None:
    """Explicit --guest_dst overrides the default for all modes."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-custom-dst'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    save_store(Store(), cfg_path)

    for mode in (AttachmentMode.SHARED, AttachmentMode.SHARED_ROOT, AttachmentMode.GIT):
        resolved = _resolve_attachment(
            cfg, cfg_path, host_src, '/custom/path', mode
        )
        assert resolved.guest_dst == '/custom/path'


# --- Tag generation ---

def test_auto_tag_includes_hash_suffix(tmp_path: Path) -> None:
    """Fresh generated tags always include a hash to avoid basename collisions."""
    from aivm.vm.share import _auto_share_tag_for_path

    d = tmp_path / 'myproject'
    d.mkdir()
    tag = _auto_share_tag_for_path(d, set())
    assert tag.startswith('hostcode-myproject-')
    # Must contain a non-trivial hash portion (8 hex chars)
    parts = tag.split('-')
    assert len(parts[-1]) == 8
    assert all(c in '0123456789abcdef' for c in parts[-1])


def test_auto_tag_different_paths_same_basename_get_different_tags(
    tmp_path: Path,
) -> None:
    """Two directories with the same basename produce different tags."""
    from aivm.vm.share import _auto_share_tag_for_path

    d1 = tmp_path / 'a' / 'repo'
    d2 = tmp_path / 'b' / 'repo'
    d1.mkdir(parents=True)
    d2.mkdir(parents=True)
    tag1 = _auto_share_tag_for_path(d1, set())
    tag2 = _auto_share_tag_for_path(d2, set())
    assert tag1 != tag2


def test_resolve_attachment_preserves_existing_saved_tag(
    tmp_path: Path,
) -> None:
    """Existing saved tags are preserved; no forced re-generation."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-tag-preserve'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()

    saved_tag = 'my-old-custom-tag'
    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(host_src.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.SHARED,
            guest_dst='/workspace/proj',
            tag=saved_tag,
        )
    )
    save_store(store, cfg_path)

    resolved = _resolve_attachment(cfg, cfg_path, host_src, '', '')
    assert resolved.tag == saved_tag


# --- Host symlink companion symlink ---


def test_ensure_guest_symlink_creates_new_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-symlink'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'

    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.cli.vm.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (cmds.append([str(c) for c in cmd]) or _Proc(0, '', '')),
    )

    _ensure_guest_symlink(
        cfg,
        '10.0.0.1',
        symlink_path='/home/joncrall/code/repo',
        target_path='/home/joncrall/code/repo',
    )

    assert len(cmds) == 1
    assert cmds[0][0] == 'ssh'
    script = cmds[0][-1]
    assert "ln -s" in script
    assert '/home/joncrall/code/repo' in script


def test_ensure_guest_symlink_warns_on_wrong_existing_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-symlink-warn'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'

    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: p or '/tmp/id_ed25519')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: ['-i', '/tmp/id_ed25519'])

    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)
        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.cli.vm.log', _FakeLog())
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        # exit code 3 = wrong symlink
        lambda cmd, **kwargs: _Proc(3, '', 'aivm-symlink-warn: /link is a symlink to /other; skipping'),
    )

    _ensure_guest_symlink(
        cfg, '10.0.0.1',
        symlink_path='/link',
        target_path='/target',
    )

    assert any('symlink to /other' in m for m in messages)


# --- Mirror-home symlink computation ---


def test_compute_mirror_home_returns_none_when_not_default_dst(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """_compute_mirror_home_symlink returns None when is_default_dst=False (custom --guest_dst)."""
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: Path('/home/joncrall'))
    host_src = Path('/home/joncrall/code/foobar')
    result = _compute_mirror_home_symlink(
        cfg, host_src, '/custom/path', is_default_dst=False
    )
    assert result is None


def test_compute_mirror_home_returns_none_when_explicit_dst(
    tmp_path: Path,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    host_src = tmp_path / 'code' / 'foobar'
    result = _compute_mirror_home_symlink(
        cfg, host_src, '/custom/path', is_default_dst=False
    )
    assert result is None


def test_compute_mirror_home_returns_none_when_path_not_under_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: Path('/home/joncrall'))
    host_src = Path('/data/external/project')
    result = _compute_mirror_home_symlink(
        cfg, host_src, str(host_src), is_default_dst=True
    )
    assert result is None


def test_compute_mirror_home_returns_none_when_guest_home_equals_host_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'joncrall'  # same user
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: Path('/home/joncrall'))
    host_src = Path('/home/joncrall/code/foobar')
    result = _compute_mirror_home_symlink(
        cfg, host_src, str(host_src), is_default_dst=True
    )
    assert result is None  # guest home == host home


def test_compute_mirror_home_returns_correct_path(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: Path('/home/joncrall'))
    host_src = Path('/home/joncrall/code/foobar')
    guest_dst = '/home/joncrall/code/foobar'
    result = _compute_mirror_home_symlink(
        cfg, host_src, guest_dst, is_default_dst=True
    )
    assert result == '/home/agent/code/foobar'


def test_compute_mirror_home_returns_none_when_mirror_equals_primary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When primary dst already matches the mirror path, skip."""
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: Path('/home/agent'))
    # host_src is under /home/agent (same as guest home)
    host_src = Path('/home/agent/code/foobar')
    guest_dst = '/home/agent/code/foobar'
    result = _compute_mirror_home_symlink(
        cfg, host_src, guest_dst, is_default_dst=True
    )
    # guest home == host home so returns None
    assert result is None


# --- ensure_guest_symlink safety rules ---


def _make_ssh_fake(exit_code: int, stderr: str = '') -> Any:
    """Return a subprocess.run replacement that always returns the given code."""
    def fake(cmd: list[str], **kwargs: Any) -> _Proc:
        return _Proc(exit_code, '', stderr)
    return fake


def test_ensure_guest_symlink_noop_on_correct_existing_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-ok'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: '/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: [])
    messages: list[str] = []
    monkeypatch.setattr('aivm.cli.vm.log', type('L', (), {
        'warning': lambda s, fmt, *a, **k: messages.append(fmt),
        'info': lambda s, *a, **k: None,
        'debug': lambda s, *a, **k: None,
        'trace': lambda s, *a, **k: None,
        'error': lambda s, *a, **k: None,
    })())
    # exit 0 = already correct
    monkeypatch.setattr('aivm.commands.subprocess.run', _make_ssh_fake(0))
    _ensure_guest_symlink(cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt')
    assert not messages


def test_ensure_guest_symlink_warns_on_nonempty_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-warn-dir'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: '/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: [])
    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)
        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.cli.vm.log', _FakeLog())
    # exit 4 = non-empty dir, with warning message
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        _make_ssh_fake(4, 'aivm-symlink-warn: /link is a non-empty directory; skipping'),
    )
    _ensure_guest_symlink(cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt')
    assert any('non-empty directory' in m for m in messages)


def test_ensure_guest_symlink_warns_on_regular_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-warn-file'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: '/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: [])
    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)
        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.cli.vm.log', _FakeLog())
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        _make_ssh_fake(5, 'aivm-symlink-warn: /link is a regular file; skipping'),
    )
    _ensure_guest_symlink(cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt')
    assert any('regular file' in m for m in messages)


# --- Mirror-home integration via _ensure_attachment_available_in_guest ---

def test_ensure_attachment_creates_mirror_home_symlink_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When mirror_home=True and conditions met, companion symlink is created."""
    from aivm.cli.vm import _ensure_attachment_available_in_guest

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-mirror'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id'

    host_src = tmp_path / 'code' / 'foobar'
    host_src.mkdir(parents=True)
    guest_dst = str(host_src.expanduser().absolute())
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=guest_dst,
        guest_dst=guest_dst,
        tag='hostcode-foobar-abc12345',
    )

    monkeypatch.setattr('aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None)

    symlink_calls: list[dict] = []

    def fake_ensure_guest_symlink(cfg_arg: Any, ip: str, *, symlink_path: str, target_path: str) -> None:
        symlink_calls.append({'symlink_path': symlink_path, 'target_path': target_path})

    monkeypatch.setattr('aivm.cli.vm._ensure_guest_symlink', fake_ensure_guest_symlink)

    # Patch Path.home to something known so mirror can be computed
    host_home = tmp_path
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: host_home)

    _activate_manager(monkeypatch)

    _ensure_attachment_available_in_guest(
        cfg,
        host_src,
        attachment,
        '10.0.0.1',
        yes=True,
        dry_run=False,
        ensure_shared_root_host_side=False,
        mirror_home=True,
    )

    # Mirror symlink should have been requested for /home/agent/code/foobar -> guest_dst
    expected_mirror = '/home/agent/code/foobar'
    assert any(c['symlink_path'] == expected_mirror for c in symlink_calls), symlink_calls


def test_ensure_attachment_no_mirror_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When mirror_home=False, no companion symlink call happens."""
    from aivm.cli.vm import _ensure_attachment_available_in_guest

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-no-mirror'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id'

    host_src = tmp_path / 'code' / 'foobar'
    host_src.mkdir(parents=True)
    guest_dst = str(host_src.expanduser().absolute())
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=guest_dst,
        guest_dst=guest_dst,
        tag='hostcode-foobar-abc12345',
    )

    monkeypatch.setattr('aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None)

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda *a, **k: symlink_calls.append(k),
    )

    _activate_manager(monkeypatch)

    _ensure_attachment_available_in_guest(
        cfg,
        host_src,
        attachment,
        '10.0.0.1',
        yes=True,
        dry_run=False,
        ensure_shared_root_host_side=False,
        mirror_home=False,
    )

    assert symlink_calls == []


# --- Git exact-path support ---

def test_ensure_guest_git_repo_uses_sudo_for_parent_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_ensure_guest_git_repo script includes sudo mkdir for parent dirs."""
    from aivm.cli.vm import _ensure_guest_git_repo

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-exact'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id'

    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: p or '/tmp/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: ['-i', '/tmp/id'])

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (cmds.append([str(c) for c in cmd]) or _Proc(0, '', '')),
    )

    _ensure_guest_git_repo(cfg, '/home/joncrall/code/myrepo', 'main')

    assert len(cmds) == 1
    script = cmds[0][-1]
    assert 'sudo -n mkdir -p' in script
    assert 'sudo -n chown' in script
    assert '/home/joncrall/code/myrepo' in script


# ---------------------------------------------------------------------------
# Follow-up patch tests: unified host-path handling, sudo symlinks, git mirror
# ---------------------------------------------------------------------------


def _make_minimal_code_ssh_mocks(
    monkeypatch: pytest.MonkeyPatch,
    cfg: AgentVMConfig,
    cfg_path: Any,
    host_src: Path,
    attachment: ResolvedAttachment,
) -> None:
    """Wire up the common mocks needed for VMCodeCLI / VMSSHCLI tests."""
    from aivm.cli._common import PreparedSession

    session = PreparedSession(
        cfg=cfg,
        cfg_path=cfg_path,
        host_src=host_src,
        attachment_mode=attachment.mode,
        share_source_dir=attachment.source_dir,
        share_tag=attachment.tag,
        share_guest_dst=attachment.guest_dst,
        ip='10.0.0.1',
        reg_path=cfg_path,
        meta_path=None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._prepare_attached_session',
        lambda **kw: session,
    )


def _fake_prepare_session(
    cfg: AgentVMConfig,
    cfg_path: Any,
    host_src: Path,
    attachment: ResolvedAttachment,
    captured: list,
) -> Any:
    """Return a fake _prepare_attached_session callable that records its kwargs."""
    from aivm.cli._common import PreparedSession

    def fake_prepare(**kw: Any) -> PreparedSession:
        captured.append(kw)
        return PreparedSession(
            cfg=cfg,
            cfg_path=cfg_path,
            host_src=kw['host_src'],
            attachment_mode=attachment.mode,
            share_source_dir=attachment.source_dir,
            share_tag=attachment.tag,
            share_guest_dst=attachment.guest_dst,
            ip='10.0.0.1',
            reg_path=cfg_path,
            meta_path=None,
        )

    return fake_prepare


def test_vm_code_passes_lexical_host_src_to_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VMCodeCLI should pass the lexical (non-resolved) host_src so symlink detection works."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-code-lexical'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(host_src.resolve()),
        guest_dst=str(host_src),
        tag='hostcode-proj-abc12345',
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._prepare_attached_session',
        _fake_prepare_session(cfg, cfg_path, host_src, attachment, captured),
    )

    # dry_run=True exits immediately after getting the session — no subprocess needed
    VMCodeCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
        dry_run=True,
    )

    assert captured, 'expected _prepare_attached_session to be called'
    passed = captured[0]['host_src']
    # Must be the lexical absolute path (expanduser+absolute), not pre-resolved
    assert passed == host_src.expanduser().absolute()


def test_vm_ssh_passes_lexical_host_src_to_session(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """VMSSHCLI should pass the lexical host_src so symlink detection works."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-ssh-lexical'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(host_src.resolve()),
        guest_dst=str(host_src),
        tag='hostcode-proj-abc12345',
    )

    captured: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._prepare_attached_session',
        _fake_prepare_session(cfg, cfg_path, host_src, attachment, captured),
    )

    VMSSHCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        yes=True,
        dry_run=True,
    )

    assert captured, 'expected _prepare_attached_session to be called'
    passed = captured[0]['host_src']
    assert passed == host_src.expanduser().absolute()


def test_ensure_guest_symlink_uses_sudo_for_ln(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """symlink creation and dir removal must use sudo -n so non-writable parents work."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-sudo-ln'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: '/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: [])

    scripts: list[str] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (scripts.append(cmd[-1]) or _Proc(0, '', '')),
    )

    _ensure_guest_symlink(
        cfg, '10.0.0.1',
        symlink_path='/home/joncrall/code/repo',
        target_path='/home/joncrall/code/repo',
    )

    assert scripts, 'expected SSH command'
    script = scripts[0]
    assert 'sudo -n ln -s' in script
    assert 'sudo -n mkdir -p' in script
    # plain ln -s (without sudo) must NOT appear
    assert '\nln -s' not in script
    assert '; ln -s' not in script


def test_ensure_guest_git_repo_uses_sudo_mkdir_for_full_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exact-path git repo creation must sudo-mkdir the full root, not just the parent."""
    from aivm.cli.vm import _ensure_guest_git_repo

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-sudo'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.cli.vm.require_ssh_identity', lambda p: '/id')
    monkeypatch.setattr('aivm.cli.vm.ssh_base_args', lambda *a, **k: [])

    scripts: list[str] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (scripts.append(cmd[-1]) or _Proc(0, '', '')),
    )

    _ensure_guest_git_repo(cfg, '/home/joncrall/code/myrepo', 'main')

    assert scripts
    script = scripts[0]
    # Must sudo the full path, not just the parent
    assert 'sudo -n mkdir -p' in script
    assert '/home/joncrall/code/myrepo' in script
    assert 'sudo -n chown' in script
    # Confirm no stale parent_q variable reference (parent-only mkdir)
    assert 'sudo -n mkdir -p /home/joncrall/code\n' not in script


def test_git_mode_in_prepare_session_gets_companion_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git mode in _prepare_attached_session creates a companion symlink for host symlinks."""
    from aivm.cli.vm import _prepare_attached_session

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-companion'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    # Set up a real dir and a symlink pointing to it
    real_dir = tmp_path / 'real'
    real_dir.mkdir()
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real_dir)

    from aivm.store import Store
    from aivm.store import save_store as _save_store
    store = Store()
    store.attachments.append(
        AttachmentEntry(
            host_path=str(real_dir.resolve()),
            vm_name=cfg.vm.name,
            mode=AttachmentMode.GIT,
            guest_dst=str(real_dir.resolve()),
            tag='',
        )
    )
    _save_store(store, cfg_path)

    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.GIT,
        source_dir=str(real_dir.resolve()),
        guest_dst=str(real_dir.resolve()),
        tag='',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code', lambda **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment', lambda *a, **k: attachment
    )
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_attached_vm',
        lambda *a, **k: type('R', (), {
            'attachment': attachment,
            'cached_ip': '10.0.0.1',
            'shared_root_host_side_ready': False,
        })(),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._maybe_offer_create_ssh_identity', lambda *a, **k: False
    )
    monkeypatch.setattr('aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm.probe_ssh_ready',
        lambda *a, **k: type('P', (), {'ok': True})(),
    )
    monkeypatch.setattr('aivm.cli.vm.load_store', lambda p: store)

    git_calls: list = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_git_clone_attachment',
        lambda *a, **k: git_calls.append(1) or (tmp_path, 'ssh', 'git'),
    )

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda cfg_a, ip, *, symlink_path, target_path: symlink_calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )

    monkeypatch.setattr(
        'aivm.cli.vm._restore_saved_vm_attachments', lambda *a, **k: None
    )

    _prepare_attached_session(
        config_opt=str(cfg_path),
        vm_opt='',
        host_src=link_dir,  # lexical symlink path
        guest_dst_opt='',
        recreate_if_needed=False,
        ensure_firewall_opt=False,
        force=False,
        dry_run=False,
        yes=True,
    )

    assert git_calls, 'git clone should have been called'
    # companion symlink from lexical link path to resolved real path
    expected_link = str(link_dir.expanduser().absolute())
    expected_target = str(real_dir.resolve())
    assert any(
        c['symlink_path'] == expected_link and c['target_path'] == expected_target
        for c in symlink_calls
    ), f'Expected companion symlink {expected_link} -> {expected_target}, got: {symlink_calls}'


def test_git_mode_in_prepare_session_gets_mirror_home_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git mode in _prepare_attached_session creates a mirror-home symlink when enabled."""
    from aivm.cli.vm import _prepare_attached_session

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-mirror'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    host_src = tmp_path / 'code' / 'myproject'
    host_src.mkdir(parents=True)

    from aivm.store import Store
    from aivm.store import save_store as _save_store
    store = Store()
    store.behavior.mirror_shared_home_folders = True
    _save_store(store, cfg_path)

    guest_dst = str(host_src.expanduser().absolute())
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.GIT,
        source_dir=guest_dst,
        guest_dst=guest_dst,
        tag='',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code', lambda **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment', lambda *a, **k: attachment
    )
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_attached_vm',
        lambda *a, **k: type('R', (), {
            'attachment': attachment,
            'cached_ip': '10.0.0.1',
            'shared_root_host_side_ready': False,
        })(),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._maybe_offer_create_ssh_identity', lambda *a, **k: False
    )
    monkeypatch.setattr('aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm.probe_ssh_ready',
        lambda *a, **k: type('P', (), {'ok': True})(),
    )
    monkeypatch.setattr('aivm.cli.vm.load_store', lambda p: store)

    monkeypatch.setattr(
        'aivm.cli.vm._ensure_git_clone_attachment',
        lambda *a, **k: (tmp_path, 'ssh', 'git'),
    )

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda cfg_a, ip, *, symlink_path, target_path: symlink_calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._restore_saved_vm_attachments', lambda *a, **k: None
    )

    # Patch Path.home so we know what the mirror path will be
    host_home = tmp_path
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: host_home)

    _prepare_attached_session(
        config_opt=str(cfg_path),
        vm_opt='',
        host_src=host_src,
        guest_dst_opt='',
        recreate_if_needed=False,
        ensure_firewall_opt=False,
        force=False,
        dry_run=False,
        yes=True,
    )

    # Mirror symlink should point into /home/agent/code/myproject
    expected_mirror = '/home/agent/code/myproject'
    assert any(
        c['symlink_path'] == expected_mirror for c in symlink_calls
    ), f'Expected mirror symlink at {expected_mirror}, got: {symlink_calls}'


# ---------------------------------------------------------------------------
# Pass 3: _apply_guest_derived_symlinks and _restore_saved_vm_attachments
# ---------------------------------------------------------------------------


def test_apply_guest_derived_symlinks_companion_only(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Companion symlink created when host_src is a symlink; no mirror without flag."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-deriv'
    cfg.vm.user = 'agent'

    real_dir = tmp_path / 'real'
    real_dir.mkdir()
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real_dir)

    resolved_dst = str(real_dir)
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=resolved_dst,
        guest_dst=resolved_dst,
        tag='tag1',
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )

    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', link_dir, attachment, mirror_home=False
    )

    assert len(calls) == 1
    assert calls[0]['symlink_path'] == str(link_dir.expanduser().absolute())
    assert calls[0]['target_path'] == resolved_dst


def test_apply_guest_derived_symlinks_dual_mirror_for_symlink_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When host_src is a symlink, mirror-home applies to both lexical and resolved paths."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-dual-mirror'
    cfg.vm.user = 'agent'

    # Set up: host_home = tmp_path
    # lexical = tmp_path/code/link  (symlink to tmp_path/real/code)
    # resolved = tmp_path/real/code
    host_home = tmp_path
    real_dir = tmp_path / 'real' / 'code'
    real_dir.mkdir(parents=True)
    code_dir = tmp_path / 'code'
    code_dir.mkdir()
    link_dir = code_dir / 'link'
    link_dir.symlink_to(real_dir)

    resolved_dst = str(real_dir)
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=resolved_dst,
        guest_dst=resolved_dst,
        tag='tag2',
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: host_home)

    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', link_dir, attachment, mirror_home=True
    )

    symlink_paths = [c['symlink_path'] for c in calls]
    # Companion symlink at lexical guest path
    assert str(link_dir.expanduser().absolute()) in symlink_paths
    # Mirror for lexical host path (tmp_path/code/link -> guest home /home/agent/code/link)
    assert '/home/agent/code/link' in symlink_paths
    # Mirror for resolved host path (tmp_path/real/code -> guest home /home/agent/real/code)
    assert '/home/agent/real/code' in symlink_paths


def test_apply_guest_derived_symlinks_no_dup_mirror_when_same(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If both lexical and resolved mirrors compute to the same path, only one symlink is created."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-nodup'
    cfg.vm.user = 'agent'

    # host_home = tmp_path/home/joncrall
    # symlink: tmp_path/home/joncrall/proj -> tmp_path/home/joncrall/proj_real
    # lexical relative to home = proj; resolved relative to home = proj_real
    # They differ, so two distinct mirrors. This test verifies deduplication
    # when lexical == resolved (not a symlink - just sanity check no duplicate).
    real_dir = tmp_path / 'code'
    real_dir.mkdir()

    resolved_dst = str(real_dir)
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=resolved_dst,
        guest_dst=resolved_dst,
        tag='tag3',
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: tmp_path)

    # Not a symlink — no companion, only one mirror
    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', real_dir, attachment, mirror_home=True
    )

    # Only one mirror call (for the non-symlink host, resolved branch is skipped)
    mirror_calls = [c for c in calls if '/home/agent' in c['symlink_path']]
    symlink_paths = [c['symlink_path'] for c in mirror_calls]
    # No duplicates
    assert len(symlink_paths) == len(set(symlink_paths))


def test_restore_shared_attachment_applies_guest_derived_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_restore_saved_vm_attachments applies _apply_guest_derived_symlinks for shared mode."""
    from aivm.cli.vm import _restore_saved_vm_attachments

    _activate_manager(monkeypatch)

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-restore-shared'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    host_src = tmp_path / 'proj'
    host_src.mkdir()

    primary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(host_src),
        guest_dst=str(host_src),
        tag='tag-primary',
    )
    secondary_src = tmp_path / 'sec'
    secondary_src.mkdir()
    secondary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(secondary_src),
        guest_dst=str(secondary_src),
        tag='tag-secondary',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.vm_share_mappings',
        lambda *a, **k: [(str(secondary_src), 'tag-secondary')],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._apply_guest_derived_symlinks',
        lambda cfg_a, ip, host_src_a, att, *, mirror_home: derived_calls.append(
            {'host_src': host_src_a, 'mirror_home': mirror_home}
        ),
    )

    _restore_saved_vm_attachments(
        cfg,
        cfg_path,
        ip='10.0.0.1',
        primary_attachment=primary,
        yes=True,
        mirror_home=True,
    )

    assert len(derived_calls) == 1
    assert derived_calls[0]['mirror_home'] is True


def test_restore_shared_root_attachment_passes_mirror_home(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_restore_saved_vm_attachments passes mirror_home to _ensure_attachment_available_in_guest for shared-root."""
    from aivm.cli.vm import _restore_saved_vm_attachments

    _activate_manager(monkeypatch)

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-restore-sr'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    host_src = tmp_path / 'proj'
    host_src.mkdir()

    primary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(host_src),
        guest_dst=str(host_src),
        tag='token-primary',
    )
    secondary_src = tmp_path / 'sec'
    secondary_src.mkdir()
    secondary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED_ROOT,
        source_dir=str(secondary_src),
        guest_dst=str(secondary_src),
        tag='token-secondary',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )

    ensure_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_attachment_available_in_guest',
        lambda cfg_a, host_src_a, att, ip, *, yes, dry_run, ensure_shared_root_host_side, allow_disruptive_shared_root_rebind, mirror_home: ensure_calls.append(
            {
                'allow_disruptive': allow_disruptive_shared_root_rebind,
                'mirror_home': mirror_home,
            }
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )

    _restore_saved_vm_attachments(
        cfg,
        cfg_path,
        ip='10.0.0.1',
        primary_attachment=primary,
        yes=True,
        mirror_home=True,
    )

    assert len(ensure_calls) == 1
    assert ensure_calls[0]['mirror_home'] is True
    # Non-disruptive rebind must remain False during restore
    assert ensure_calls[0]['allow_disruptive'] is False


# ---------------------------------------------------------------------------
# Pass 4: host_lexical_path persistence and custom guest_dst mirror suppression
# ---------------------------------------------------------------------------


def test_record_attachment_persists_lexical_path_for_symlink(
    tmp_path: Path,
) -> None:
    """_record_attachment stores host_lexical_path when host_src is a symlink."""
    from aivm.cli.vm import _record_attachment

    real_dir = tmp_path / 'real'
    real_dir.mkdir()
    link_dir = tmp_path / 'link'
    link_dir.symlink_to(real_dir)

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-lex-persist'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    _record_attachment(
        cfg,
        cfg_path,
        host_src=link_dir,
        mode='shared',
        access='rw',
        guest_dst=str(real_dir),
        tag='tag-lex',
    )

    reg = load_store(cfg_path)
    entries = [a for a in reg.attachments if a.vm_name == cfg.vm.name]
    assert len(entries) == 1
    assert entries[0].host_lexical_path == str(link_dir.expanduser().absolute())
    # host_path (the resolved canonical key) must be the real path
    assert entries[0].host_path == str(real_dir.resolve())


def test_record_attachment_no_lexical_path_for_non_symlink(
    tmp_path: Path,
) -> None:
    """_record_attachment leaves host_lexical_path empty for non-symlink paths."""
    from aivm.cli.vm import _record_attachment

    real_dir = tmp_path / 'real'
    real_dir.mkdir()

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-nolex'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    _record_attachment(
        cfg,
        cfg_path,
        host_src=real_dir,
        mode='shared',
        access='rw',
        guest_dst=str(real_dir),
        tag='tag-nolex',
    )

    reg = load_store(cfg_path)
    entries = [a for a in reg.attachments if a.vm_name == cfg.vm.name]
    assert len(entries) == 1
    assert entries[0].host_lexical_path == ''


def test_store_backward_compat_missing_lexical_path(
    tmp_path: Path,
) -> None:
    """Store loads cleanly from old TOML files that have no host_lexical_path field."""
    cfg_path = tmp_path / 'config.toml'
    # Minimal old-format store with no host_lexical_path
    cfg_path.write_text(
        'schema_version = 5\n'
        'active_vm = ""\n'
        '[behavior]\n'
        'yes_sudo = false\n'
        'auto_approve_readonly_sudo = true\n'
        'verbose = 1\n'
        'mirror_shared_home_folders = false\n'
        '[[attachments]]\n'
        'host_path = "/some/real/path"\n'
        'vm_name = "oldvm"\n'
        'mode = "shared"\n'
        'access = "rw"\n'
        'guest_dst = "/some/real/path"\n'
        'tag = "hostcode-path-abcd1234"\n',
        encoding='utf-8',
    )

    reg = load_store(cfg_path)
    assert len(reg.attachments) == 1
    att = reg.attachments[0]
    assert att.host_path == '/some/real/path'
    assert att.host_lexical_path == ''  # graceful default


def test_restore_uses_lexical_path_for_companion_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """After restore, companion guest symlink is created using the stored lexical path."""
    from aivm.cli.vm import _restore_saved_vm_attachments

    _activate_manager(monkeypatch)

    real_dir = tmp_path / 'real' / 'proj'
    real_dir.mkdir(parents=True)
    link_dir = tmp_path / 'link' / 'proj'
    (tmp_path / 'link').mkdir()
    link_dir.symlink_to(real_dir)

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-lex-restore'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    # Set up store with saved attachment that has host_lexical_path
    reg = Store()
    upsert_attachment(
        reg,
        host_path=real_dir,  # resolved key
        vm_name=cfg.vm.name,
        mode='shared',
        access='rw',
        guest_dst=str(real_dir),
        tag='tag-lex-restore',
        host_lexical_path=str(link_dir),
    )
    save_store(reg, cfg_path)

    primary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(tmp_path / 'primary'),  # different source so secondary runs
        guest_dst=str(tmp_path / 'primary'),
        tag='tag-primary',
    )
    (tmp_path / 'primary').mkdir()

    secondary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(real_dir),
        guest_dst=str(real_dir),
        tag='tag-lex-restore',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.vm_share_mappings',
        lambda *a, **k: [(str(real_dir), 'tag-lex-restore')],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr('aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None)
    monkeypatch.setattr('aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path)

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._apply_guest_derived_symlinks',
        lambda cfg_a, ip, host_src_a, att, *, mirror_home: derived_calls.append(
            {'host_src': host_src_a}
        ),
    )

    _restore_saved_vm_attachments(
        cfg,
        cfg_path,
        ip='10.0.0.1',
        primary_attachment=primary,
        yes=True,
        mirror_home=False,
    )

    assert len(derived_calls) == 1
    # Must have received the lexical path, not the resolved source_dir
    assert derived_calls[0]['host_src'] == link_dir


def test_restore_non_symlink_attachment_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Non-symlink attachments without host_lexical_path use source_dir as before."""
    from aivm.cli.vm import _restore_saved_vm_attachments

    _activate_manager(monkeypatch)

    real_dir = tmp_path / 'proj'
    real_dir.mkdir()

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-nolex-restore'
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'

    # No host_lexical_path in store
    reg = Store()
    upsert_attachment(
        reg,
        host_path=real_dir,
        vm_name=cfg.vm.name,
        mode='shared',
        access='rw',
        guest_dst=str(real_dir),
        tag='tag-plain',
    )
    save_store(reg, cfg_path)

    primary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(tmp_path / 'primary'),
        guest_dst=str(tmp_path / 'primary'),
        tag='tag-primary',
    )
    (tmp_path / 'primary').mkdir()

    secondary = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=str(real_dir),
        guest_dst=str(real_dir),
        tag='tag-plain',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.vm_share_mappings',
        lambda *a, **k: [(str(real_dir), 'tag-plain')],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr('aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None)
    monkeypatch.setattr('aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path)

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._apply_guest_derived_symlinks',
        lambda cfg_a, ip, host_src_a, att, *, mirror_home: derived_calls.append(
            {'host_src': host_src_a}
        ),
    )

    _restore_saved_vm_attachments(
        cfg,
        cfg_path,
        ip='10.0.0.1',
        primary_attachment=primary,
        yes=True,
        mirror_home=False,
    )

    assert len(derived_calls) == 1
    # Falls back to source_dir (resolved) since no lexical path stored
    assert derived_calls[0]['host_src'] == Path(str(real_dir))


def test_apply_guest_derived_symlinks_custom_dst_suppresses_all_mirrors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Custom guest_dst suppresses both lexical and resolved mirror-home creation."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-custom-dst'
    cfg.vm.user = 'agent'

    host_home = tmp_path
    real_dir = tmp_path / 'code' / 'proj'
    real_dir.mkdir(parents=True)
    link_dir = tmp_path / 'link' / 'proj'
    (tmp_path / 'link').mkdir()
    link_dir.symlink_to(real_dir)

    resolved_dst = str(real_dir)
    custom_dst = '/custom/guest/path'  # explicit non-default destination

    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.SHARED,
        source_dir=resolved_dst,
        guest_dst=custom_dst,
        tag='tag-custom',
    )

    calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.Path.home', lambda: host_home)

    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', link_dir, attachment, mirror_home=True
    )

    # Companion symlink (lexical -> custom_dst) is allowed
    companion_calls = [c for c in calls if '/home/agent' not in c['symlink_path']]
    mirror_calls = [c for c in calls if '/home/agent' in c['symlink_path']]

    # The companion symlink at the lexical path is expected (it points to custom_dst)
    assert len(companion_calls) == 1
    assert companion_calls[0]['symlink_path'] == str(link_dir.expanduser().absolute())
    # No mirror-home symlinks should be created when guest_dst is custom
    assert mirror_calls == [], f'Expected no mirror calls, got: {mirror_calls}'
