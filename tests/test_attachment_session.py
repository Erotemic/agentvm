"""Tests for VMAttachCLI workflow, record_attachment, restore flows, session prep."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aivm.attachments.session import _record_attachment
from aivm.cli.vm import VMSSHCLI, VMAttachCLI, VMCodeCLI
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
        lambda *a, **k: resolved.append((a, k)) or '10.77.0.55',
    )

    mounted: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.attachments.guest.ensure_share_mounted',
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
    refreshes: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.refresh_cloud_init_seed_for_next_boot',
        lambda *a, **k: refreshes.append((a, k)) or None,
    )
    monkeypatch.setattr(
        'aivm.attachments.guest.ensure_share_mounted',
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


def test_vm_attach_persistent_syncs_manifest_and_replays_when_running(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-running'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.PERSISTENT,
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
            ProbeOutcome(True, 'vm-persistent-running state=running'),
            True,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: '10.77.0.77',
    )

    syncs: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: syncs.append((a, k)) or cfg_path,
    )
    guest_mounts: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._ensure_attachment_available_in_guest',
        lambda *a, **k: guest_mounts.append((a, k)) or None,
    )
    replays: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_persistent_attachments_in_guest',
        lambda *a, **k: replays.append((a, k)) or None,
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        mode='persistent',
        yes=True,
    )

    assert rc == 0
    assert syncs
    assert guest_mounts
    assert replays
    assert guest_mounts[0][1]['ensure_shared_root_host_side'] is True


def test_vm_attach_persistent_prepares_dedicated_export_when_vm_stopped(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-persistent-stopped'
    cfg_path = tmp_path / 'config.toml'
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode=AttachmentMode.PERSISTENT,
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
            ProbeOutcome(False, 'vm-persistent-stopped state=shut off'),
            True,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: cfg_path
    )
    syncs: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._sync_persistent_attachment_manifest_on_host',
        lambda *a, **k: syncs.append((a, k)) or cfg_path,
    )
    prepares: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm._prepare_persistent_attachment_host_and_vm',
        lambda *a, **k: prepares.append((a, k)) or None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_ip_for_ssh_ops',
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError('_resolve_ip_for_ssh_ops should not be called')
        ),
    )
    refreshes: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.refresh_cloud_init_seed_for_next_boot',
        lambda *a, **k: refreshes.append((a, k)) or None,
    )

    rc = VMAttachCLI.main(
        argv=False,
        config=str(cfg_path),
        host_src=str(host_src),
        mode='persistent',
        yes=True,
    )

    assert rc == 0
    assert prepares
    assert prepares[0][1]['vm_running'] is False
    assert syncs
    assert refreshes


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
        'aivm.attachments.guest.ensure_share_mounted',
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
        'aivm.attachments.guest._ensure_git_clone_attachment',
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
        'aivm.attachments.session.save_store',
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
        'aivm.attachments.session.save_store',
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


def test_git_mode_in_prepare_session_gets_companion_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git mode in _prepare_attached_session creates a companion symlink for host symlinks."""
    from aivm.attachments.session import _prepare_attached_session

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
        'aivm.attachments.session._resolve_cfg_for_code',
        lambda **k: (cfg, cfg_path),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.attachments.session._reconcile_attached_vm',
        lambda *a, **k: type(
            'R',
            (),
            {
                'attachment': attachment,
                'cached_ip': '10.0.0.1',
                'shared_root_host_side_ready': False,
            },
        )(),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._maybe_offer_create_ssh_identity',
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.attachments.session.probe_ssh_ready',
        lambda *a, **k: type('P', (), {'ok': True})(),
    )
    monkeypatch.setattr('aivm.attachments.session.load_store', lambda p: store)

    git_calls: list = []
    monkeypatch.setattr(
        'aivm.attachments.session._ensure_git_clone_attachment',
        lambda *a, **k: git_calls.append(1) or (tmp_path, 'ssh', 'git'),
    )

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.guest._ensure_guest_symlink',
        lambda cfg_a, ip, *, symlink_path, target_path: symlink_calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )

    monkeypatch.setattr(
        'aivm.attachments.session._restore_saved_vm_attachments',
        lambda *a, **k: None,
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
        c['symlink_path'] == expected_link
        and c['target_path'] == expected_target
        for c in symlink_calls
    ), (
        f'Expected companion symlink {expected_link} -> {expected_target}, got: {symlink_calls}'
    )


