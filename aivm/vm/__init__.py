"""VM operation exports for lifecycle, share, and sync helpers."""

from __future__ import annotations

from .lifecycle import (
    _ensure_disk,
    _ensure_qemu_access,
    _mac_for_vm,
    _paths,
    _sudo_file_exists,
    _sudo_path_exists,
    _write_cloud_init,
    create_or_start_vm,
    destroy_vm,
    fetch_image,
    get_ip_cached,
    provision,
    ssh_config,
    vm_exists,
    vm_status,
    wait_for_ip,
    wait_for_ssh,
)
from .share import (
    attach_vm_share,
    ensure_share_mounted,
    vm_has_share,
    vm_has_virtiofs_shared_memory,
    vm_share_mappings,
)
from .sync import sync_settings

__all__ = [
    '_ensure_disk',
    '_ensure_qemu_access',
    '_mac_for_vm',
    '_paths',
    '_sudo_file_exists',
    '_sudo_path_exists',
    '_write_cloud_init',
    'attach_vm_share',
    'create_or_start_vm',
    'destroy_vm',
    'ensure_share_mounted',
    'fetch_image',
    'get_ip_cached',
    'provision',
    'ssh_config',
    'sync_settings',
    'vm_exists',
    'vm_has_share',
    'vm_has_virtiofs_shared_memory',
    'vm_share_mappings',
    'vm_status',
    'wait_for_ip',
    'wait_for_ssh',
]
