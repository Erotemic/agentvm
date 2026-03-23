"""Unit tests for the shared VM drift detection module.

This module tests the drift detection logic in `aivm/vm/drift.py`, ensuring
that expected vs actual state comparison works correctly for hardware
(CPU, RAM) and share mappings.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from aivm.config import (
    AgentVMConfig,
    PathsConfig,
    VMConfig,
)
from aivm.vm.drift import (
    DriftItem,
    DriftReport,
    attachment_drift_report,
    attachment_has_mapping,
    expected_mapping_for_attachment,
    hardware_drift_report,
    parse_dominfo_hardware,
    vm_config_drift_report,
)
from aivm.vm.share import AttachmentMode, ResolvedAttachment


class TestDriftItem:
    """Tests for DriftItem dataclass."""

    def test_drift_item_basic(self):
        """Test basic DriftItem creation."""
        item = DriftItem(key='cpus', expected=4, actual=2)
        assert item.key == 'cpus'
        assert item.expected == 4
        assert item.actual == 2
        assert item.reason == ''

    def test_drift_item_with_reason(self):
        """Test DriftItem with custom reason."""
        item = DriftItem(
            key='ram_mb',
            expected=8192,
            actual=4096,
            reason='Config specifies different RAM than current VM',
        )
        assert item.reason == 'Config specifies different RAM than current VM'


class TestDriftReport:
    """Tests for DriftReport dataclass."""

    def test_drift_report_no_drift(self):
        """Test DriftReport with no drift."""
        report = DriftReport(available=True, summary='no drift detected')
        assert report.available is True
        assert report.summary == 'no drift detected'
        assert report.items == ()
        assert report.ok is True

    def test_drift_report_with_drift(self):
        """Test DriftReport with drift items."""
        item = DriftItem(key='cpus', expected=4, actual=2)
        report = DriftReport(
            available=True, summary='1 drift item detected', items=(item,)
        )
        assert report.available is True
        assert report.ok is False

    def test_drift_report_unavailable(self):
        """Test DriftReport when libvirt query unavailable."""
        report = DriftReport(
            available=False,
            summary='VM not defined',
            diag='virsh dominfo failed',
        )
        assert report.available is False
        assert report.ok is None


class TestParseDominfoHardware:
    """Tests for parse_dominfo_hardware helper."""

    def test_parse_dominfo_cpu_and_memory_mib(self):
        """Test parsing CPU and memory from dominfo output with MiB unit."""
        dominfo = """
Domain: test-vm
    ID: 1
    UUID: 12345678-1234-1234-1234-123456789abc
    OS Type: hvm
    State: running
    CPU(s): 4
    Max memory: 8192 MiB
    """
        cpus, mem = parse_dominfo_hardware(dominfo)
        assert cpus == 4
        assert mem == 8192  # Memory is returned in MiB

    def test_parse_dominfo_no_cpu(self):
        """Test parsing when CPU line is missing."""
        dominfo = """
Domain: test-vm
    State: running
    Max memory: 8192 MiB
    """
        cpus, mem = parse_dominfo_hardware(dominfo)
        assert cpus is None
        assert mem == 8192  # Memory is returned in MiB

    def test_parse_dominfo_no_memory(self):
        """Test parsing when memory line is missing."""
        dominfo = """
Domain: test-vm
    State: running
    CPU(s): 4
    """
        cpus, mem = parse_dominfo_hardware(dominfo)
        assert cpus == 4
        assert mem is None

    def test_parse_dominfo_kib_unit(self):
        """Test parsing when memory is in KiB (typical virsh dominfo format)."""
        dominfo = """
Domain: test-vm
    State: running
    CPU(s): 4
    Max memory: 8388608 KiB
    """
        cpus, mem = parse_dominfo_hardware(dominfo)
        assert cpus == 4
        assert mem == 8192  # 8388608 KiB = 8192 MiB

    def test_parse_dominfo_gib_unit(self):
        """Test parsing when memory is in GiB."""
        dominfo = """
