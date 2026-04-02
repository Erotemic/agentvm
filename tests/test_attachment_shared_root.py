"""Tests for shared-root host/guest bind mechanics."""

from __future__ import annotations

import builtins
from pathlib import Path
from typing import Any

import pytest

from aivm.attachments.shared_root import (
    _ensure_shared_root_guest_bind,
    _ensure_shared_root_host_bind,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.status import ProbeOutcome
from aivm.vm.share import AttachmentAccess, AttachmentMode, ResolvedAttachment


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


def test_vm_attach_shared_root_running_ensures_guest_ready(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from aivm.cli.vm import VMAttachCLI

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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['bash', '-c']:
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
        line.startswith('findmnt -P -n -o SOURCE,ROOT,FSTYPE --target')
        for line in command_text
    )
    assert any('mount --bind' in line for line in command_text)
    assert all('umount ' not in line for line in command_text)


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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(
                0, f'SOURCE="{source_dir}[/sub]" ROOT="" FSTYPE=""', ''
            )
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
        line.startswith('findmnt -P -n -o SOURCE,ROOT,FSTYPE --target')
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(
                0, f'SOURCE="/dev/vda1[{source_dir}]" ROOT="" FSTYPE=""', ''
            )
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
        line.startswith('findmnt -P -n -o SOURCE,ROOT,FSTYPE --target')
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(0, 'SOURCE="/other/source" ROOT="" FSTYPE=""', '')
        if normalized[:2] == ['bash', '-c']:
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
        line for line in command_text if line.startswith('bash -c ')
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(0, 'SOURCE="/other/source" ROOT="" FSTYPE=""', '')
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
        line.startswith('findmnt -P -n -o SOURCE,ROOT,FSTYPE --target')
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(0, 'SOURCE="/dev/nvme0n1p1" ROOT="" FSTYPE=""', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['bash', '-c']:
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
    assert any(line.startswith('bash -c ') for line in command_text)


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
        'aivm.attachments.shared_root.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.attachments.shared_root.ssh_base_args',
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['bash', '-c']:
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
        msg.startswith('     command: sudo bash -c ') for msg in messages
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
        if normalized[:3] == ['findmnt', '-P', '-n']:
            return _Proc(1, '', '')
        if normalized[:2] == ['mkdir', '-p']:
            return _Proc(0, '', '')
        if normalized[:2] == ['bash', '-c']:
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
            '     command (read-only): sudo findmnt -P -n -o SOURCE,ROOT,FSTYPE --target '
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

    from aivm.attachments.shared_root import _ensure_shared_root_vm_mapping

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
        'aivm.attachments.shared_root.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.attachments.shared_root.ssh_base_args',
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
