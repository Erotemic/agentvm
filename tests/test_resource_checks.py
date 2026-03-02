"""Tests for shared VM resource check helpers."""

from __future__ import annotations

from aivm.config import AgentVMConfig
from aivm.resource_checks import (
    vm_resource_impossible_lines,
    vm_resource_warning_lines,
)


def test_impossible_uses_memtotal_not_memavailable(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.ram_mb = 1024
    cfg.vm.cpus = 2
    monkeypatch.setattr('aivm.resource_checks.host_mem_total_mb', lambda: 8192)
    monkeypatch.setattr(
        'aivm.resource_checks.host_mem_available_mb', lambda: 570
    )
    monkeypatch.setattr('aivm.resource_checks.host_cpu_count', lambda: 8)
    problems = vm_resource_impossible_lines(cfg)
    assert problems == []


def test_impossible_flags_memtotal_and_cpu(monkeypatch) -> None:
    cfg = AgentVMConfig()
    cfg.vm.ram_mb = 16384
    cfg.vm.cpus = 16
    monkeypatch.setattr('aivm.resource_checks.host_mem_total_mb', lambda: 4096)
    monkeypatch.setattr('aivm.resource_checks.host_cpu_count', lambda: 4)
    problems = vm_resource_impossible_lines(cfg)
    text = '\n'.join(problems)
    assert 'MemTotal' in text
    assert 'host CPU count' in text


def test_warning_uses_memtotal_and_ignores_low_memavailable(
    monkeypatch, tmp_path
) -> None:
    cfg = AgentVMConfig()
    cfg.vm.ram_mb = 1024
    cfg.paths.base_dir = str(tmp_path)
    monkeypatch.setattr('aivm.resource_checks.host_mem_total_mb', lambda: 8192)
    monkeypatch.setattr(
        'aivm.resource_checks.host_mem_available_mb', lambda: 488
    )
    monkeypatch.setattr('aivm.resource_checks.host_cpu_count', lambda: 8)
    monkeypatch.setattr(
        'aivm.resource_checks.host_free_disk_gb', lambda p: 200.0
    )
    warnings = vm_resource_warning_lines(cfg)
    assert not any('RAM' in w for w in warnings)