Domain: test-vm
    State: running
    CPU(s): 4
    Max memory: 8 GiB
    """
        cpus, mem = parse_dominfo_hardware(dominfo)
        assert cpus == 4
        assert mem == 8192  # 8 GiB = 8192 MiB

    def test_parse_dominfo_empty(self):
        """Test parsing empty input."""
        cpus, mem = parse_dominfo_hardware('')
        assert cpus is None
        assert mem is None


class TestExpectedMappingForAttachment:
    """Tests for expected_mapping_for_attachment helper."""

    def test_shared_mode_mapping(self):
        """Test expected mapping for shared mode attachment."""
        cfg = MagicMock(spec=AgentVMConfig)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode=AttachmentMode.SHARED,
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )
        result = expected_mapping_for_attachment(cfg, att)
        assert result == ('/home/user/project', 'my-tag')

    def test_shared_root_mode_mapping(self):
        """Test expected mapping for shared-root mode attachment."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.paths = PathsConfig(base_dir='/var/lib/aivm')
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode=AttachmentMode.SHARED_ROOT,
            source_dir='/home/user/project',
            tag='shared-root',
            guest_dst='/guest/path',
        )
        result = expected_mapping_for_attachment(cfg, att)
        assert result is not None
        assert result[1] == 'aivm-shared-root'

    def test_git_mode_no_mapping(self):
        """Test that git mode returns no mapping."""
        cfg = MagicMock(spec=AgentVMConfig)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode='git',
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )
        result = expected_mapping_for_attachment(cfg, att)
        assert result is None


class TestAttachmentHasMapping:
    """Tests for attachment_has_mapping helper."""

    def test_mapping_exists_shared_mode(self):
        """Test when mapping exists for shared mode."""
        cfg = MagicMock(spec=AgentVMConfig)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode='shared',
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )
        mappings = [
            ('/home/user/project', 'my-tag'),
            ('/other/path', 'other-tag'),
        ]
        assert attachment_has_mapping(cfg, att, mappings) is True

    def test_mapping_missing_shared_mode(self):
        """Test when mapping does not exist for shared mode."""
        cfg = MagicMock(spec=AgentVMConfig)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode='shared',
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )
        mappings = [('/other/path', 'other-tag')]
        assert attachment_has_mapping(cfg, att, mappings) is False

    def test_mapping_exists_shared_root_mode(self):
        """Test when mapping exists for shared-root mode."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.paths = PathsConfig(base_dir='/var/lib/aivm')
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode='shared-root',
            source_dir='/home/user/project',
            tag='shared-root',
            guest_dst='/guest/path',
        )
        # Shared-root uses canonical path, not the attachment source_dir
        from aivm.vm.drift import (
            SHARED_ROOT_VIRTIOFS_TAG,
            _shared_root_host_dir,
        )

        expected_src = str(_shared_root_host_dir(cfg))
        mappings = [(expected_src, SHARED_ROOT_VIRTIOFS_TAG)]
        assert attachment_has_mapping(cfg, att, mappings) is True

    def test_mapping_missing_shared_root_mode(self):
        """Test when mapping does not exist for shared-root mode."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.paths = PathsConfig(base_dir='/var/lib/aivm')
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode='shared-root',
            source_dir='/home/user/project',
            tag='shared-root',
            guest_dst='/guest/path',
        )
        # Wrong tag for shared-root
        from aivm.vm.drift import _shared_root_host_dir

        expected_src = str(_shared_root_host_dir(cfg))
        mappings = [(expected_src, 'wrong-tag')]
        assert attachment_has_mapping(cfg, att, mappings) is False


