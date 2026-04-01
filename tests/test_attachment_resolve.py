"""Tests for _resolve_attachment, normalization helpers, tag generation, and guest_dst resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from aivm.vm.share import AttachmentAccess, AttachmentMode
from aivm.attachments.resolve import (
    _compute_mirror_home_symlink,
    _default_primary_guest_dst,
    _host_symlink_lexical_path,
    _resolve_attachment,
)
from aivm.config import AgentVMConfig
from aivm.store import (
    AttachmentEntry,
    Store,
    save_store,
)


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

    msg = ''
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
