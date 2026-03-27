"""Tests for runtime environment detection surfaced in status output."""

from __future__ import annotations

from unittest.mock import patch

from pytest import MonkeyPatch
from pathlib import Path
from aivm.config import (
    AgentVMConfig,
    FirewallConfig,
    NetworkConfig,
    PathsConfig,
    VMConfig,
)
from aivm.status import (
    ProbeOutcome,
    probe_cwd_shared_with_vm,
    probe_runtime_environment,
    render_status,
    render_global_status,
)
from aivm.store import Store, upsert_attachment
from aivm.util import CmdResult
from aivm.vm.drift import saved_vm_drift_report


def test_probe_runtime_environment_from_systemd_detect_virt_guest(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: '/usr/bin/' + cmd)
    monkeypatch.setattr(
        'aivm.status.CommandManager.run',
        lambda self, *a, **k: CmdResult(0, 'kvm\n', ''),
    )
    out = probe_runtime_environment()
    assert out.ok is True
    assert 'virtualized guest' in out.detail
    assert 'kvm' in out.detail


def test_probe_runtime_environment_from_systemd_detect_virt_host(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: '/usr/bin/' + cmd)
    monkeypatch.setattr(
        'aivm.status.CommandManager.run',
        lambda self, *a, **k: CmdResult(0, 'none\n', ''),
    )
    out = probe_runtime_environment()
    assert out.ok is True
    assert 'host system' in out.detail


def test_probe_runtime_environment_cpuinfo_hypervisor_fallback(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: None)
    monkeypatch.setattr(
        'aivm.status.Path.exists',
        lambda self: str(self) == '/proc/cpuinfo',
    )
    monkeypatch.setattr(
        'aivm.status.Path.read_text',
        lambda self, **kw: (
            'flags\t: fpu vme de pse tsc msr pae mce cx8 apic sep '
            'mtrr pge mca cmov pat pse36 clflush mmx fxsr sse sse2 '
            'ht syscall nx rdtscp lm constant_tsc rep_good nopl '
            'xtopology nonstop_tsc cpuid aperfmperf pni pclmulqdq '
            'vmx ssse3 fma cx16 hypervisor'
        ),
    )
    out = probe_runtime_environment()
    assert out.ok is True
    assert 'virtualized guest' in out.detail
    assert 'cpu hypervisor flag' in out.detail


def test_probe_runtime_environment_unknown_when_no_signals(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: None)
    monkeypatch.setattr('aivm.status.Path.exists', lambda self: False)
    out = probe_runtime_environment()
    assert out.ok is None
    assert 'unable to determine' in out.detail


def test_render_global_status_includes_runtime_environment(
    monkeypatch: MonkeyPatch,
) -> None:
    monkeypatch.setattr('aivm.status.check_commands', lambda: ([], []))
    monkeypatch.setattr(
        'aivm.status.probe_runtime_environment',
        lambda: ProbeOutcome(True, 'virtualized guest (kvm)', ''),
    )
    monkeypatch.setattr('aivm.status.store_path', lambda: 'dummy.toml')
    monkeypatch.setattr('aivm.status.load_store', lambda _: Store())
    text = render_global_status()
    assert 'Runtime environment' in text
    assert 'virtualized guest (kvm)' in text


