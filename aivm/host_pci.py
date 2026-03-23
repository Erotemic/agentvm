"""AIVM-managed boot-time VFIO host preparation helpers."""

from __future__ import annotations

import tempfile
import textwrap
from dataclasses import dataclass
from pathlib import Path

from .commands import CommandManager, IntentScope, PlanScope
from .pci import PCIDevice, inspect_pci_device

MODULES_LOAD_DIR = Path('/etc/modules-load.d')
INITRAMFS_SCRIPT_DIR = Path('/etc/initramfs-tools/scripts/init-top')


@dataclass(frozen=True)
class HostVFIOBootPrep:
    vm_name: str
    bdfs: tuple[str, ...]
    modules_load_path: Path
    initramfs_script_path: Path


def _slug(text: str) -> str:
    safe = ''.join(ch.lower() if ch.isalnum() else '-' for ch in text)
    while '--' in safe:
        safe = safe.replace('--', '-')
    return safe.strip('-') or 'vm'


def prep_paths(vm_name: str) -> HostVFIOBootPrep:
    slug = _slug(vm_name)
    return HostVFIOBootPrep(
        vm_name=vm_name,
        bdfs=(),
        modules_load_path=MODULES_LOAD_DIR / f'aivm-vfio-{slug}.conf',
        initramfs_script_path=INITRAMFS_SCRIPT_DIR / f'aivm-vfio-bind-{slug}',
    )


def render_modules_load_conf(vm_name: str, bdfs: list[str]) -> str:
    rendered_bdfs = ' '.join(sorted(set(bdfs)))
    return textwrap.dedent(
        f'''
        # Managed by aivm for VM {vm_name}
        # Boot-time VFIO prep for PCI devices: {rendered_bdfs}
        vfio
        vfio-pci
        vfio_iommu_type1
        '''
    ).lstrip()


def render_initramfs_bind_script(vm_name: str, bdfs: list[str]) -> str:
    lines = [
        '#!/bin/sh',
        f'# Managed by aivm for VM {vm_name}',
        'PREREQ=""',
        'prereqs() {',
        '    echo "$PREREQ"',
        '}',
        'case "$1" in',
        '    prereqs)',
        '        prereqs',
        '        exit 0',
        '        ;;',
        'esac',
        'modprobe vfio || true',
        'modprobe vfio-pci || true',
        '',
    ]
    for bdf in sorted(set(bdfs)):
        lines.extend(
            [
                f'if [ -e "/sys/bus/pci/devices/{bdf}" ]; then',
                f'    echo vfio-pci > "/sys/bus/pci/devices/{bdf}/driver_override"',
                f'    if [ -e "/sys/bus/pci/devices/{bdf}/driver/unbind" ]; then',
                f'        echo "{bdf}" > "/sys/bus/pci/devices/{bdf}/driver/unbind" || true',
                '    fi',
                f'    echo "{bdf}" > /sys/bus/pci/drivers/vfio-pci/bind || true',
                'fi',
                '',
            ]
        )
    return '\n'.join(lines).rstrip() + '\n'


def describe_host_prep(vm_name: str, devices: list[PCIDevice]) -> str:
    paths = prep_paths(vm_name)
    bdfs = ', '.join(device.bdf for device in devices)
    return textwrap.dedent(
        f'''
        Stable boot-time GPU attachment will make host-level changes.

        AIVM will manage these files:
          {paths.modules_load_path}
          {paths.initramfs_script_path}

        What this does:
          - loads vfio modules at boot for this VM's passthrough workflow
          - binds the selected PCI devices to vfio-pci during early boot
          - rebuilds initramfs so the binding happens on the next host boot

        Consequences:
          - the host and guest cannot use this GPU at the same time
          - after reboot, the host may lose display or compute access on this GPU
          - undoing this later also requires another host reboot

        Undo with AIVM:
          aivm vm gpu detach {devices[0].bdf} --vm {vm_name}

        Manual undo:
          - remove the AIVM-managed files above
          - run: sudo update-initramfs -u
          - reboot the host
          - verify the normal host driver rebinds to: {bdfs}
        '''
    ).strip()


