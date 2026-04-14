"""Tests for persistent attachment manifest and reconcile orchestration."""

from __future__ import annotations

import json
import subprocess
import sys
from contextlib import nullcontext, redirect_stderr
from io import StringIO
from pathlib import Path
from types import SimpleNamespace

import pytest

from aivm.attachments.persistent import (
    _install_persistent_attachment_replay,
    _install_guest_text_if_changed,
    _persistent_attachment_manifest_text,
    _persistent_host_manifest_path,
    _reconcile_persistent_attachments_in_guest,
    _run_guest_root_script,
    _sync_persistent_attachment_manifest_on_host,
    _sync_persistent_attachment_manifest_to_guest,
    _write_text_if_changed,
)
from aivm.commands import CommandError, CommandManager
from aivm.config import AgentVMConfig
from aivm.store import Store, save_store


def _activate_manager(
    monkeypatch: pytest.MonkeyPatch, *, yes_sudo: bool = True
) -> None:
    CommandManager.activate(CommandManager(yes_sudo=yes_sudo))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: False)


def _exec_guest_replay_helper(source: str) -> dict[str, object]:
    ns: dict[str, object] = {'__name__': 'not_main'}
    exec(source, ns)
    real_subprocess = ns.get('subprocess')
    ns['subprocess'] = SimpleNamespace(
        run=getattr(real_subprocess, 'run', subprocess.run),
        PIPE=getattr(real_subprocess, 'PIPE', subprocess.PIPE),
        DEVNULL=getattr(real_subprocess, 'DEVNULL', subprocess.DEVNULL),
    )
    return ns