def test_git_mode_in_prepare_session_gets_mirror_home_symlink(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Git mode in _prepare_attached_session creates a mirror-home symlink when enabled."""
    from aivm.attachments.session import _prepare_attached_session

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
        'aivm.attachments.session._resolve_cfg_for_code',
        lambda **k: (cfg, cfg_path),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._resolve_attachment',
        lambda *a, **k: attachment,
    )
    monkeypatch.setattr(
        'aivm.attachments.session._reconcile_attached_vm',
        lambda *a, **k: type(
            'R',
            (),
            {
                'attachment': attachment,
                'cached_ip': '10.0.0.1',
                'shared_root_host_side_ready': False,
            },
        )(),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._maybe_offer_create_ssh_identity',
        lambda *a, **k: False,
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
    )
    monkeypatch.setattr(
        'aivm.attachments.session.probe_ssh_ready',
        lambda *a, **k: type('P', (), {'ok': True})(),
    )
    monkeypatch.setattr('aivm.attachments.session.load_store', lambda p: store)

    monkeypatch.setattr(
        'aivm.attachments.session._ensure_git_clone_attachment',
        lambda *a, **k: (tmp_path, 'ssh', 'git'),
    )

    symlink_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.guest._ensure_guest_symlink',
        lambda cfg_a, ip, *, symlink_path, target_path: symlink_calls.append(
            {'symlink_path': symlink_path, 'target_path': target_path}
        ),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._restore_saved_vm_attachments',
        lambda *a, **k: None,
    )

    # Patch Path.home so we know what the mirror path will be
    host_home = tmp_path
    monkeypatch.setattr('aivm.attachments.resolve.Path.home', lambda: host_home)

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
    assert any(c['symlink_path'] == expected_mirror for c in symlink_calls), (
        f'Expected mirror symlink at {expected_mirror}, got: {symlink_calls}'
    )


def test_restore_shared_attachment_applies_guest_derived_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """_restore_saved_vm_attachments applies _apply_guest_derived_symlinks for shared mode."""
    from aivm.attachments.session import _restore_saved_vm_attachments

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
        'aivm.attachments.session._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.vm_share_mappings',
        lambda *a, **k: [(str(secondary_src), 'tag-secondary')],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.ensure_share_mounted', lambda *a, **k: None
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
    )

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.session._apply_guest_derived_symlinks',
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
    from aivm.attachments.session import _restore_saved_vm_attachments

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
        'aivm.attachments.session._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )

    ensure_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.session._ensure_attachment_available_in_guest',
        lambda cfg_a, host_src_a, att, ip, *, yes, dry_run, ensure_shared_root_host_side, allow_disruptive_shared_root_rebind, mirror_home: (
            ensure_calls.append(
                {
                    'allow_disruptive': allow_disruptive_shared_root_rebind,
                    'mirror_home': mirror_home,
                }
            )
        ),
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
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


def test_record_attachment_persists_lexical_path_for_symlink(
    tmp_path: Path,
) -> None:
    """_record_attachment stores host_lexical_path when host_src is a symlink."""
    from aivm.attachments.session import _record_attachment

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
    from aivm.attachments.session import _record_attachment

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
    from aivm.attachments.session import _restore_saved_vm_attachments

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
        source_dir=str(
            tmp_path / 'primary'
        ),  # different source so secondary runs
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
        'aivm.attachments.session._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.vm_share_mappings',
        lambda *a, **k: [(str(real_dir), 'tag-lex-restore')],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.ensure_share_mounted', lambda *a, **k: None
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
    )

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.session._apply_guest_derived_symlinks',
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
    from aivm.attachments.session import _restore_saved_vm_attachments

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
        'aivm.attachments.session._saved_vm_attachments',
        lambda *a, **k: [primary, secondary],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.vm_share_mappings',
        lambda *a, **k: [(str(real_dir), 'tag-plain')],
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_align_attachment_tag_with_mappings',
        lambda att, *a, **k: att,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.drift_attachment_has_mapping',
        lambda cfg_a, att, mappings: True,
    )
    monkeypatch.setattr(
        'aivm.attachments.session.ensure_share_mounted', lambda *a, **k: None
    )
    monkeypatch.setattr(
        'aivm.attachments.session._record_attachment', lambda *a, **k: cfg_path
    )

    derived_calls: list[dict] = []
    monkeypatch.setattr(
        'aivm.attachments.session._apply_guest_derived_symlinks',
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
