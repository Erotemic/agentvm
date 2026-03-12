"""Tests for vm update drift detection and command behavior."""

from __future__ import annotations

from pathlib import Path

from aivm.cli.vm import (
    ReconcileResult,
    ResolvedAttachment,
    VMUpdateCLI,
    VMUpdateDrift,
    _apply_vm_update,
    _parse_qemu_img_virtual_size,
    _parse_vm_disk_path_from_dumpxml,
    _parse_vm_network_from_dumpxml,
    _prepare_attached_session,
    _vm_update_drift,
)
from aivm.config import AgentVMConfig
from aivm.status import ProbeOutcome
from aivm.util import CmdResult


def test_parse_qemu_img_virtual_size() -> None:
    assert (
        _parse_qemu_img_virtual_size('{"virtual-size": 42949672960}')
        == 42949672960
    )
    assert _parse_qemu_img_virtual_size('{"virtual-size": 0}') is None
    assert _parse_qemu_img_virtual_size('not-json') is None


def test_parse_vm_disk_and_network_from_dumpxml() -> None:
    xml = """
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/images/vm.qcow2'/>
      <target dev='vda' bus='virtio'/>
    </disk>
    <interface type='network'>
      <source network='aivm-net'/>
    </interface>
  </devices>
</domain>
"""
    assert (
        _parse_vm_disk_path_from_dumpxml(xml)
        == '/var/lib/libvirt/images/vm.qcow2'
    )
    assert _parse_vm_network_from_dumpxml(xml) == 'aivm-net'


def test_apply_vm_update_rejects_disk_shrink() -> None:
    cfg = AgentVMConfig()
    drift = VMUpdateDrift(
        disk_bytes=(40 * 1024**3, 20 * 1024**3),
        disk_path='/tmp/vm.qcow2',
    )
    try:
        _apply_vm_update(cfg, drift, dry_run=False)
    except RuntimeError as ex:
        assert 'Disk shrink is not supported safely' in str(ex)
    else:
        raise AssertionError('Expected RuntimeError on disk shrink')


def test_vm_update_no_changes(monkeypatch, capsys, tmp_path: Path) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-noop'
    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, tmp_path / 'config.toml'),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._vm_update_drift',
        lambda *a, **k: (VMUpdateDrift(), False),
    )
    rc = VMUpdateCLI.main(argv=False, config=str(tmp_path / 'config.toml'))
    assert rc == 0
    out = capsys.readouterr().out
    assert 'already in sync' in out


def test_vm_update_restarts_when_required(monkeypatch, tmp_path: Path) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-update'
    drift = VMUpdateDrift(cpus=(2, 4))
    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path',
        lambda *a, **k: (cfg, tmp_path / 'config.toml'),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._vm_update_drift',
        lambda *a, **k: (drift, True),
    )
    monkeypatch.setattr('aivm.cli.vm._confirm_sudo_block', lambda **k: None)
    monkeypatch.setattr(
        'aivm.cli.vm._apply_vm_update',
        lambda *a, **k: (True, True),
    )
    called: dict[str, object] = {}

    def fake_restart(*a, **k):
        called['kwargs'] = k

    monkeypatch.setattr(
        'aivm.cli.vm._maybe_restart_vm_after_update', fake_restart
    )
    rc = VMUpdateCLI.main(
        argv=False,
        config=str(tmp_path / 'config.toml'),
        yes=True,
        restart='always',
    )
    assert rc == 0
    assert called['kwargs']['restart_policy'] == 'always'