class _FakeSubprocessResult:
    def __init__(self, returncode: int = 0, stdout: str = '', stderr: str = ''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _make_guest_replay_fake_run(
    mounts: dict[str, dict[str, str]],
):
    root_mount: str = ''

    def fake_run(
        cmd,
        check=False,
        capture_output=False,
        text=False,
        stdout=None,
        stderr=None,
        **kwargs,
    ):
        del check, capture_output, text, stdout, stderr, kwargs
        nonlocal root_mount
        if cmd[:2] == ['mountpoint', '-q']:
            target = cmd[-1]
            return _FakeSubprocessResult(
                returncode=0 if target in mounts else 1
            )
        if cmd and cmd[0] == 'findmnt' and '--mountpoint' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(returncode=1)
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd[:2] == ['mount', '-t']:
            root_mount = cmd[-1]
            mounts[root_mount] = {'source': root_mount, 'options': 'rw'}
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and '--bind' in cmd:
            target = cmd[-1]
            source = cmd[cmd.index('--bind') + 1]
            mounts[target] = {'source': source, 'options': 'rw'}
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,ro' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'ro'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,rw' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'rw'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'findmnt' and '--target' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(
                    stdout='TARGET="/" SOURCE="/dev/vda1" OPTIONS="rw"'
                )
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt':
            lines = [
                f'TARGET="{target}" SOURCE="{info["source"]}"'
                for target, info in mounts.items()
            ]
            return _FakeSubprocessResult(stdout='\n'.join(lines))
        if cmd and cmd[0] == 'umount':
            mounts.pop(cmd[-1], None)
            return _FakeSubprocessResult()
        raise AssertionError(f'unhandled fake command: {cmd}')

    fake_run.mounts = mounts  # type: ignore[attr-defined]
    return fake_run


def test_exec_guest_replay_helper_does_not_leak_subprocess_run() -> None:
    from aivm.persistent_replay import persistent_replay_python

    original_run = subprocess.run
    ns = _exec_guest_replay_helper(persistent_replay_python())

    assert ns['subprocess'] is not subprocess
    ns['subprocess'].run = lambda *a, **k: None
    assert subprocess.run is original_run


def test_persistent_replay_templates_are_deterministic_in_process() -> None:
    from aivm.persistent_replay import (
        persistent_replay_python,
        persistent_replay_service_unit,
    )

    helper_a = persistent_replay_python()
    helper_b = persistent_replay_python()
    unit_a = persistent_replay_service_unit()
    unit_b = persistent_replay_service_unit()

    assert helper_a == helper_b
    assert unit_a == unit_b


def test_persistent_replay_templates_are_deterministic_across_processes() -> None:
    script = (
        'from aivm.persistent_replay import '
        'persistent_replay_python, persistent_replay_service_unit; '
        'from hashlib import sha256; '
        'print(sha256(persistent_replay_python().encode()).hexdigest()); '
        'print(sha256(persistent_replay_service_unit().encode()).hexdigest())'
    )
    first = subprocess.run(
        [sys.executable, '-c', script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    second = subprocess.run(
        [sys.executable, '-c', script],
        check=True,
        capture_output=True,
        text=True,
    ).stdout

    assert first == second


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


def test_persistent_manifest_write_is_byte_for_byte_noop(
    tmp_path: Path,
) -> None:
    path = tmp_path / 'state' / 'persistent-attachments.json'
    assert _write_text_if_changed(path, 'alpha\n') is True
    before = path.read_bytes()
    assert _write_text_if_changed(path, 'alpha\n') is False
    assert path.read_bytes() == before


def test_persistent_host_manifest_path_uses_app_data_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-app-data'
    cfg.paths.base_dir = '/var/lib/libvirt/aivm/aivm-2404'

    calls: list[tuple[str, str]] = []

    def fake_appdir(appname: str, kind: str) -> Path:
        calls.append((appname, kind))
        return tmp_path / kind

    monkeypatch.setattr('aivm.store._appdir', fake_appdir)

    path = _persistent_host_manifest_path(cfg)

    assert calls == [('aivm', 'data')]
    assert (
        path
        == tmp_path
        / 'data'
        / cfg.vm.name
        / 'state'
        / 'persistent-attachments.json'
    )
    assert str(cfg.paths.base_dir) not in str(path)


def test_persistent_manifest_sync_uses_checksum_rsync(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-sync'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    _sync_persistent_attachment_manifest_on_host(cfg, cfg_path, dry_run=False)
    _activate_manager(monkeypatch)

    calls: list[list[str]] = []

    class FakeManager:
        def step(self, *args, **kwargs):
            del args, kwargs
            return nullcontext()

        def run(self, cmd, **kwargs):
            del kwargs
            calls.append(list(cmd))
            if cmd and cmd[0] == 'rsync':
                return SimpleNamespace(
                    stdout='>f..t...... persistent-attachments.json\n'
                )
            return SimpleNamespace(stdout='')

    monkeypatch.setattr(
        'aivm.attachments.persistent.CommandManager.current',
        lambda: FakeManager(),
    )

    changed = _sync_persistent_attachment_manifest_to_guest(
        cfg,
        '10.0.0.5',
        dry_run=False,
    )

    assert changed is True
    assert calls[0][:3] == ['ssh', '-o', 'BatchMode=yes']
    assert any(cmd[0] == 'rsync' for cmd in calls)
    rsync_cmd = next(cmd for cmd in calls if cmd and cmd[0] == 'rsync')
    assert '--checksum' in rsync_cmd
    assert '--itemize-changes' in rsync_cmd


def test_persistent_manifest_sync_retries_transient_ssh_banner_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-sync-retry'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    _sync_persistent_attachment_manifest_on_host(cfg, cfg_path, dry_run=False)
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.attachments.persistent.time.sleep', lambda s: None)

    calls: list[list[str]] = []
    rsync_calls = {'n': 0}

    class FakeManager:
        def step(self, *args, **kwargs):
            del args, kwargs
            return nullcontext()

        def run(self, cmd, **kwargs):
            del kwargs
            calls.append(list(cmd))
            if cmd and cmd[0] == 'ssh':
                return SimpleNamespace(stdout='')
            if cmd and cmd[0] == 'rsync':
                rsync_calls['n'] += 1
                if rsync_calls['n'] == 1:
                    return SimpleNamespace(
                        stdout='',
                        stderr=(
                            'Connection timed out during banner exchange\n'
                            'Connection to 10.0.0.5 port 22 timed out'
                        ),
                        code=255,
                    )
                return SimpleNamespace(
                    stdout='>f..t...... persistent-attachments.json\n',
                    stderr='',
                    code=0,
                )
            return SimpleNamespace(stdout='')

    monkeypatch.setattr(
        'aivm.attachments.persistent.CommandManager.current',
        lambda: FakeManager(),
    )

    changed = _sync_persistent_attachment_manifest_to_guest(
        cfg,
        '10.0.0.5',
        dry_run=False,
    )

    assert changed is True
    assert rsync_calls['n'] == 2
    ssh_cmd = next(cmd for cmd in calls if cmd and cmd[0] == 'ssh')
    assert any('ConnectTimeout=15' in arg for arg in ssh_cmd)
    rsync_cmd = next(cmd for cmd in calls if cmd and cmd[0] == 'rsync')
    assert any('ConnectTimeout=15' in arg for arg in rsync_cmd)


@pytest.mark.parametrize(
    'status, expect_install',
    [('MISSING', True), ('MATCH', False), ('MISMATCH', True)],
)
def test_persistent_guest_text_sync_checks_hash_before_installing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, status: str, expect_install: bool
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = f'vm-persistent-install-{status.lower()}'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, str, str | None]] = []

    def fake_run(*args, **kwargs):
        del args
        summary = str(kwargs.get('summary') or '')
        script = str(kwargs.get('script') or '')
        role = kwargs.get('role')
        calls.append((summary, script, role))
        if summary == 'Check guest replay helper hash':
            assert 'cmp -s' not in script
            assert 'sha256sum' in script
            assert 'MISSING' in script and 'MATCH' in script and 'MISMATCH' in script
            return SimpleNamespace(stdout=f'{status}\n')
        if summary == 'Install guest replay helper':
            assert expect_install
            assert "printf '%s'" in script
            assert 'install -m 0755' in script
            assert role == 'modify'
            return SimpleNamespace(stdout='')
        if summary == 'Verify guest replay helper hash after install':
            assert expect_install
            return SimpleNamespace(stdout='MATCH\n')
        raise AssertionError(f'unexpected summary: {summary}')

    monkeypatch.setattr(
        'aivm.attachments.persistent._run_guest_root_script',
        fake_run,
    )

    changed = _install_guest_text_if_changed(
        cfg,
        '10.0.0.5',
        target='/usr/local/libexec/aivm-persistent-attachment-replay',
        text='helper body\n',
        mode='0755',
        label='guest replay helper',
        dry_run=False,
    )

    assert changed is expect_install
    assert [summary for summary, _, _ in calls] == (
        [
            'Check guest replay helper hash',
            'Install guest replay helper',
            'Verify guest replay helper hash after install',
        ]
        if expect_install
        else ['Check guest replay helper hash']
    )
    assert all(role == 'read' for summary, _, role in calls if 'Check' in summary)


def test_persistent_replay_install_refreshes_when_unit_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-install'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, str, str | None]] = []
    statuses = {
        'Check guest replay helper hash': 'MATCH',
        'Check guest replay unit hash': 'MISMATCH',
    }

    def fake_run(*args, **kwargs):
        del args
        summary = str(kwargs.get('summary') or '')
        script = str(kwargs.get('script') or '')
        role = kwargs.get('role')
        calls.append((summary, script, role))
        if summary in statuses:
            assert 'cmp -s' not in script
            assert 'sha256sum' in script
            return SimpleNamespace(stdout=f'{statuses[summary]}\n')
        if summary == 'Install guest replay unit':
            assert "printf '%s'" in script
            assert 'install -m 0644' in script
            assert role == 'modify'
            return SimpleNamespace(stdout='')
        if summary == 'Verify guest replay unit hash after install':
            assert statuses['Check guest replay unit hash'] == 'MISMATCH'
            return SimpleNamespace(stdout='MATCH\n')
        if summary == 'Refresh persistent attachment replay unit':
            assert 'systemctl daemon-reload' in script
            assert 'systemctl enable aivm-persistent-attachment-replay.service' in script
            return SimpleNamespace(stdout='')
        raise AssertionError(f'unexpected summary: {summary}')

    monkeypatch.setattr(
        'aivm.attachments.persistent._run_guest_root_script',
        fake_run,
    )

    changed = _install_persistent_attachment_replay(
        cfg,
        '10.0.0.5',
        dry_run=False,
    )

    assert changed is True
    assert [summary for summary, _, _ in calls] == [
        'Check guest replay helper hash',
        'Check guest replay unit hash',
        'Install guest replay unit',
        'Verify guest replay unit hash after install',
        'Refresh persistent attachment replay unit',
    ]


def test_persistent_replay_install_skips_refresh_when_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-install-unchanged'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, str, str | None]] = []

    def fake_run(*args, **kwargs):
        del args
        summary = str(kwargs.get('summary') or '')
        script = str(kwargs.get('script') or '')
        role = kwargs.get('role')
        calls.append((summary, script, role))
        if 'Check guest replay helper hash' == summary:
            assert 'sha256sum' in script
            return SimpleNamespace(stdout='MATCH\n')
        if 'Check guest replay unit hash' == summary:
            assert 'sha256sum' in script
            return SimpleNamespace(stdout='MATCH\n')
        if 'Verify guest replay helper hash after install' == summary:
            raise AssertionError(
                'post-install verification should not run when unchanged'
            )
        if 'Refresh persistent attachment replay unit' == summary:
            raise AssertionError(
                'daemon-reload should be skipped when unchanged'
            )
        raise AssertionError(f'unexpected summary: {summary}')

    monkeypatch.setattr(
        'aivm.attachments.persistent._run_guest_root_script',
        fake_run,
    )

    changed = _install_persistent_attachment_replay(
        cfg,
        '10.0.0.5',
        dry_run=False,
    )

    assert changed is False
    assert not any('cmp -s' in script for _, script, _ in calls)
    assert not any('daemon-reload' in script for _, script, _ in calls)


def test_persistent_reconcile_reruns_replay_when_guest_manifest_unchanged(
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
        lambda *a, **k: calls.append(('host', a, k))
        or _persistent_host_manifest_path(cfg),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: calls.append(('guest-sync', a, k)) or False,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._install_persistent_attachment_replay',
        lambda *a, **k: calls.append(('install', a, k)) or False,
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

    assert [item[0] for item in calls] == [
        'host',
        'guest-sync',
        'install',
        'replay',
    ]


def test_persistent_reconcile_skips_replay_when_not_forced_and_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-reconcile-skip'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg_path = tmp_path / 'config.toml'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: calls.append(('host', a, k))
        or _persistent_host_manifest_path(cfg),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: calls.append(('guest-sync', a, k)) or False,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._install_persistent_attachment_replay',
        lambda *a, **k: calls.append(('install', a, k)) or False,
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
        replay_even_if_unchanged=False,
    )

    assert [item[0] for item in calls] == ['host', 'guest-sync', 'install']


def test_persistent_manifest_sync_returns_false_when_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-sync-unchanged'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)

    calls: list[list[str]] = []

    class FakeManager:
        def step(self, *args, **kwargs):
            del args, kwargs
            return nullcontext()

        def run(self, cmd, **kwargs):
            del kwargs
            calls.append(list(cmd))
            if cmd and cmd[0] == 'rsync':
                return SimpleNamespace(stdout='')
            return SimpleNamespace(stdout='')

    monkeypatch.setattr(
        'aivm.attachments.persistent.CommandManager.current',
        lambda: FakeManager(),
    )

    changed = _sync_persistent_attachment_manifest_to_guest(
        cfg,
        '10.0.0.5',
        dry_run=False,
    )

    assert changed is False
    assert any(cmd[0] == 'rsync' for cmd in calls)


@pytest.mark.parametrize(
    'phase',
    ['sync', 'install', 'replay'],
)
def test_persistent_reconcile_propagates_primary_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = f'vm-persistent-fail-{phase}'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    _activate_manager(monkeypatch)

    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: cfg_path,
    )
    if phase == 'sync':
        monkeypatch.setattr(
            'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('sync boom')),
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._install_persistent_attachment_replay',
            lambda *a, **k: False,
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._run_guest_root_script',
            lambda *a, **k: None,
        )
        with pytest.raises(RuntimeError, match='sync boom'):
            _reconcile_persistent_attachments_in_guest(
                cfg,
                cfg_path,
                '10.0.0.5',
                dry_run=False,
            )
    elif phase == 'install':
        monkeypatch.setattr(
            'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
            lambda *a, **k: False,
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._install_persistent_attachment_replay',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('install boom')),
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._run_guest_root_script',
            lambda *a, **k: None,
        )
        with pytest.raises(RuntimeError, match='install boom'):
            _reconcile_persistent_attachments_in_guest(
                cfg,
                cfg_path,
                '10.0.0.5',
                dry_run=False,
            )
    else:
        monkeypatch.setattr(
            'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
            lambda *a, **k: False,
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._install_persistent_attachment_replay',
            lambda *a, **k: False,
        )

        def fake_run(*args, **kwargs):
            del args
            if (
                kwargs.get('summary')
                == 'Replay persistent attachment mounts inside guest'
            ):
                raise RuntimeError('replay boom')
            return SimpleNamespace(stdout='')

        monkeypatch.setattr(
            'aivm.attachments.persistent._run_guest_root_script',
            fake_run,
        )
        with pytest.raises(RuntimeError, match='replay boom'):
            _reconcile_persistent_attachments_in_guest(
                cfg,
                cfg_path,
                '10.0.0.5',
                dry_run=False,
            )


def test_persistent_reconcile_continue_on_error_logs_and_continues(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-continue-on-error'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    _activate_manager(monkeypatch)

    warnings: list[str] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent.log.warning',
        lambda fmt, *args: warnings.append(fmt.format(*args)),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: cfg_path,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: (_ for _ in ()).throw(RuntimeError('sync boom')),
    )

    _reconcile_persistent_attachments_in_guest(
        cfg,
        cfg_path,
        '10.0.0.5',
        dry_run=False,
        continue_on_error=True,
    )

    assert any('persistent-reconcile: VM' in msg for msg in warnings)


@pytest.mark.parametrize('phase', ['install', 'replay'])
def test_persistent_reconcile_continue_on_error_logs_and_continues_on_late_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, phase: str
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = f'vm-persistent-continue-on-error-{phase}'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    _activate_manager(monkeypatch)

    warnings: list[str] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent.log.warning',
        lambda fmt, *args: warnings.append(fmt.format(*args)),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: cfg_path,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: False,
    )
    if phase == 'install':
        monkeypatch.setattr(
            'aivm.attachments.persistent._install_persistent_attachment_replay',
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError('install boom')),
        )
        monkeypatch.setattr(
            'aivm.attachments.persistent._run_guest_root_script',
            lambda *a, **k: None,
        )
    else:
        monkeypatch.setattr(
            'aivm.attachments.persistent._install_persistent_attachment_replay',
            lambda *a, **k: False,
        )

        def fake_run(*args, **kwargs):
            del args
            if (
                kwargs.get('summary')
                == 'Replay persistent attachment mounts inside guest'
            ):
                raise RuntimeError('replay boom')
            return SimpleNamespace(stdout='')

        monkeypatch.setattr(
            'aivm.attachments.persistent._run_guest_root_script',
            fake_run,
        )

    _reconcile_persistent_attachments_in_guest(
        cfg,
        cfg_path,
        '10.0.0.5',
        dry_run=False,
        continue_on_error=True,
    )

    assert any('persistent-reconcile: VM' in msg for msg in warnings)


