"""Tests for test vm helpers."""

from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from aivm.cli.vm import ResolvedAttachment, _ensure_shared_root_host_bind
from aivm.commands import CommandManager
from aivm.config import (
    DEFAULT_UBUNTU_NOBLE_IMG_URL,
    AgentVMConfig,
)
from aivm.util import CmdError, CmdResult
from aivm.vm import (
    _ensure_qemu_access,
    _mac_for_vm,
    _write_cloud_init,
    attach_vm_share,
    create_or_start_vm,
    ensure_share_mounted,
    fetch_image,
    get_ip_cached,
    vm_has_share,
    vm_has_virtiofs_shared_memory,
    vm_share_mappings,
    wait_for_ssh,
)


def _activate_manager(*, yes_sudo: bool = True) -> None:
    CommandManager.activate(CommandManager(yes_sudo=yes_sudo))


class _Proc:
    def __init__(self, returncode=0, stdout='', stderr=''):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def test_mac_for_vm_parsing(monkeypatch) -> None:
    stdout = """
 Interface   Type      Source     Model    MAC
---------------------------------------------------------------
 vnet0       network   default    virtio   52:54:00:12:34:56
"""
    monkeypatch.setattr(
        'aivm.vm.lifecycle.run_cmd', lambda *a, **k: CmdResult(0, stdout, '')
    )
    cfg = AgentVMConfig()
    assert _mac_for_vm(cfg) == '52:54:00:12:34:56'


def test_get_ip_cached(tmp_path: Path) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.state_dir = str(tmp_path)
    ip_dir = tmp_path / 'vmx'
    ip_dir.mkdir()
    (ip_dir / 'vmx.ip').write_text('10.77.0.123\n', encoding='utf-8')
    assert get_ip_cached(cfg) == '10.77.0.123'


def test_vm_share_helpers(monkeypatch, tmp_path: Path) -> None:
    source = tmp_path / 'src'
    source.mkdir()
    cfg = AgentVMConfig()
    source_dir = str(source)
    share_tag = 'hostcode-src'
    xml = f"""
<domain>
  <devices>
    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtiofs'/>
      <source dir='{source.resolve()}'/>
      <target dir='hostcode-src'/>
    </filesystem>
    <filesystem type='mount' accessmode='passthrough'>
      <driver type='virtio-9p'/>
      <source dir='/opt/other'/>
      <target dir='other'/>
    </filesystem>
  </devices>
</domain>
"""
    _activate_manager()
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: _Proc(0, xml, ''),
    )
    assert vm_has_share(cfg, source_dir, share_tag, use_sudo=False) is True
    assert vm_share_mappings(cfg, use_sudo=False) == [
        (str(source.resolve()), 'hostcode-src'),
    ]


def test_vm_has_virtiofs_shared_memory(monkeypatch) -> None:
    cfg = AgentVMConfig()
    _activate_manager()
    xml_with_shared = """
<domain>
  <memoryBacking>
    <source type='memfd'/>
    <access mode='shared'/>
  </memoryBacking>
</domain>
"""
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: _Proc(0, xml_with_shared, ''),
    )
    assert vm_has_virtiofs_shared_memory(cfg, use_sudo=False) is True

    xml_without_shared = '<domain><memoryBacking/></domain>'
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: _Proc(0, xml_without_shared, ''),
    )
    assert vm_has_virtiofs_shared_memory(cfg, use_sudo=False) is False


def test_attach_vm_share_treats_existing_mapping_as_satisfied(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    source = tmp_path / 'src'
    source.mkdir()
    source_dir = str(source.resolve())
    tag = 'hostcode-src'

    calls: list[list[str]] = []

    _activate_manager()
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: False)

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        parts = list(cmd)
        calls.append(parts)
        normalized = parts
        if normalized[:2] == ['sudo', '-n']:
            normalized = normalized[2:]
        elif normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:2] == ['virsh', 'domstate']:
            return _Proc(0, 'running\n', '')
        if normalized[:2] == ['virsh', 'attach-device']:
            return _Proc(
                1,
                '',
                'error: Requested operation is not valid: Target already exists',
            )
        raise AssertionError(f'unexpected command: {cmd!r}')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    monkeypatch.setattr(
        'aivm.vm.share.vm_share_mappings',
        lambda *_a, **_k: [(source_dir, tag)],
    )

    attach_vm_share(cfg, source_dir, tag, dry_run=False)
    norm0 = calls[0][2:] if calls[0][:2] == ['sudo', '-n'] else calls[0]
    norm1 = calls[1][2:] if calls[1][:2] == ['sudo', '-n'] else calls[1]
    assert norm0[:2] == ['virsh', 'domstate']
    assert norm1[:2] == ['virsh', 'attach-device']


