"""Tests for guest-side symlink creation, git helpers, and apply_guest_derived_symlinks."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from aivm.attachments.guest import (
    _apply_guest_derived_symlinks,
    _ensure_guest_symlink,
    _git_attachment_remote_name,
    _git_current_branch,
    _upsert_host_git_remote,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig
from aivm.util import CmdResult
from aivm.vm.share import AttachmentMode, ResolvedAttachment


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


def test_ensure_guest_symlink_creates_new_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-symlink'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'

    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (
            cmds.append([str(c) for c in cmd]) or _Proc(0, '', '')
        ),
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
    assert 'ln -s' in script
    assert '/home/joncrall/code/repo' in script


def test_ensure_guest_symlink_warns_on_wrong_existing_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-symlink-warn'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'

    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )

    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)

        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.attachments.guest.log', _FakeLog())
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        # exit code 3 = wrong symlink
        lambda cmd, **kwargs: _Proc(
            3, '', 'aivm-symlink-warn: /link is a symlink to /other; skipping'
        ),
    )

    _ensure_guest_symlink(
        cfg,
        '10.0.0.1',
        symlink_path='/link',
        target_path='/target',
    )

    assert any('symlink to /other' in m for m in messages)


def test_ensure_guest_symlink_noop_on_correct_existing_symlink(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-ok'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )
    messages: list[str] = []
    monkeypatch.setattr(
        'aivm.attachments.guest.log',
        type(
            'L',
            (),
            {
                'warning': lambda s, fmt, *a, **k: messages.append(fmt),
                'info': lambda s, *a, **k: None,
                'debug': lambda s, *a, **k: None,
                'trace': lambda s, *a, **k: None,
                'error': lambda s, *a, **k: None,
            },
        )(),
    )
    # exit 0 = already correct
    monkeypatch.setattr(
        'aivm.commands.subprocess.run', lambda cmd, **kwargs: _Proc(0, '', '')
    )
    _ensure_guest_symlink(
        cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt'
    )
    assert not messages


def test_ensure_guest_symlink_warns_on_nonempty_dir(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-warn-dir'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )
    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)

        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.attachments.guest.log', _FakeLog())
    # exit 4 = non-empty dir, with warning message

    def _make_ssh_fake(exit_code: int, stderr: str = '') -> Any:
        def fake(cmd: list[str], **kwargs: Any) -> _Proc:
            return _Proc(exit_code, '', stderr)

        return fake

    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        _make_ssh_fake(
            4, 'aivm-symlink-warn: /link is a non-empty directory; skipping'
        ),
    )
    _ensure_guest_symlink(
        cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt'
    )
    assert any('non-empty directory' in m for m in messages)


def test_ensure_guest_symlink_warns_on_regular_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-warn-file'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )
    messages: list[str] = []

    class _FakeLog:
        def warning(self, fmt: str, *args: Any) -> None:
            messages.append(fmt.format(*args) if args else fmt)

        def info(self, *a: Any, **k: Any) -> None: ...
        def debug(self, *a: Any, **k: Any) -> None: ...
        def trace(self, *a: Any, **k: Any) -> None: ...
        def error(self, *a: Any, **k: Any) -> None: ...

    monkeypatch.setattr('aivm.attachments.guest.log', _FakeLog())

    def _make_ssh_fake(exit_code: int, stderr: str = '') -> Any:
        def fake(cmd: list[str], **kwargs: Any) -> _Proc:
            return _Proc(exit_code, '', stderr)

        return fake

    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        _make_ssh_fake(
            5, 'aivm-symlink-warn: /link is a regular file; skipping'
        ),
    )
    _ensure_guest_symlink(
        cfg, '10.0.0.1', symlink_path='/link', target_path='/tgt'
    )
    assert any('regular file' in m for m in messages)


def test_ensure_attachment_creates_mirror_home_symlink_when_enabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When mirror_home=True and conditions met, companion symlink is created."""
    from aivm.attachments.guest import _ensure_attachment_available_in_guest

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

    monkeypatch.setattr(
        'aivm.attachments.guest.ensure_share_mounted', lambda *a, **k: None
    )

    symlink_calls: list[dict] = []

    def fake_ensure_guest_symlink(
        cfg_arg: Any, ip: str, *, symlink_path: str, target_path: str
    ) -> None:
        symlink_calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        )

    monkeypatch.setattr(
        'aivm.attachments.guest._ensure_guest_symlink',
        fake_ensure_guest_symlink,
    )

    # Patch Path.home to something known so mirror can be computed
    host_home = tmp_path
    monkeypatch.setattr('aivm.attachments.resolve.Path.home', lambda: host_home)

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
    assert any(c['symlink_path'] == expected_mirror for c in symlink_calls), (
        symlink_calls
    )


def test_ensure_attachment_no_mirror_when_disabled(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When mirror_home=False, no companion symlink call happens."""
    from aivm.attachments.guest import _ensure_attachment_available_in_guest

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

    monkeypatch.setattr(
        'aivm.attachments.guest.ensure_share_mounted', lambda *a, **k: None
    )

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.guest._ensure_guest_symlink',
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


def test_ensure_guest_git_repo_uses_sudo_for_parent_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_ensure_guest_git_repo script includes sudo mkdir for parent dirs."""
    from aivm.attachments.guest import _ensure_guest_git_repo

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-exact'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id'

    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: p or '/tmp/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id'],
    )

    cmds: list[list[str]] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: (
            cmds.append([str(c) for c in cmd]) or _Proc(0, '', '')
        ),
    )

    _ensure_guest_git_repo(cfg, '/home/joncrall/code/myrepo')

    assert len(cmds) == 1
    script = cmds[0][-1]
    assert 'sudo -n mkdir -p' in script
    assert 'sudo -n chown' in script
    assert '/home/joncrall/code/myrepo' in script
    assert 'git init' in script
    assert 'symbolic-ref' not in script
    assert 'working tree' not in script