def apply_vfio_boot_prep(vm_name: str, bdfs: list[str]) -> HostVFIOBootPrep:
    paths = prep_paths(vm_name)
    prep = HostVFIOBootPrep(
        vm_name=vm_name,
        bdfs=tuple(sorted(set(bdfs))),
        modules_load_path=paths.modules_load_path,
        initramfs_script_path=paths.initramfs_script_path,
    )
    modules_text = render_modules_load_conf(vm_name, bdfs)
    script_text = render_initramfs_bind_script(vm_name, bdfs)
    with tempfile.NamedTemporaryFile('w', delete=False) as file:
        file.write(modules_text)
        modules_tmp = file.name
    with tempfile.NamedTemporaryFile('w', delete=False) as file:
        file.write(script_text)
        script_tmp = file.name
    mgr = CommandManager.current()
    try:
        with IntentScope(
            mgr,
            'Prepare host GPU VFIO boot binding',
            why=(
                'Stable GPU passthrough needs an explicit host boot-time '
                'handoff from the normal host driver to vfio-pci.'
            ),
            role='modify',
        ):
            with PlanScope(
                mgr,
                'Write AIVM-managed VFIO boot files',
                why=(
                    'Install explicit AIVM-managed boot files and rebuild '
                    'initramfs so the selected GPU binds to vfio-pci on the '
                    'next host reboot.'
                ),
                approval_scope=f'gpu-host-prep:{vm_name}',
            ):
                mgr.submit(
                    ['install', '-D', '-m', '0644', modules_tmp, str(prep.modules_load_path)],
                    sudo=True,
                    role='modify',
                    summary='Install AIVM-managed vfio modules-load file',
                    detail=str(prep.modules_load_path),
                )
                mgr.submit(
                    ['install', '-D', '-m', '0755', script_tmp, str(prep.initramfs_script_path)],
                    sudo=True,
                    role='modify',
                    summary='Install AIVM-managed initramfs VFIO bind script',
                    detail=str(prep.initramfs_script_path),
                )
                mgr.submit(
                    ['update-initramfs', '-u'],
                    sudo=True,
                    role='modify',
                    summary='Rebuild initramfs for next-boot VFIO binding',
                )
    finally:
        Path(modules_tmp).unlink(missing_ok=True)
        Path(script_tmp).unlink(missing_ok=True)
    return prep


def remove_vfio_boot_prep(vm_name: str) -> HostVFIOBootPrep:
    prep = prep_paths(vm_name)
    mgr = CommandManager.current()
    with IntentScope(
        mgr,
        'Undo host GPU VFIO boot binding',
        why=(
            'Detaching stable GPU passthrough should remove AIVM-managed boot '
            'binding so the host can reclaim the device after reboot.'
        ),
        role='modify',
    ):
        with PlanScope(
            mgr,
            'Remove AIVM-managed VFIO boot files',
            why=(
                'Delete the AIVM-managed boot-time VFIO files and rebuild '
                'initramfs so the host can rebind its normal driver after reboot.'
            ),
            approval_scope=f'gpu-host-unprep:{vm_name}',
        ):
            mgr.submit(
                ['rm', '-f', str(prep.modules_load_path)],
                sudo=True,
                role='modify',
                summary='Remove AIVM-managed vfio modules-load file',
                detail=str(prep.modules_load_path),
            )
            mgr.submit(
                ['rm', '-f', str(prep.initramfs_script_path)],
                sudo=True,
                role='modify',
                summary='Remove AIVM-managed initramfs VFIO bind script',
                detail=str(prep.initramfs_script_path),
            )
            mgr.submit(
                ['update-initramfs', '-u'],
                sudo=True,
                role='modify',
                summary='Rebuild initramfs after removing VFIO boot prep',
            )
    return prep


def boot_prep_paths_for_vm(vm_name: str) -> tuple[Path, Path]:
    prep = prep_paths(vm_name)
    return prep.modules_load_path, prep.initramfs_script_path


def boot_prep_file_descriptions(vm_name: str, bdfs: list[str]) -> list[str]:
    devices = [inspect_pci_device(bdf) for bdf in sorted(set(bdfs))]
    return describe_host_prep(vm_name, devices).splitlines()
