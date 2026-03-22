"""Helpers for persistent libvirt PCI hostdev reconciliation."""

from __future__ import annotations

import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from ..commands import CommandManager
from ..runtime import virsh_system_cmd
from ..util import CmdError


@dataclass(frozen=True)
class HostdevDrift:
    declared: tuple[str, ...]
    persistent: tuple[str, ...]
    live: tuple[str, ...]

    @property
    def only_declared(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.declared) - set(self.persistent)))

    @property
    def only_persistent(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.persistent) - set(self.declared)))

    @property
    def only_live(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.live) - set(self.persistent)))


def render_hostdev_xml(bdfs: list[str]) -> str:
    devices = ET.Element('devices')
    for bdf in sorted(set(bdfs)):
        domain, bus, slot_fn = bdf.split(':')
        slot, function = slot_fn.split('.')
        hostdev = ET.SubElement(
            devices,
            'hostdev',
            attrib={'mode': 'subsystem', 'type': 'pci', 'managed': 'yes'},
        )
        source = ET.SubElement(hostdev, 'source')
        ET.SubElement(
            source,
            'address',
            attrib={
                'domain': f'0x{domain}',
                'bus': f'0x{bus}',
                'slot': f'0x{slot}',
                'function': f'0x{function}',
            },
        )
    return ET.tostring(devices, encoding='unicode')


def _render_one_hostdev_xml(bdf: str) -> str:
    xml = render_hostdev_xml([bdf])
    root = ET.fromstring(xml)
    return ET.tostring(root[0], encoding='unicode')


def _extract_hostdev_bdfs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    found: list[str] = []
    for hostdev in root.findall('.//devices/hostdev[@type="pci"]'):
        address = hostdev.find('./source/address')
        if address is None:
            continue
        domain = (address.get('domain', '') or '').replace('0x', '').zfill(4)
        bus = (address.get('bus', '') or '').replace('0x', '').zfill(2)
        slot = (address.get('slot', '') or '').replace('0x', '').zfill(2)
        function = (address.get('function', '') or '').replace('0x', '')
        if not all((domain, bus, slot, function)):
            continue
        found.append(f'{domain}:{bus}:{slot}.{function}')
    return sorted(set(found))


def _dumpxml(vm_name: str, *, inactive: bool) -> str:
    cmd = virsh_system_cmd('dumpxml', vm_name, '--inactive')
    if not inactive:
        cmd = virsh_system_cmd('dumpxml', vm_name)
    return (
        CommandManager.current()
        .submit(
            cmd,
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=(
                f'Inspect {"persistent" if inactive else "live"} PCI hostdevs for VM {vm_name}'
            ),
        )
        .result()
        .stdout
    )


def domain_hostdevs_persistent(vm_name: str) -> list[str]:
    return _extract_hostdev_bdfs(_dumpxml(vm_name, inactive=True))


def domain_hostdevs_live(vm_name: str) -> list[str]:
    return _extract_hostdev_bdfs(_dumpxml(vm_name, inactive=False))


def vm_is_running(vm_name: str) -> bool:
    result = (
        CommandManager.current()
        .submit(
            virsh_system_cmd('domstate', vm_name),
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Check whether VM {vm_name} is running',
        )
        .result()
    )
    return result.code == 0 and 'running' in result.stdout.strip().lower()


def _apply_hostdev_change(
    vm_name: str,
    bdf: str,
    *,
    action: str,
    live: bool = False,
) -> None:
    xml = _render_one_hostdev_xml(bdf)
    with tempfile.NamedTemporaryFile('w', delete=False) as file:
        file.write(xml)
        tmp = file.name
    try:
        cmd = virsh_system_cmd(
            f'{action}-device',
            vm_name,
            tmp,
            '--live' if live else '--config',
        )
        if live:
            cmd = virsh_system_cmd(f'{action}-device', vm_name, tmp, '--live')
        result = (
            CommandManager.current()
            .submit(
                cmd,
                sudo=True,
                role='modify',
                check=False,
                capture=True,
                summary=f'{action.title()} PCI hostdev {bdf} for VM {vm_name}',
            )
            .result()
        )
        if result.code != 0:
            raise CmdError(cmd, result)
    finally:
        Path(tmp).unlink(missing_ok=True)


def ensure_hostdev_persistent(vm_name: str, bdfs: list[str]) -> list[str]:
    existing = set(domain_hostdevs_persistent(vm_name))
    changed: list[str] = []
    for bdf in sorted(set(bdfs)):
        if bdf in existing:
            continue
        _apply_hostdev_change(vm_name, bdf, action='attach')
        changed.append(bdf)
    return changed


def detach_hostdev_persistent(vm_name: str, bdfs: list[str]) -> list[str]:
    existing = set(domain_hostdevs_persistent(vm_name))
    changed: list[str] = []
    for bdf in sorted(set(bdfs)):
        if bdf not in existing:
            continue
        _apply_hostdev_change(vm_name, bdf, action='detach')
        changed.append(bdf)
    return changed


def compute_hostdev_drift(
    *,
    declared: list[str],
    persistent: list[str],
    live: list[str],
) -> HostdevDrift:
    return HostdevDrift(
        declared=tuple(sorted(set(declared))),
        persistent=tuple(sorted(set(persistent))),
        live=tuple(sorted(set(live))),
    )