def test_vm_update_drift_escalates_for_disk_probe(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-drift'
    cfg.vm.disk_gb = 60

    sudo_prompts: list[str] = []

    def fake_confirm_sudo_block(*, yes, purpose, **kwargs):
        del yes, kwargs
        sudo_prompts.append(purpose)

    def fake_run_cmd(cmd, *, sudo=False, **kwargs):
        del kwargs
        if cmd[:3] == ['virsh', '-c', 'qemu:///system'] and cmd[3] == 'dominfo':
            return CmdResult(
                0,
                'CPU(s):         4\nMax memory:     8388608 KiB\n',
                '',
            )
        if (
            cmd[:3] == ['virsh', '-c', 'qemu:///system']
            and cmd[3] == 'domstate'
        ):
            return CmdResult(0, 'running\n', '')
        if (
            cmd[:3] == ['virsh', '-c', 'qemu:///system']
            and cmd[3] == 'dumpxml'
            and not sudo
        ):
            return CmdResult(1, '', 'permission denied')
        if (
            cmd[:3] == ['virsh', '-c', 'qemu:///system']
            and cmd[3] == 'dumpxml'
            and sudo
        ):
            xml = """
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/aivm/vm-drift/images/vm-drift.qcow2'/>
    </disk>
    <interface type='network'>
      <source network='aivm-net'/>
    </interface>
  </devices>
</domain>
""".strip()
            return CmdResult(
                0,
                xml,
                '',
            )
        if cmd[:3] == ['qemu-img', 'info', '--output=json'] and not sudo:
            return CmdResult(1, '', 'permission denied')
        if cmd[:3] == ['qemu-img', 'info', '--output=json'] and sudo:
            return CmdResult(0, '{"virtual-size": 42949672960}', '')
        raise AssertionError(f'Unexpected cmd={cmd!r} sudo={sudo}')

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', fake_confirm_sudo_block
    )
    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)
    drift, running = _vm_update_drift(cfg, yes=False)
    assert running is True
    assert drift.disk_bytes == (40 * 1024**3, 60 * 1024**3)
    assert len(sudo_prompts) == 1


def test_vm_update_drift_falls_back_to_domblkinfo_on_lock(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'vm-lock'
    cfg.vm.disk_gb = 60

    def fake_confirm_sudo_block(*, yes, purpose, **kwargs):
        del yes, purpose, kwargs

    def fake_run_cmd(cmd, *, sudo=False, **kwargs):
        del kwargs, sudo
        if cmd[:3] == ['virsh', '-c', 'qemu:///system'] and cmd[3] == 'dominfo':
            return CmdResult(
                0,
                'CPU(s):         4\nMax memory:     8388608 KiB\n',
                '',
            )
        if (
            cmd[:3] == ['virsh', '-c', 'qemu:///system']
            and cmd[3] == 'domstate'
        ):
            return CmdResult(0, 'running\n', '')
        if cmd[:3] == ['virsh', '-c', 'qemu:///system'] and cmd[3] == 'dumpxml':
            xml = """
<domain>
  <devices>
    <disk type='file' device='disk'>
      <source file='/var/lib/libvirt/aivm/vm-lock/images/vm-lock.qcow2'/>
    </disk>
    <interface type='network'>
      <source network='aivm-net'/>
    </interface>
  </devices>
</domain>
""".strip()
            return CmdResult(0, xml, '')
        if cmd[:3] == ['qemu-img', 'info', '--output=json']:
            return CmdResult(
                1,
                '',
                'Failed to get shared "write" lock\nIs another process using the image?',
            )
        if (
            cmd[:3] == ['virsh', '-c', 'qemu:///system']
            and cmd[3] == 'domblkinfo'
        ):
            return CmdResult(0, 'Capacity: 42949672960\nAllocation: 0\n', '')
        raise AssertionError(f'Unexpected command: {cmd!r}')

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', fake_confirm_sudo_block
    )
    monkeypatch.setattr('aivm.cli.vm.run_cmd', fake_run_cmd)
    drift, _running = _vm_update_drift(cfg, yes=True)
    assert drift.disk_bytes == (40 * 1024**3, 60 * 1024**3)
    assert any('falling back to virsh domblkinfo' in n for n in drift.notes)