def test_ensure_guest_symlink_uses_sudo_for_ln(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """symlink creation and dir removal must use sudo -n so non-writable parents work."""
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-sudo-ln'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )

    scripts: list[str] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: scripts.append(cmd[-1]) or _Proc(0, '', ''),
    )

    _ensure_guest_symlink(
        cfg,
        '10.0.0.1',
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
    from aivm.attachments.guest import _ensure_guest_git_repo

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-sudo'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )

    scripts: list[str] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: scripts.append(cmd[-1]) or _Proc(0, '', ''),
    )

    _ensure_guest_git_repo(cfg, '/home/joncrall/code/myrepo')

    assert scripts
    script = scripts[0]
    # Must sudo the full path, not just the parent
    assert 'sudo -n mkdir -p' in script
    assert '/home/joncrall/code/myrepo' in script
    assert 'sudo -n chown' in script
    # Confirm no stale parent_q variable reference (parent-only mkdir)
    assert 'sudo -n mkdir -p /home/joncrall/code\n' not in script
    assert 'symbolic-ref' not in script


def test_ensure_guest_git_repo_allows_existing_dirty_tree(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aivm.attachments.guest import _ensure_guest_git_repo

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-dirty-guest'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = ''
    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )

    scripts: list[str] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: scripts.append(cmd[-1]) or _Proc(0, '', ''),
    )

    _ensure_guest_git_repo(cfg, '/home/joncrall/code/dirty-repo')

    script = scripts[0]
    assert 'git init' in script
    assert 'is not empty and is not a git repo' not in script
    assert 'symbolic-ref' not in script


def test_ensure_git_clone_attachment_skips_push_and_dirty_warning(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from aivm.attachments.guest import _ensure_git_clone_attachment

    repo = tmp_path / 'repo'
    repo.mkdir()
    subprocess.run(['git', 'init'], cwd=repo, check=True, capture_output=True)
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
    (repo / 'tracked.txt').write_text('tracked\n', encoding='utf-8')
    subprocess.run(
        ['git', '-C', str(repo), 'add', 'tracked.txt'],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ['git', '-C', str(repo), 'commit', '-m', 'init'],
        check=True,
        capture_output=True,
    )
    (repo / 'dirty.txt').write_text('dirty\n', encoding='utf-8')

    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-git-light'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id'
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.GIT,
        source_dir=str(repo.resolve()),
        guest_dst='/workspace/repo',
        tag='',
    )

    _activate_manager(monkeypatch)
    monkeypatch.setattr(
        'aivm.attachments.guest.require_ssh_identity', lambda p: '/tmp/id'
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ssh_base_args', lambda *a, **k: []
    )
    monkeypatch.setattr(
        'aivm.attachments.guest._upsert_ssh_config_entry',
        lambda *a, **k: (tmp_path / 'ssh-config', False),
    )
    monkeypatch.setattr(
        'aivm.attachments.guest._upsert_host_git_remote',
        lambda *a, **k: (tmp_path / 'git-config', False),
    )
    guest_repo_calls: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.attachments.guest._ensure_guest_git_repo',
        lambda *a, **k: guest_repo_calls.append((a, k)) or None,
    )

    warnings: list[str] = []
    monkeypatch.setattr(
        'aivm.attachments.guest.log.warning',
        lambda fmt, *args: warnings.append(fmt.format(*args)),
    )

    repo_root, ssh_cfg, git_cfg = _ensure_git_clone_attachment(
        cfg,
        repo,
        attachment,
        '10.0.0.5',
        yes=True,
        dry_run=False,
    )

    assert repo_root == repo.resolve()
    assert ssh_cfg == str(tmp_path / 'ssh-config')
    assert git_cfg == str(tmp_path / 'git-config')
    assert guest_repo_calls
    assert warnings == []


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
        'aivm.attachments.guest._ensure_guest_symlink',
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
        'aivm.attachments.guest._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.attachments.resolve.Path.home', lambda: host_home)

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
        'aivm.attachments.guest._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.attachments.resolve.Path.home', lambda: tmp_path)

    # Not a symlink — no companion, only one mirror
    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', real_dir, attachment, mirror_home=True
    )

    # Only one mirror call (for the non-symlink host, resolved branch is skipped)
    mirror_calls = [c for c in calls if '/home/agent' in c['symlink_path']]
    symlink_paths = [c['symlink_path'] for c in mirror_calls]
    # No duplicates
    assert len(symlink_paths) == len(set(symlink_paths))


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
        'aivm.attachments.guest._ensure_guest_symlink',
        lambda c, ip, *, symlink_path, target_path: calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr('aivm.attachments.resolve.Path.home', lambda: host_home)

    _apply_guest_derived_symlinks(
        cfg, '10.0.0.1', link_dir, attachment, mirror_home=True
    )

    # Companion symlink (lexical -> custom_dst) is allowed
    companion_calls = [
        c for c in calls if '/home/agent' not in c['symlink_path']
    ]
    mirror_calls = [c for c in calls if '/home/agent' in c['symlink_path']]

    # The companion symlink at the lexical path is expected (it points to custom_dst)
    assert len(companion_calls) == 1
    assert companion_calls[0]['symlink_path'] == str(
        link_dir.expanduser().absolute()
    )
    # No mirror-home symlinks should be created when guest_dst is custom
    assert mirror_calls == [], f'Expected no mirror calls, got: {mirror_calls}'
