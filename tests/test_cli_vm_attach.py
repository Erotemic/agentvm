"""Tests for vm attach live-mount behavior."""

from __future__ import annotations

from pathlib import Path

from aivm.cli.vm import ResolvedAttachment, VMAttachCLI
from aivm.config import AgentVMConfig


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
        'aivm.cli.vm._check_vm_state',
        lambda *a, **k: (True, True, 'vm-running state=running'),
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
        'aivm.cli.vm._check_vm_state',
        lambda *a, **k: (False, True, 'vm-stopped state=shut off'),
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
        (None, False, 'probe inconclusive without sudo'),
        (True, True, 'vm-needs-sudo state=running'),
    ]
    monkeypatch.setattr(
        'aivm.cli.vm._check_vm_state',
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