def test_prepare_attached_session_bootstraps_missing_vm(
    monkeypatch, tmp_path: Path
) -> None:
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    cfg = AgentVMConfig()
    cfg.vm.name = 'bootstrap-vm'
    cfg_path = tmp_path / 'config.toml'

    calls: list[str] = []
    state = {'ready': False}

    def fake_resolve_cfg_for_code(**kwargs):
        del kwargs
        if not state['ready']:
            raise RuntimeError(
                f'No VM definitions found in config store: {cfg_path}. '
                'Run `aivm config init` then `aivm vm create` first.'
            )
        return cfg, cfg_path

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code', fake_resolve_cfg_for_code
    )
    monkeypatch.setattr(
        'aivm.cli.config.InitCLI.main',
        lambda *a, **k: calls.append('config_init') or 0,
    )

    def fake_vm_create(*a, **k):
        calls.append('vm_create')
        state['ready'] = True
        return 0

    monkeypatch.setattr('aivm.cli.vm.VMCreateCLI.main', fake_vm_create)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: ResolvedAttachment(
            vm_name=cfg.vm.name,
            source_dir=str(host_src),
            guest_dst=str(host_src),
            tag='hostcode-proj',
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_attached_vm',
        lambda *a, **k: ReconcileResult(
            attachment=ResolvedAttachment(
                vm_name=cfg.vm.name,
                source_dir=str(host_src),
                guest_dst=str(host_src),
                tag='hostcode-proj',
            ),
            cached_ip=None,
            cached_ssh_ok=False,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: tmp_path / 'dummy'
    )
    monkeypatch.setattr('aivm.cli.vm.get_ip_cached', lambda *a, **k: '10.0.0.2')
    monkeypatch.setattr(
        'aivm.cli.vm.probe_ssh_ready',
        lambda *a, **k: ProbeOutcome(True, 'ready', ''),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None
    )

    session = _prepare_attached_session(
        config_opt=None,
        vm_opt='',
        host_src=host_src,
        guest_dst_opt='',
        recreate_if_needed=False,
        ensure_firewall_opt=True,
        force=False,
        dry_run=False,
        yes=True,
    )
    assert session.cfg.vm.name == 'bootstrap-vm'
    assert calls == ['config_init', 'vm_create']


def test_prepare_attached_session_bootstraps_create_only_when_defaults_exist(
    monkeypatch, tmp_path: Path
) -> None:
    host_src = tmp_path / 'proj'
    host_src.mkdir()
    cfg = AgentVMConfig()
    cfg.vm.name = 'bootstrap-vm'
    cfg_path = tmp_path / 'config.toml'

    from aivm.store import Store, save_store

    store = Store()
    store.defaults = AgentVMConfig()
    save_store(store, cfg_path)

    calls: list[str] = []
    state = {'ready': False}

    def fake_resolve_cfg_for_code(**kwargs):
        del kwargs
        if not state['ready']:
            raise RuntimeError(
                f'No VM definitions found in config store: {cfg_path}. '
                'Run `aivm config init` then `aivm vm create` first.'
            )
        return cfg, cfg_path

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code', fake_resolve_cfg_for_code
    )
    monkeypatch.setattr(
        'aivm.cli.config.InitCLI.main',
        lambda *a, **k: calls.append('config_init') or 0,
    )

    def fake_vm_create(*a, **k):
        calls.append('vm_create')
        state['ready'] = True
        return 0

    monkeypatch.setattr('aivm.cli.vm.VMCreateCLI.main', fake_vm_create)
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *a, **k: ResolvedAttachment(
            vm_name=cfg.vm.name,
            source_dir=str(host_src),
            guest_dst=str(host_src),
            tag='hostcode-proj',
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_attached_vm',
        lambda *a, **k: ReconcileResult(
            attachment=ResolvedAttachment(
                vm_name=cfg.vm.name,
                source_dir=str(host_src),
                guest_dst=str(host_src),
                tag='hostcode-proj',
            ),
            cached_ip=None,
            cached_ssh_ok=False,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_attachment', lambda *a, **k: tmp_path / 'dummy'
    )
    monkeypatch.setattr('aivm.cli.vm.get_ip_cached', lambda *a, **k: '10.0.0.2')
    monkeypatch.setattr(
        'aivm.cli.vm.probe_ssh_ready',
        lambda *a, **k: ProbeOutcome(True, 'ready', ''),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted', lambda *a, **k: None
    )

    session = _prepare_attached_session(
        config_opt=str(cfg_path),
        vm_opt='',
        host_src=host_src,
        guest_dst_opt='',
        recreate_if_needed=False,
        ensure_firewall_opt=True,
        force=False,
        dry_run=False,
        yes=True,
    )
    assert session.cfg.vm.name == 'bootstrap-vm'
    assert calls == ['vm_create']


def test_prepare_attached_session_restores_saved_vm_attachments(
    monkeypatch, tmp_path: Path
) -> None:
    from aivm.store import Store, save_store, upsert_attachment, upsert_vm

    host_src = tmp_path / 'proj'
    other_src = tmp_path / 'docs'
    host_src.mkdir()
    other_src.mkdir()
    cfg = AgentVMConfig()
    cfg.vm.name = 'restore-vm'
    cfg_path = tmp_path / 'config.toml'

    store = Store()
    upsert_vm(store, cfg)
    upsert_attachment(
        store,
        host_path=host_src,
        vm_name=cfg.vm.name,
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )
    upsert_attachment(
        store,
        host_path=other_src,
        vm_name=cfg.vm.name,
        guest_dst='/workspace/docs',
        tag='hostcode-docs',
    )
    save_store(store, cfg_path)

    current_attachment = ResolvedAttachment(
        vm_name=cfg.vm.name,
        source_dir=str(host_src.resolve()),
        guest_dst='/workspace/proj',
        tag='hostcode-proj',
    )

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_cfg_for_code',
        lambda **kwargs: (cfg, cfg_path),
    )

    def fake_resolve_attachment(_cfg, _cfg_path, host_path, _guest_dst_opt):
        host_path = Path(host_path).resolve()
        if host_path == host_src.resolve():
            return current_attachment
        if host_path == other_src.resolve():
            return ResolvedAttachment(
                vm_name=cfg.vm.name,
                source_dir=str(other_src.resolve()),
                guest_dst='/workspace/docs',
                tag='hostcode-docs',
            )
        raise AssertionError(f'unexpected host_path={host_path}')

    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        fake_resolve_attachment,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._reconcile_attached_vm',
        lambda *a, **k: ReconcileResult(
            attachment=current_attachment,
            cached_ip='10.0.0.2',
            cached_ssh_ok=True,
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None
    )
    monkeypatch.setattr(
        'aivm.cli.vm.probe_ssh_ready',
        lambda *a, **k: ProbeOutcome(True, 'ready', ''),
    )

    mappings = [(str(host_src.resolve()), 'hostcode-proj')]

    def fake_vm_share_mappings(*a, **k):
        del a, k
        return list(mappings)

    monkeypatch.setattr('aivm.cli.vm.vm_share_mappings', fake_vm_share_mappings)

    attached: list[tuple[tuple, dict]] = []

    def fake_attach_vm_share(*a, **k):
        attached.append((a, k))
        mappings.append((str(other_src.resolve()), 'hostcode-docs'))

    monkeypatch.setattr('aivm.cli.vm.attach_vm_share', fake_attach_vm_share)

    mounted: list[tuple[tuple, dict]] = []
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_share_mounted',
        lambda *a, **k: mounted.append((a, k)),
    )

    session = _prepare_attached_session(
        config_opt=str(cfg_path),
        vm_opt='',
        host_src=host_src,
        guest_dst_opt='',
        recreate_if_needed=False,
        ensure_firewall_opt=True,
        force=False,
        dry_run=False,
        yes=True,
    )

    assert session.cfg.vm.name == 'restore-vm'
    assert len(attached) == 1
    attach_args, attach_kwargs = attached[0]
    assert attach_args[1] == str(other_src.resolve())
    assert attach_args[2] == 'hostcode-docs'
    assert attach_kwargs['dry_run'] is False
    assert [kwargs['guest_dst'] for _, kwargs in mounted] == [
        '/workspace/proj',
        '/workspace/docs',
    ]