def test_ensure_share_mounted_retries_then_succeeds(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    calls = {'n': 0}
    sleeps: list[float] = []

    monkeypatch.setattr(
        'aivm.vm.share.require_ssh_identity', lambda p: p or '/tmp/id_ed25519'
    )
    monkeypatch.setattr(
        'aivm.vm.share.ssh_base_args', lambda *a, **k: ['-i', '/tmp/id_ed25519']
    )

    def fake_run_cmd(*a, **k):
        del a, k
        calls['n'] += 1
        if calls['n'] == 1:
            return CmdResult(
                32,
                '',
                'mount: /workspace: wrong fs type, bad option',
            )
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.share.run_cmd', fake_run_cmd)
    monkeypatch.setattr('aivm.vm.share.time.sleep', lambda s: sleeps.append(s))
    ensure_share_mounted(
        cfg,
        '10.0.0.2',
        guest_dst='/workspace',
        tag='hostcode-workspace',
        dry_run=False,
    )
    assert calls['n'] == 2
    assert sleeps == [2.0]


def test_ensure_share_mounted_raises_after_retries(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-share-fail'
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    sleeps: list[float] = []

    monkeypatch.setattr(
        'aivm.vm.share.require_ssh_identity', lambda p: p or '/tmp/id_ed25519'
    )
    monkeypatch.setattr(
        'aivm.vm.share.ssh_base_args', lambda *a, **k: ['-i', '/tmp/id_ed25519']
    )
    monkeypatch.setattr(
        'aivm.vm.share.run_cmd',
        lambda *a, **k: CmdResult(
            32,
            '',
            'mount: /workspace: wrong fs type, bad option',
        ),
    )
    monkeypatch.setattr('aivm.vm.share.time.sleep', lambda s: sleeps.append(s))

    with pytest.raises(
        RuntimeError, match='Failed to mount shared folder inside guest'
    ):
        ensure_share_mounted(
            cfg,
            '10.0.0.2',
            guest_dst='/workspace',
            tag='hostcode-workspace',
            dry_run=False,
        )
    assert len(sleeps) == 11


def test_ensure_share_mounted_read_only_uses_ro_option(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    cmds: list[list[str]] = []
    run_kwargs: list[dict] = []

    monkeypatch.setattr(
        'aivm.vm.share.require_ssh_identity', lambda p: p or '/tmp/id_ed25519'
    )
    monkeypatch.setattr(
        'aivm.vm.share.ssh_base_args', lambda *a, **k: ['-i', '/tmp/id_ed25519']
    )

    def fake_run_cmd(cmd, **kwargs):
        cmds.append([str(c) for c in cmd])
        run_kwargs.append(dict(kwargs))
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.share.run_cmd', fake_run_cmd)

    ensure_share_mounted(
        cfg,
        '10.0.0.2',
        guest_dst='/workspace',
        tag='hostcode-workspace',
        read_only=True,
        dry_run=False,
    )

    assert len(cmds) == 1
    remote_script = cmds[0][-1]
    assert run_kwargs[0]['timeout'] == 20
    assert 'sudo -n mount -t virtiofs -o ro' in remote_script
    assert 'mount -t virtiofs -o ro' in remote_script


def test_create_vm_fallback_when_uefi_firmware_missing(monkeypatch) -> None:
    cfg = AgentVMConfig()
    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *a, **k: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.fetch_image', lambda *a, **k: Path('/tmp/base.img')
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._write_cloud_init',
        lambda *a, **k: {'seed_iso': Path('/tmp/seed.iso')},
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_disk', lambda *a, **k: Path('/tmp/vm.qcow2')
    )

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd[0] == 'virt-install' and '--boot' in cmd:
            raise CmdError(
                cmd,
                CmdResult(
                    1,
                    '',
                    "ERROR    Did not find any UEFI binary path for arch 'x86_64'",
                ),
            )
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    create_or_start_vm(cfg, dry_run=False, recreate=False)

    virt_calls = [c for c in calls if c and c[0] == 'virt-install']
    assert len(virt_calls) == 2
    assert '--memorybacking' in virt_calls[0]
    assert '--memorybacking' in virt_calls[1]
    assert '--tpm' in virt_calls[0]
    assert 'none' in virt_calls[0]
    assert '--tpm' in virt_calls[1]
    assert 'none' in virt_calls[1]
    assert '--boot' in virt_calls[0]
    assert 'uefi,loader.secure=no,bios.useserial=on' in virt_calls[0]
    assert '--boot' not in virt_calls[1]


def test_create_vm_prefers_uefi_even_when_host_looks_nested(
    monkeypatch,
) -> None:
    cfg = AgentVMConfig()
    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *a, **k: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.fetch_image', lambda *a, **k: Path('/tmp/base.img')
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._write_cloud_init',
        lambda *a, **k: {'seed_iso': Path('/tmp/seed.iso')},
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_disk', lambda *a, **k: Path('/tmp/vm.qcow2')
    )

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    create_or_start_vm(cfg, dry_run=False, recreate=False)

    virt_calls = [c for c in calls if c and c[0] == 'virt-install']
    assert len(virt_calls) == 1
    assert '--memorybacking' in virt_calls[0]
    assert '--tpm' in virt_calls[0]
    assert 'none' in virt_calls[0]
    assert '--boot' in virt_calls[0]
    assert 'uefi,loader.secure=no,bios.useserial=on' in virt_calls[0]


def test_write_cloud_init_user_data_avoids_invalid_datasource_keys(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path / 'base')
    pubkey_path = tmp_path / 'id_ed25519.pub'
    pubkey_path.write_text(
        'ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAITestKey agent@test\n',
        encoding='utf-8',
    )
    cfg.paths.ssh_pubkey_path = str(pubkey_path)
    heredocs: dict[str, str] = {}

    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )

    class P:
        def __init__(self, returncode=0, stdout='', stderr=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = cmd[1:] if cmd and cmd[0] == 'sudo' else cmd
        if normalized[:2] == ['bash', '-lc'] and 'cat > ' in normalized[2]:
            script = normalized[2]
            if 'user-data' in script:
                heredocs['user-data'] = script
        return P(0, '', '')

    CommandManager.activate(CommandManager(yes_sudo=True))
    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 1000)
    monkeypatch.setattr('aivm.commands.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    _write_cloud_init(cfg, dry_run=False)
    user_data_script = heredocs['user-data']
    assert '#cloud-config' in user_data_script
    assert 'datasource_list:' not in user_data_script
    assert '\ndatasource:\n' not in user_data_script


def test_fetch_image_uses_atomic_temp_then_move(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )
    calls = []
    expected = (
        '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21'
    )

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{expected}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'noble-base.img'
    curl_calls = [c for c in calls if c and c[0] == 'curl']
    assert len(curl_calls) == 1
    mv_calls = [c for c in calls if c and c[0] == 'mv']
    assert len(mv_calls) == 1
    sha_calls = [c for c in calls if c and c[0] == 'sha256sum']
    assert len(sha_calls) == 1
    tmp_target = str(out) + '.part'
    assert tmp_target in curl_calls[0]
    assert tmp_target in mv_calls[0]
    assert str(out) in mv_calls[0]


def test_fetch_image_revalidates_cached_image_before_reuse(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    expected = (
        '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21'
    )
    calls = []

    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: True)

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{expected}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'noble-base.img'
    assert any(c[:1] == ['sha256sum'] for c in calls)
    assert not any(c[:1] == ['curl'] for c in calls)
    assert not any(c[:2] == ['cp', '--reflink=auto'] for c in calls)


def test_fetch_image_redownloads_when_cached_hash_is_stale(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    expected = (
        '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21'
    )
    calls = []
    sha_calls = 0

    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: True)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )

    def fake_subprocess_run(cmd, **kwargs):
        nonlocal sha_calls
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:1] == ['sha256sum']:
            sha_calls += 1
            digest = 'bad' * 21 + 'b' if sha_calls == 1 else expected
            return _Proc(0, f'{digest[:64]}  {normalized[-1]}\n', '')
        if normalized[:6] == ['curl', '-L', '--fail', '--progress-bar', '-o']:
            return _Proc(0, '', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'noble-base.img'
    assert sha_calls >= 2
    assert any(c[:2] == ['rm', '-f'] for c in calls)
    assert any(c[:1] == ['curl'] for c in calls)
    assert any(c[:2] == ['mv', '-f'] for c in calls)


def test_fetch_image_validates_ubuntu_checksum(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )
    calls = []
    expected = (
        '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21'
    )

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:6] == ['curl', '-L', '--fail', '--progress-bar', '-o']:
            return _Proc(0, '', '')
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{expected}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'noble-base.img'
    assert any(c[:1] == ['sha256sum'] for c in calls)