def test_persistent_reconcile_continue_on_error_isolates_outer_command_queue(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-continue-on-error-isolation'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    cfg_path = tmp_path / 'config.toml'
    save_store(Store(), cfg_path)
    outer = CommandManager(yes=True, yes_sudo=True)
    CommandManager.activate(outer)

    pending = outer.submit(
        ['python', '-c', 'import sys; sys.exit(7)'],
        summary='pending outer command',
        eager=False,
    )

    warnings: list[str] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent.log.warning',
        lambda fmt, *args: warnings.append(fmt.format(*args)),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: cfg_path,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._install_persistent_attachment_replay',
        lambda *a, **k: False,
    )
    replay_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent._run_guest_root_script',
        lambda *a, **k: replay_calls.append(k)
        or SimpleNamespace(code=1, stdout='', stderr='replay boom'),
    )

    _reconcile_persistent_attachments_in_guest(
        cfg,
        cfg_path,
        '10.0.0.5',
        dry_run=False,
        continue_on_error=True,
    )

    assert any('persistent-reconcile: VM' in msg for msg in warnings)
    assert replay_calls and replay_calls[0]['check'] is False
    assert pending.done() is False
    with pytest.raises(CommandError):
        pending.result()


def test_persistent_replay_script_nonchecking_path_avoids_error_log(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-nonchecking-replay'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)

    calls: list[list[str]] = []

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        calls.append(list(cmd))
        return SimpleNamespace(returncode=1, stdout='', stderr='replay boom')

    errors: list[tuple] = []
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        fake_subprocess_run,
    )
    monkeypatch.setattr(
        'aivm.commands.log.error',
        lambda *args, **kwargs: errors.append((args, kwargs)),
    )

    with pytest.raises(RuntimeError, match='replay boom'):
        _run_guest_root_script(
            cfg,
            '10.0.0.5',
            script='echo replay',
            summary='Replay persistent attachment mounts inside guest',
            detail='',
            dry_run=False,
            check=False,
        )

    assert calls
    assert not errors


