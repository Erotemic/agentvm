"""Shared VM resource sanity checks used by config-init and vm-create flows."""

from __future__ import annotations

import os
from pathlib import Path

from .config import AgentVMConfig


def host_mem_available_mb() -> int | None:
    try:
        text = Path('/proc/meminfo').read_text(
            encoding='utf-8', errors='ignore'
        )
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith('MemAvailable:'):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
    return None


def host_mem_total_mb() -> int | None:
    try:
        text = Path('/proc/meminfo').read_text(
            encoding='utf-8', errors='ignore'
        )
    except Exception:
        return None
    for line in text.splitlines():
        if line.startswith('MemTotal:'):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                return int(parts[1]) // 1024
    return None


def host_cpu_count() -> int | None:
    try:
        count = os.cpu_count()
    except Exception:
        return None
    return int(count) if count else None


def host_free_disk_gb(path: Path) -> float | None:
    try:
        stat = os.statvfs(str(path))
    except Exception:
        return None
    free_bytes = int(stat.f_bavail) * int(stat.f_frsize)
    return free_bytes / (1024**3)


def vm_resource_warning_lines(cfg: AgentVMConfig) -> list[str]:
    warnings: list[str] = []
    mem_total_mb = host_mem_total_mb()
    if mem_total_mb is not None and cfg.vm.ram_mb > int(mem_total_mb * 0.8):
        warnings.append(
            'Requested VM RAM is large relative to host total memory: '
            f'requested={cfg.vm.ram_mb} MiB, MemTotal={mem_total_mb} MiB. '
            'If VM creation fails, lower vm.ram_mb.'
        )
    elif mem_total_mb is None:
        mem_avail_mb = host_mem_available_mb()
        if mem_avail_mb is not None and cfg.vm.ram_mb > int(mem_avail_mb * 0.8):
            warnings.append(
                'Requested VM RAM may be high for currently available memory: '
                f'requested={cfg.vm.ram_mb} MiB, MemAvailable={mem_avail_mb} MiB. '
                'If VM creation fails, lower vm.ram_mb.'
            )

    cpu_count = host_cpu_count()
    if cpu_count is not None and cfg.vm.cpus > cpu_count:
        warnings.append(
            'Requested VM CPUs exceed host CPU count: '
            f'requested={cfg.vm.cpus}, host_cpus={cpu_count}. '
            'If VM creation fails, lower vm.cpus.'
        )

    free_gb = host_free_disk_gb(Path(cfg.paths.base_dir).expanduser())
    if free_gb is not None and float(cfg.vm.disk_gb) > float(free_gb) * 0.9:
        warnings.append(
            'Requested VM disk may be too large for free space at base_dir: '
            f'requested={cfg.vm.disk_gb} GiB, free≈{free_gb:.1f} GiB '
            f'(base_dir={cfg.paths.base_dir}). '
            'If provisioning fails later, lower vm.disk_gb or free disk space.'
        )
    return warnings


def vm_resource_impossible_lines(cfg: AgentVMConfig) -> list[str]:
    problems: list[str] = []
    mem_total_mb = host_mem_total_mb()
    if mem_total_mb is not None and cfg.vm.ram_mb > mem_total_mb:
        problems.append(
            f'vm.ram_mb={cfg.vm.ram_mb} exceeds host MemTotal={mem_total_mb} MiB'
        )

    cpu_count = host_cpu_count()
    if cpu_count is not None and cfg.vm.cpus > cpu_count:
        problems.append(
            f'vm.cpus={cfg.vm.cpus} exceeds host CPU count={cpu_count}'
        )
    return problems