def test_fetch_image_raises_on_checksum_mismatch(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )
    calls = []
    actual = 'abcdef0123456789abcdef0123456789abcdef0123456789abcdef0123456789'

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:6] == ['curl', '-L', '--fail', '--progress-bar', '-o']:
            return _Proc(0, '', '')
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{actual}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    with pytest.raises(RuntimeError, match='checksum mismatch'):
        fetch_image(cfg, dry_run=False)
    assert any(c[:2] == ['rm', '-f'] for c in calls)


def test_fetch_image_rejects_unsupported_url(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'base.img'
    cfg.image.ubuntu_img_url = 'https://example.com/custom.img'
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    with pytest.raises(
        RuntimeError, match='not in the built-in verified image registry'
    ):
        fetch_image(cfg, dry_run=False)


def test_fetch_image_accepts_supported_file_url(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.image.cache_name = 'noble-base.img'
    local_img = tmp_path / 'source.img'
    local_img.write_bytes(b'e2e-source-image')
    digest = sha256(local_img.read_bytes()).hexdigest()
    cfg.image.ubuntu_img_url = f'file://{local_img}'
    monkeypatch.setattr(
        'aivm.vm.lifecycle.SUPPORTED_IMAGE_SHA256',
        {DEFAULT_UBUNTU_NOBLE_IMG_URL: digest},
    )
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )

    calls = []

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        calls.append(normalized)
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{digest}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'noble-base.img'
    assert any(c[:2] == ['cp', '--reflink=auto'] for c in calls)
    assert any(c[:2] == ['mv', '-f'] for c in calls)


def test_fetch_image_preview_uses_grouped_block_summaries(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'noble-base.img'
    cfg.image.ubuntu_img_url = DEFAULT_UBUNTU_NOBLE_IMG_URL
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )
    messages: list[str] = []
    expected = (
        '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21'
    )

    class _FakeLog:
        def info(self, fmt: str, *args) -> None:
            messages.append(fmt.format(*args))

        def debug(self, fmt: str, *args) -> None:
            return None

        def trace(self, fmt: str, *args) -> None:
            return None

        def warning(self, fmt: str, *args) -> None:
            messages.append(fmt.format(*args))

        def error(self, fmt: str, *args) -> None:
            messages.append(fmt.format(*args))

    monkeypatch.setattr('aivm.commands.log.opt', lambda **kwargs: _FakeLog())

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:1] == ['sudo']:
            normalized = normalized[1:]
        if normalized[:1] == ['-n']:
            normalized = normalized[1:]
        if normalized[:1] == ['sha256sum']:
            return _Proc(0, f'{expected}  {normalized[-1]}\n', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    fetch_image(cfg, dry_run=False)

    assert 'Step: Fetch and verify base image' in messages
    assert '  1. Create VM image directory' in messages
    assert '  2. Remove stale partial image file' in messages
    assert '  3. Download base image into staging file' in messages
    assert '  4. Move staged base image into cache' in messages
    assert '  5. Compute base image checksum' in messages


def test_qemu_access_does_not_recurse_vm_root_after_shared_root_bind(
    monkeypatch, tmp_path: Path
) -> None:
    _activate_manager()
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-bind-safe'
    cfg.paths.base_dir = str(tmp_path / 'base')
    source_dir = tmp_path / 'source'
    source_dir.mkdir()
    attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        mode='shared-root',
        access='rw',
        source_dir=str(source_dir.resolve()),
        guest_dst='/workspace/source',
        tag='hostcode-source',
    )
    calls: list[list[str]] = []

    def fake_run_cmd(cmd, **kwargs):
        del kwargs
        calls.append([str(part) for part in cmd])
        if cmd[:2] == ['getent', 'group']:
            return CmdResult(0, 'libvirt-qemu:x:1:\n', '')
        return CmdResult(0, '', '')

    def fake_subprocess_run(cmd, **kwargs):
        del kwargs
        normalized = [str(part) for part in cmd]
        if normalized[:2] == ['sudo', '-n']:
            normalized = normalized[2:]
        calls.append(normalized)
        if normalized[:2] == ['findmnt', '-n']:
            return _Proc(1, '', '')
        return _Proc(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    monkeypatch.setattr('aivm.commands.subprocess.run', fake_subprocess_run)

    _ensure_shared_root_host_bind(
        cfg,
        attachment,
        yes=True,
        dry_run=False,
    )
    _ensure_qemu_access(cfg, dry_run=False)

    command_text = [' '.join(c) for c in calls]
    base_root = str(Path(cfg.paths.base_dir) / cfg.vm.name)
    assert any(
        line.startswith(f'mount --bind {source_dir}') for line in command_text
    )
    assert f'chown -R root:libvirt-qemu {base_root}' not in command_text
    assert f'chown -R root:kvm {base_root}' not in command_text


def test_fetch_image_rejects_unsupported_file_url_digest(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.image.cache_name = 'noble-base.img'
    local_img = tmp_path / 'bad.img'
    local_img.write_bytes(b'corrupt-partial')
    cfg.image.ubuntu_img_url = f'file://{local_img}'
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    with pytest.raises(
        RuntimeError,
        match='digest is not in the built-in verified image registry',
    ):
        fetch_image(cfg, dry_run=False)


def test_wait_for_ssh_uses_generous_probe_timeout(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.user = 'agent'
    cfg.paths.ssh_identity_file = '/tmp/id_ed25519'
    timeouts: list[int | None] = []
    calls = {'n': 0}

    monkeypatch.setattr(
        'aivm.vm.lifecycle.require_ssh_identity',
        lambda p: p or '/tmp/id_ed25519',
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle.ssh_base_args',
        lambda *a, **k: ['-i', '/tmp/id_ed25519'],
    )
    monkeypatch.setattr('aivm.vm.lifecycle.time.sleep', lambda s: None)

    def fake_run_cmd(cmd, **kwargs):
        del cmd
        calls['n'] += 1
        timeouts.append(kwargs.get('timeout'))
        if calls['n'] == 1:
            return CmdResult(124, '', 'command timed out')
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    wait_for_ssh(cfg, '10.0.0.2', timeout_s=60, dry_run=False)
    assert calls['n'] == 2
    assert all(timeout == 30 for timeout in timeouts)


def test_create_vm_raises_clear_error_when_virtiofsd_missing(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    source_dir = str(tmp_path)
    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *a, **k: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.fetch_image', lambda *a, **k: Path('/tmp/base.img')
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._write_cloud_init',
        lambda *a, **k: {'seed_iso': Path('/tmp/seed.iso')},
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_disk', lambda *a, **k: Path('/tmp/vm.qcow2')
    )

    def fake_run_cmd(cmd, **kwargs):
        if cmd and cmd[0] == 'virt-install':
            raise CmdError(
                cmd,
                CmdResult(
                    1,
                    '',
                    'operation failed: Unable to find a satisfying virtiofsd',
                ),
            )
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    with pytest.raises(RuntimeError, match='virtiofsd is not available'):
        create_or_start_vm(
            cfg,
            dry_run=False,
            recreate=False,
            share_source_dir=source_dir,
            share_tag='hostcode',
        )


def test_create_vm_raises_clear_error_when_guest_memory_unavailable(
    monkeypatch,
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.vm.ram_mb = 8192
    cfg.vm.cpus = 4
    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *a, **k: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.fetch_image', lambda *a, **k: Path('/tmp/base.img')
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._write_cloud_init',
        lambda *a, **k: {'seed_iso': Path('/tmp/seed.iso')},
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_disk', lambda *a, **k: Path('/tmp/vm.qcow2')
    )

    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        if cmd and cmd[0] == 'virt-install' and '--boot' in cmd:
            raise CmdError(
                cmd,
                CmdResult(
                    1,
                    '',
                    "ERROR    Did not find any UEFI binary path for arch 'x86_64'",
                ),
            )
        if cmd and cmd[0] == 'virt-install':
            raise CmdError(
                cmd,
                CmdResult(
                    1,
                    '',
                    "qemu-system-x86_64: cannot set up guest memory 'pc.ram': Cannot allocate memory",
                ),
            )
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    with pytest.raises(RuntimeError, match='could not allocate guest RAM'):
        create_or_start_vm(cfg, dry_run=False, recreate=False)