class TestHardwareDriftReport:
    """Tests for hardware_drift_report function."""

    def test_no_hardware_drift(self):
        """Test when hardware matches config."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_read:
            mock_read.return_value = (
                4,
                8192,
                '',
                '',
            )  # cpus=4, mem=8192 MiB, no error
            report = hardware_drift_report(cfg, use_sudo=False)
            assert report.available is True
            assert report.ok is True
            assert len(report.items) == 0

    def test_memory_parse_none_no_crash(self):
        """Test that None memory value doesn't crash (reported as unavailable, not 'in sync')."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_read:
            # If memory parsing fails (returns None), report as unavailable
            mock_read.return_value = (
                4,
                None,
                '',
                '',
            )  # cpus=4, mem parsing failed
            report = hardware_drift_report(cfg, use_sudo=False)
            # Parse failures should be reported as unavailable, not "in sync"
            assert report.available is False
            assert report.ok is None
            assert 'could not be parsed' in report.summary

    def test_cpu_drift(self):
        """Test when CPU count differs from config."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_read:
            mock_read.return_value = (2, 8192, '', '')  # cpus=2, mem=8192 MiB
            report = hardware_drift_report(cfg, use_sudo=False)
            assert report.available is True
            assert report.ok is False
            assert len(report.items) == 1
            assert report.items[0].key == 'cpus'
            assert report.items[0].expected == 4
            assert report.items[0].actual == 2

    def test_memory_drift(self):
        """Test when RAM differs from config."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_read:
            mock_read.return_value = (4, 4096, '', '')  # cpus=4, mem=4096 MiB
            report = hardware_drift_report(cfg, use_sudo=False)
            assert report.available is True
            assert report.ok is False
            assert len(report.items) == 1
            assert report.items[0].key == 'ram_mb'
            assert report.items[0].expected == 8192
            assert report.items[0].actual == 4096

    def test_vm_not_defined(self):
        """Test when VM is not defined."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.read_actual_vm_hardware') as mock_read:
            mock_read.return_value = (
                None,
                None,
                'not_found',
                'virsh: domain not found',
            )
            report = hardware_drift_report(cfg, use_sudo=False)
            assert report.available is False
            assert report.ok is None
            assert 'not defined' in report.summary


class TestAttachmentDriftReport:
    """Tests for attachment_drift_report function."""

    def test_mapping_present(self):
        """Test when share mapping is present."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode=AttachmentMode.SHARED,
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )

        with patch('aivm.vm.drift.read_actual_vm_mappings') as mock_read:
            mock_read.return_value = (
                [('/home/user/project', 'my-tag'), ('/other', 'other')],
                '',
            )
            report = attachment_drift_report(
                cfg, att, host_src=Path('/home/user/project'), use_sudo=False
            )
            assert report.available is True
            assert report.ok is True

    def test_mapping_missing(self):
        """Test when share mapping is missing."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode=AttachmentMode.SHARED,
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )

        with patch('aivm.vm.drift.read_actual_vm_mappings') as mock_read:
            mock_read.return_value = [('/other/path', 'other-tag')], ''
            report = attachment_drift_report(
                cfg, att, host_src=Path('/home/user/project'), use_sudo=False
            )
            assert report.available is True
            assert report.ok is False
            assert len(report.items) == 1
            assert report.items[0].key == 'share_mapping'

    def test_vm_not_defined(self):
        """Test when VM is not defined."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)
        att = ResolvedAttachment(
            vm_name='test-vm',
            mode=AttachmentMode.SHARED,
            source_dir='/home/user/project',
            tag='my-tag',
            guest_dst='/guest/path',
        )

        with patch('aivm.vm.drift.read_actual_vm_mappings') as mock_read:
            mock_read.return_value = None, 'failed to read mappings'
            report = attachment_drift_report(
                cfg, att, host_src=Path('/home/user/project'), use_sudo=False
            )
            assert report.available is False
            assert report.ok is None


class TestVmConfigDriftReport:
    """Tests for vm_config_drift_report combined report function."""

    def test_no_drift(self):
        """Test when no drift exists."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.hardware_drift_report') as mock_hw:
            mock_hw.return_value = DriftReport(
                available=True, summary='no drift', items=()
            )
            report = vm_config_drift_report(cfg, use_sudo=False)
            assert report.available is True
            assert report.ok is True

    def test_combined_drift(self):
        """Test combined hardware and share drift."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        with patch('aivm.vm.drift.hardware_drift_report') as mock_hw:
            mock_hw.return_value = DriftReport(
                available=True,
                summary='1 hardware drift',
                items=(DriftItem(key='cpus', expected=4, actual=2),),
            )
            report = vm_config_drift_report(cfg, use_sudo=False)
            assert report.available is True
            assert report.ok is False
            assert len(report.items) == 1

    def test_with_expected_mappings(self):
        """Test with explicit expected mappings."""
        cfg = MagicMock(spec=AgentVMConfig)
        cfg.vm = VMConfig(name='test-vm', cpus=4, ram_mb=8192, disk_gb=50)

        # Patch hardware_drift_report to avoid hitting real virsh
        with patch('aivm.vm.drift.hardware_drift_report') as mock_hw:
            mock_hw.return_value = DriftReport(
                available=True, summary='No hardware drift', items=()
            )
            with patch('aivm.vm.drift.read_actual_vm_mappings') as mock_read:
                # VM has /other mapped, but we expect /home/user/project
                # This creates 2 drift items: missing expected + unexpected extra
                mock_read.return_value = [('/other', 'other')], ''
                report = vm_config_drift_report(
                    cfg,
                    use_sudo=False,
                    expected_mappings=[('/home/user/project', 'my-tag')],
                )
                assert report.available is True
                assert report.ok is False
                # Two-way diff detects both missing and unexpected mappings
                assert len(report.items) == 2
                # One should be the missing expected mapping
                assert any(
                    'share_mapping:my-tag' in item.key for item in report.items
                )
                # One should be the unexpected extra mapping
                assert any(
                    'share_mapping:extra:other' in item.key
                    for item in report.items
                )
