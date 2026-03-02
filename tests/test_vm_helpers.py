"""Tests for test vm helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from aivm.config import AgentVMConfig
from aivm.util import CmdError, CmdResult
from aivm.vm import (
    _mac_for_vm,
    create_or_start_vm,
    fetch_image,
    get_ip_cached,
    vm_has_share,
    vm_has_virtiofs_shared_memory,
    vm_share_mappings,
)


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
    monkeypatch.setattr(
        'aivm.vm.share.run_cmd', lambda *a, **k: CmdResult(0, xml, '')
    )
    assert vm_has_share(cfg, source_dir, share_tag, use_sudo=False) is True
    assert vm_share_mappings(cfg, use_sudo=False) == [
        (str(source.resolve()), 'hostcode-src'),
    ]


def test_vm_has_virtiofs_shared_memory(monkeypatch) -> None:
    cfg = AgentVMConfig()
    xml_with_shared = """
<domain>
  <memoryBacking>
    <source type='memfd'/>
    <access mode='shared'/>
  </memoryBacking>
</domain>
"""
    monkeypatch.setattr(
        'aivm.vm.share.run_cmd',
        lambda *a, **k: CmdResult(0, xml_with_shared, ''),
    )
    assert vm_has_virtiofs_shared_memory(cfg, use_sudo=False) is True

    xml_without_shared = '<domain><memoryBacking/></domain>'
    monkeypatch.setattr(
        'aivm.vm.share.run_cmd',
        lambda *a, **k: CmdResult(0, xml_without_shared, ''),
    )
    assert vm_has_virtiofs_shared_memory(cfg, use_sudo=False) is False


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
    assert '--boot' in virt_calls[0]
    assert '--boot' not in virt_calls[1]


def test_fetch_image_uses_atomic_temp_then_move(
    monkeypatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vmx'
    cfg.paths.base_dir = str(tmp_path)
    cfg.image.cache_name = 'base.img'
    cfg.image.ubuntu_img_url = 'http://example.com/base.img'
    monkeypatch.setattr('aivm.vm.lifecycle._sudo_file_exists', lambda p: False)
    monkeypatch.setattr(
        'aivm.vm.lifecycle._ensure_qemu_access', lambda *a, **k: None
    )
    calls = []

    def fake_run_cmd(cmd, **kwargs):
        calls.append(cmd)
        return CmdResult(0, '', '')

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    out = fetch_image(cfg, dry_run=False)
    assert out.name == 'base.img'
    curl_calls = [c for c in calls if c and c[0] == 'curl']
    assert len(curl_calls) == 1
    mv_calls = [c for c in calls if c and c[0] == 'mv']
    assert len(mv_calls) == 1
    tmp_target = str(out) + '.part'
    assert tmp_target in curl_calls[0]
    assert tmp_target in mv_calls[0]
    assert str(out) in mv_calls[0]


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