def test_persistent_guest_root_script_retries_transient_banner_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-guest-root-retry'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.ssh_identity_file = str(tmp_path / 'id_ed25519')
    cfg.vm.user = 'agent'
    _activate_manager(monkeypatch)
    monkeypatch.setattr('aivm.attachments.persistent.time.sleep', lambda s: None)

    calls: list[list[str]] = []
    attempts = {'n': 0}

    class FakeManager:
        def run(self, cmd, **kwargs):
            del kwargs
            calls.append(list(cmd))
            attempts['n'] += 1
            if attempts['n'] == 1:
                return SimpleNamespace(
                    code=255,
                    stdout='',
                    stderr=(
                        'Connection timed out during banner exchange\n'
                        'Connection to 10.0.0.5 port 22 timed out'
                    ),
                )
            return SimpleNamespace(code=0, stdout='ok\n', stderr='')

    monkeypatch.setattr(
        'aivm.attachments.persistent.CommandManager.current',
        lambda: FakeManager(),
    )

    result = _run_guest_root_script(
        cfg,
        '10.0.0.5',
        script='echo ok',
        summary='Check guest helper',
        detail='',
        dry_run=False,
        check=True,
    )

    assert attempts['n'] == 2
    assert result is not None
    ssh_cmd = calls[0]
    assert ssh_cmd[0] == 'ssh'
    assert any('ConnectTimeout=15' in arg for arg in ssh_cmd)