def test_probe_cwd_shared_with_vm_exact_match(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'test-vm'
    cfg_path = tmp_path / 'config.toml'
    project = tmp_path / 'project'
    project.mkdir()

    reg = Store()
    upsert_attachment(
        reg,
        host_path=project,
        vm_name='test-vm',
        mode='shared',
        access='rw',
        guest_dst='/guest/project',
        tag='project',
    )

    monkeypatch.setattr('aivm.status.load_store', lambda _: reg)
    monkeypatch.setattr('aivm.status.Path.cwd', lambda: project)

    out = probe_cwd_shared_with_vm(cfg, cfg_path)
    assert out.ok is True
    assert 'current directory is shared' in out.detail
    assert str(project) in out.detail


def test_probe_cwd_shared_with_vm_parent_match(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'test-vm'
    cfg_path = tmp_path / 'config.toml'
    project = tmp_path / 'project'
    nested = project / 'src' / 'pkg'
    nested.mkdir(parents=True)

    reg = Store()
    upsert_attachment(
        reg,
        host_path=project,
        vm_name='test-vm',
        mode='shared',
        access='rw',
        guest_dst='/guest/project',
        tag='project',
    )

    monkeypatch.setattr('aivm.status.load_store', lambda _: reg)
    monkeypatch.setattr('aivm.status.Path.cwd', lambda: nested)

    out = probe_cwd_shared_with_vm(cfg, cfg_path)
    assert out.ok is True
    assert 'parent share covers current directory' in out.detail
    assert str(project) in out.detail
    assert 'src/pkg' in out.detail


def test_render_status_reports_unshared_cwd(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'test-vm'
    cfg.paths.base_dir = str(tmp_path / 'base')
    cfg.paths.state_dir = str(tmp_path / 'state')
    cfg.image.cache_name = 'base.img'
    cfg_path = tmp_path / 'config.toml'
    cwd = tmp_path / 'unshared'
    cwd.mkdir()

    reg = Store()
    monkeypatch.setattr('aivm.status.load_store', lambda _: reg)
    monkeypatch.setattr('aivm.status.Path.cwd', lambda: cwd)
    monkeypatch.setattr('aivm.status.check_commands', lambda: ([], []))
    monkeypatch.setattr(
        'aivm.status.probe_runtime_environment',
        lambda: ProbeOutcome(True, 'ok', ''),
    )
    monkeypatch.setattr(
        'aivm.status.probe_network',
        lambda cfg, use_sudo=False: ProbeOutcome(True, 'ok', ''),
    )
    monkeypatch.setattr(
        'aivm.status.probe_firewall',
        lambda cfg, use_sudo=False: ProbeOutcome(True, 'ok', ''),
    )
    monkeypatch.setattr(
        'aivm.status.probe_vm_state',
        lambda cfg, use_sudo=False: (ProbeOutcome(False, 'not defined', ''), False),
    )
    monkeypatch.setattr('aivm.status.get_ip_cached', lambda cfg: '')

    text = render_status(cfg, cfg_path, detail=False, use_sudo=False)
    assert 'Current directory shared' in text
    assert 'is not covered by a saved VM share' in text


def test_saved_vm_drift_report_no_nameerror(
    monkeypatch: MonkeyPatch,
) -> None:
    """Regression test for runtime status path.

    This test ensures that saved_vm_drift_report can be called without
    raising a NameError for vm_share_mappings. The original bug was that
    drift.py called vm_share_mappings() without importing it.
    """
    # Create a minimal VM config
    cfg = AgentVMConfig()
    cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
    cfg.network = NetworkConfig(name='aivm-net')
    cfg.firewall = FirewallConfig()
    cfg.paths = PathsConfig(
        base_dir='/var/lib/aivm',
        state_dir='/var/lib/aivm',
        ssh_identity_file='/home/user/.ssh/id_rsa',
    )

    # Create a minimal store with no VMs
    reg = Store()

    # Mock the hardware read to return valid values
    with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_hw:
        mock_hw.return_value = (4, 8192, '', '')

        # Mock vm_share_mappings to return empty mappings
        with patch('aivm.vm.drift.vm_share_mappings') as mock_mappings:
            mock_mappings.return_value = []

            # This should NOT raise NameError: name 'vm_share_mappings' is not defined
            report = saved_vm_drift_report(cfg, reg, use_sudo=False)

            # Verify the report is available and has no drift
            assert report.available is True
            assert report.ok is True


def test_saved_vm_drift_report_with_mappings(
    monkeypatch: MonkeyPatch,
) -> None:
    """Test saved_vm_drift_report with actual share mappings.

    This test verifies that the drift report correctly handles share mappings
    and that vm_share_mappings is properly imported and callable.
    """
    cfg = AgentVMConfig()
    cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
    cfg.network = NetworkConfig(name='aivm-net')
    cfg.firewall = FirewallConfig()
    cfg.paths = PathsConfig(
        base_dir='/var/lib/aivm',
        state_dir='/var/lib/aivm',
        ssh_identity_file='/home/user/.ssh/id_rsa',
    )

    # Create a store with an attachment
    reg = Store()
    upsert_attachment(
        reg,
        host_path='/home/user/project',
        vm_name='test-vm',
        mode='shared',
        access='rw',
        guest_dst='/guest/path',
        tag='my-tag',
    )

    with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_hw:
        mock_hw.return_value = (4, 8192, '', '')

        with patch('aivm.vm.drift.vm_share_mappings') as mock_mappings:
            # VM has the same share mapping as in the store
            mock_mappings.return_value = [('/home/user/project', 'my-tag')]

            report = saved_vm_drift_report(cfg, reg, use_sudo=False)

            assert report.available is True
            assert report.ok is True


def test_saved_vm_drift_report_mapping_drift(
    monkeypatch: MonkeyPatch,
) -> None:
    """Test saved_vm_drift_report when VM has different mappings than expected.

    This test verifies that drift is correctly detected when the VM's actual
    share mappings differ from what's expected based on the store.
    """
    cfg = AgentVMConfig()
    cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
    cfg.network = NetworkConfig(name='aivm-net')
    cfg.firewall = FirewallConfig()
    cfg.paths = PathsConfig(
        base_dir='/var/lib/aivm',
        state_dir='/var/lib/aivm',
        ssh_identity_file='/home/user/.ssh/id_rsa',
    )

    # Create a store with an attachment
    reg = Store()
    upsert_attachment(
        reg,
        host_path='/home/user/project',
        vm_name='test-vm',
        mode='shared',
        access='rw',
        guest_dst='/guest/path',
        tag='my-tag',
    )

    with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_hw:
        mock_hw.return_value = (4, 8192, '', '')

        with patch('aivm.vm.drift.vm_share_mappings') as mock_mappings:
            # VM has a different share mapping than expected
            mock_mappings.return_value = [('/other/path', 'other-tag')]

            report = saved_vm_drift_report(cfg, reg, use_sudo=False)

            assert report.available is True
            assert report.ok is False
            assert len(report.items) == 2  # missing expected + unexpected extra


def test_vm_share_mappings_imported_in_drift_module(
    monkeypatch: MonkeyPatch,
) -> None:
    """Verify that vm_share_mappings is properly imported in drift module.

    This test specifically checks that the drift module has vm_share_mappings
    available as an imported function, which was the root cause of the
    original NameError.
    """
    # Import the drift module to check its namespace
    from aivm.vm import drift

    # vm_share_mappings should be available in the drift module namespace
    assert hasattr(drift, 'vm_share_mappings')
    assert callable(drift.vm_share_mappings)