def test_persistent_reconcile_replays_when_guest_manifest_changes(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-reconcile-changed'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg_path = tmp_path / 'config.toml'
    _activate_manager(monkeypatch)

    calls: list[tuple[str, tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: calls.append(('host', a, k))
        or _persistent_host_manifest_path(cfg),
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._sync_persistent_attachment_manifest_to_guest',
        lambda *a, **k: calls.append(('guest-sync', a, k)) or True,
    )
    monkeypatch.setattr(
        'aivm.attachments.persistent._install_persistent_attachment_replay',
        lambda *a, **k: calls.append(('install', a, k)) or False,
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

    assert [item[0] for item in calls] == [
        'host',
        'guest-sync',
        'install',
        'replay',
    ]
    assert (
        calls[-1][2]['summary']
        == 'Replay persistent attachment mounts inside guest'
    )


def test_persistent_replay_helper_uses_guest_local_manifest_and_skips_bad_records(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    assert 'HOST_MANIFEST' not in source
    assert '/var/lib/aivm/attachments.json' in source

    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]
    ns['subprocess'].run = _make_guest_replay_fake_run({})  # type: ignore[index]

    mount_root = Path(ns['PERSISTENT_ROOT_MOUNT'])
    mount_root.mkdir(parents=True, exist_ok=True)
    for token in ['parent', 'dup-a', 'dup-b', 'unique']:
        (mount_root / token).mkdir(parents=True, exist_ok=True)

    payload = {
        'schema_version': 1,
        'vm_name': 'vm',
        'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
        'records': [
            {
                'guest_dst': '/workspace/proj',
                'shared_root_token': 'parent',
                'access': 'rw',
                'enabled': True,
            },
            {
                'guest_dst': '/workspace/proj/sub',
                'shared_root_token': 'unique',
                'access': 'ro',
                'enabled': True,
            },
            {
                'guest_dst': '/workspace/dup',
                'shared_root_token': '',
                'access': 'rw',
                'enabled': True,
            },
            {
                'guest_dst': '/workspace/dup',
                'shared_root_token': 'dup-a',
                'access': 'rw',
                'enabled': True,
            },
            {
                'guest_dst': '/workspace/dup',
                'shared_root_token': 'dup-b',
                'access': 'rw',
                'enabled': True,
            },
        ],
    }
    Path(ns['STATE_PATH']).write_text(json.dumps(payload), encoding='utf-8')

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    messages = stderr.getvalue()
    assert 'HOST_MANIFEST' not in messages
    assert 'missing shared_root_token' in messages
    assert (
        'nested persistent attachment child /workspace/proj/sub under /workspace/proj'
        in messages
    )
    assert (
        'duplicate persistent attachment guest_dst /workspace/dup' in messages
    )
    mounts = ns['subprocess'].run.mounts  # type: ignore[attr-defined]
    assert '/workspace/proj' in mounts
    assert '/workspace/dup' in mounts
    assert '/workspace/proj/sub' not in mounts


def test_persistent_replay_helper_treats_plain_root_directory_as_unmounted(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    desired_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'token')
    calls: list[list[str]] = []

    def fake_run(
        cmd,
        check=False,
        capture_output=False,
        text=False,
        stdout=None,
        stderr=None,
        **kwargs,
    ):
        del check, capture_output, text, stdout, stderr, kwargs
        calls.append(list(cmd))
        if cmd[:2] == ['mountpoint', '-q']:
            target = cmd[-1]
            return _FakeSubprocessResult(
                returncode=0
                if target == ns['PERSISTENT_ROOT_MOUNT'] or target in mounts
                else 1
            )
        if cmd and cmd[0] == 'findmnt' and '--target' in cmd:
            return _FakeSubprocessResult(
                stdout='TARGET="/" SOURCE="/dev/vda1" OPTIONS="rw"'
            )
        if cmd and cmd[0] == 'findmnt' and '--mountpoint' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(returncode=1)
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt':
            lines = [
                f'TARGET="{target}" SOURCE="{info["source"]}"'
                for target, info in mounts.items()
            ]
            return _FakeSubprocessResult(stdout='\n'.join(lines))
        if cmd[:2] == ['mount', '-t']:
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and '--bind' in cmd:
            target = cmd[-1]
            source_path = cmd[cmd.index('--bind') + 1]
            mounts[target] = {'source': source_path, 'options': 'rw'}
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,ro' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'ro'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,rw' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'rw'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'umount':
            return _FakeSubprocessResult()
        raise AssertionError(f'unhandled fake command: {cmd}')

    mounts: dict[str, dict[str, str]] = {}
    ns['subprocess'].run = fake_run  # type: ignore[index]

    Path(ns['PERSISTENT_ROOT_MOUNT']).mkdir(parents=True, exist_ok=True)
    Path(desired_source).mkdir(parents=True, exist_ok=True)
    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [
                    {
                        'guest_dst': '/data/joncrall/dvc-repos/shitspotter_expt_dvc',
                        'shared_root_token': 'token',
                        'access': 'rw',
                        'enabled': True,
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    assert any('--mountpoint' in call for call in calls)
    assert not any('--target' in call for call in calls)
    assert (
        mounts['/data/joncrall/dvc-repos/shitspotter_expt_dvc']['source']
        == desired_source
    )
    assert 'busy mount' not in stderr.getvalue()


def test_persistent_replay_helper_replaces_wrong_source_on_real_mountpoint(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    desired_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'desired-token')
    wrong_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'wrong-token')
    mounts = {
        '/workspace/proj': {
            'source': wrong_source,
            'options': 'rw',
        }
    }
    ns['subprocess'].run = _make_guest_replay_fake_run(mounts)  # type: ignore[index]

    Path(ns['PERSISTENT_ROOT_MOUNT']).mkdir(parents=True, exist_ok=True)
    Path(desired_source).mkdir(parents=True, exist_ok=True)
    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [
                    {
                        'guest_dst': '/workspace/proj',
                        'shared_root_token': 'desired-token',
                        'access': 'rw',
                        'enabled': True,
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    assert mounts['/workspace/proj']['source'] == desired_source
    assert 'busy mount' not in stderr.getvalue()


def test_persistent_replay_helper_keeps_correct_real_mountpoint(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    desired_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'desired-token')
    mounts = {
        '/workspace/proj': {
            'source': desired_source,
            'options': 'rw',
        }
    }
    ns['subprocess'].run = _make_guest_replay_fake_run(mounts)  # type: ignore[index]

    Path(ns['PERSISTENT_ROOT_MOUNT']).mkdir(parents=True, exist_ok=True)
    Path(desired_source).mkdir(parents=True, exist_ok=True)
    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [
                    {
                        'guest_dst': '/workspace/proj',
                        'shared_root_token': 'desired-token',
                        'access': 'rw',
                        'enabled': True,
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    assert mounts['/workspace/proj']['source'] == desired_source
    assert stderr.getvalue() == ''


def test_persistent_replay_helper_skips_busy_stale_prune_and_continues(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    stale_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'stale-token')
    mounts = {
        stale_source: {
            'source': stale_source,
            'options': 'rw',
        }
    }

    def fake_run(
        cmd,
        check=False,
        capture_output=False,
        text=False,
        stdout=None,
        stderr=None,
        **kwargs,
    ):
        del check, capture_output, text, stdout, stderr, kwargs
        if cmd[:2] == ['mountpoint', '-q']:
            target = cmd[-1]
            return _FakeSubprocessResult(
                returncode=0 if target in mounts else 1
            )
        if cmd and cmd[0] == 'findmnt' and '--mountpoint' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(returncode=1)
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt' and '--target' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(
                    stdout='TARGET="/" SOURCE="/dev/vda1" OPTIONS="rw"'
                )
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt':
            lines = [
                f'TARGET="{target}" SOURCE="{info["source"]}"'
                for target, info in mounts.items()
            ]
            return _FakeSubprocessResult(stdout='\n'.join(lines))
        if cmd and cmd[0] == 'umount':
            return _FakeSubprocessResult(
                returncode=16, stderr='umount: target is busy'
            )
        if cmd[:2] == ['mount', '-t']:
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and '--bind' in cmd:
            target = cmd[-1]
            source_path = cmd[cmd.index('--bind') + 1]
            mounts[target] = {'source': source_path, 'options': 'rw'}
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,ro' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'ro'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,rw' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'rw'
            return _FakeSubprocessResult()
        raise AssertionError(f'unhandled fake command: {cmd}')

    ns['subprocess'].run = fake_run  # type: ignore[index]

    (Path(ns['PERSISTENT_ROOT_MOUNT']) / 'keep').mkdir(
        parents=True, exist_ok=True
    )
    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [
                    {
                        'guest_dst': '/workspace/keep',
                        'shared_root_token': 'keep',
                        'access': 'rw',
                        'enabled': True,
                    }
                ],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    assert (
        f'WARNING: skipping busy stale persistent attachment mount {stale_source}'
        in stderr.getvalue()
    )
    assert '/workspace/keep' in mounts
    assert stale_source in mounts


def test_persistent_replay_helper_skips_busy_source_replacement_and_continues(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    wrong_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'wrong-token')
    mounts = {
        '/workspace/proj': {
            'source': wrong_source,
            'options': 'rw',
        }
    }

    def fake_run(
        cmd,
        check=False,
        capture_output=False,
        text=False,
        stdout=None,
        stderr=None,
        **kwargs,
    ):
        del check, capture_output, text, stdout, stderr, kwargs
        if cmd[:2] == ['mountpoint', '-q']:
            target = cmd[-1]
            return _FakeSubprocessResult(
                returncode=0 if target in mounts else 1
            )
        if cmd and cmd[0] == 'findmnt' and '--mountpoint' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(returncode=1)
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt' and '--target' in cmd:
            target = cmd[-1]
            info = mounts.get(target)
            if info is None:
                return _FakeSubprocessResult(
                    stdout='TARGET="/" SOURCE="/dev/vda1" OPTIONS="rw"'
                )
            return _FakeSubprocessResult(
                stdout=(
                    f'TARGET="{target}" SOURCE="{info["source"]}" '
                    f'OPTIONS="{info["options"]}"'
                )
            )
        if cmd and cmd[0] == 'findmnt':
            lines = [
                f'TARGET="{target}" SOURCE="{info["source"]}"'
                for target, info in mounts.items()
            ]
            return _FakeSubprocessResult(stdout='\n'.join(lines))
        if cmd and cmd[0] == 'umount':
            return _FakeSubprocessResult(
                returncode=16, stderr='umount: target is busy'
            )
        if cmd[:2] == ['mount', '-t']:
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and '--bind' in cmd:
            target = cmd[-1]
            source_path = cmd[cmd.index('--bind') + 1]
            mounts[target] = {'source': source_path, 'options': 'rw'}
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,ro' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'ro'
            return _FakeSubprocessResult()
        if cmd and cmd[0] == 'mount' and 'remount,bind,rw' in cmd[-2]:
            target = cmd[-1]
            if target in mounts:
                mounts[target]['options'] = 'rw'
            return _FakeSubprocessResult()
        raise AssertionError(f'unhandled fake command: {cmd}')

    ns['subprocess'].run = fake_run  # type: ignore[index]

    Path(ns['PERSISTENT_ROOT_MOUNT']).mkdir(parents=True, exist_ok=True)
    (Path(ns['PERSISTENT_ROOT_MOUNT']) / 'desired-token').mkdir(
        parents=True, exist_ok=True
    )
    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [
                    {
                        'guest_dst': '/workspace/proj',
                        'shared_root_token': 'desired-token',
                        'access': 'rw',
                        'enabled': True,
                    },
                    {
                        'guest_dst': '/workspace/keep',
                        'shared_root_token': 'desired-token',
                        'access': 'rw',
                        'enabled': True,
                    },
                ],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    messages = stderr.getvalue()
    assert (
        'WARNING: skipping persistent attachment replacement for busy mount /workspace/proj'
        in messages
    )
    assert '/workspace/keep' in mounts
    assert mounts['/workspace/proj']['source'] == wrong_source


def test_persistent_replay_helper_removes_nonbusy_stale_mount(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]

    stale_source = str(Path(ns['PERSISTENT_ROOT_MOUNT']) / 'stale-token')
    mounts = {
        stale_source: {
            'source': stale_source,
            'options': 'rw',
        }
    }
    ns['subprocess'].run = _make_guest_replay_fake_run(mounts)  # type: ignore[index]

    Path(ns['STATE_PATH']).write_text(
        json.dumps(
            {
                'schema_version': 1,
                'vm_name': 'vm',
                'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
                'records': [],
            }
        ),
        encoding='utf-8',
    )

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    assert stale_source not in mounts


def test_persistent_replay_helper_allows_enabled_child_under_disabled_parent(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]
    ns['subprocess'].run = _make_guest_replay_fake_run({})  # type: ignore[index]

    mount_root = Path(ns['PERSISTENT_ROOT_MOUNT'])
    mount_root.mkdir(parents=True, exist_ok=True)
    for token in ['parent', 'child']:
        (mount_root / token).mkdir(parents=True, exist_ok=True)

    payload = {
        'schema_version': 1,
        'vm_name': 'vm',
        'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
        'records': [
            {
                'guest_dst': '/workspace/proj',
                'shared_root_token': 'parent',
                'access': 'rw',
                'enabled': False,
            },
            {
                'guest_dst': '/workspace/proj/sub',
                'shared_root_token': 'child',
                'access': 'rw',
                'enabled': True,
            },
        ],
    }
    Path(ns['STATE_PATH']).write_text(json.dumps(payload), encoding='utf-8')

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    messages = stderr.getvalue()
    assert 'ignoring nested persistent attachment child' not in messages
    mounts = ns['subprocess'].run.mounts  # type: ignore[attr-defined]
    assert '/workspace/proj/sub' in mounts
    assert '/workspace/proj' not in mounts


def test_persistent_replay_helper_ignores_enabled_child_under_enabled_parent(
    tmp_path: Path,
) -> None:
    from aivm.persistent_replay import persistent_replay_python

    source = persistent_replay_python()
    ns = _exec_guest_replay_helper(source)
    ns['PERSISTENT_ROOT_MOUNT'] = str(tmp_path / 'mnt')
    ns['STATE_PATH'] = str(tmp_path / 'attachments.json')
    ns['os'].makedirs = lambda *a, **k: None  # type: ignore[attr-defined]
    ns['subprocess'].run = _make_guest_replay_fake_run({})  # type: ignore[index]

    mount_root = Path(ns['PERSISTENT_ROOT_MOUNT'])
    mount_root.mkdir(parents=True, exist_ok=True)
    for token in ['parent', 'child']:
        (mount_root / token).mkdir(parents=True, exist_ok=True)

    payload = {
        'schema_version': 1,
        'vm_name': 'vm',
        'shared_root_mount': ns['PERSISTENT_ROOT_MOUNT'],
        'records': [
            {
                'guest_dst': '/workspace/proj',
                'shared_root_token': 'parent',
                'access': 'rw',
                'enabled': True,
            },
            {
                'guest_dst': '/workspace/proj/sub',
                'shared_root_token': 'child',
                'access': 'rw',
                'enabled': True,
            },
        ],
    }
    Path(ns['STATE_PATH']).write_text(json.dumps(payload), encoding='utf-8')

    stderr = StringIO()
    with redirect_stderr(stderr):
        ns['main']()

    messages = stderr.getvalue()
    assert (
        'WARNING: ignoring nested persistent attachment child /workspace/proj/sub under /workspace/proj'
        in messages
    )
    mounts = ns['subprocess'].run.mounts  # type: ignore[attr-defined]
    assert '/workspace/proj' in mounts
    assert '/workspace/proj/sub' not in mounts
