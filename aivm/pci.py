"""Conservative PCI/GPU passthrough inspection helpers.

This module intentionally aims for a small, safety-first first pass. It helps
the CLI answer two questions:

1. what device set should move together for one GPU passthrough request?
2. is the host-side PCI state plausibly ready for persistent passthrough?
"""

from __future__ import annotations

import textwrap
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

from .commands import CommandManager, IntentScope, PlanScope
from .runtime import virsh_system_cmd


@dataclass(frozen=True)
class PCIDevice:
    bdf: str
    nodedev_name: str
    class_code: str = ''
    vendor_id: str = ''
    device_id: str = ''
    vendor_name: str = ''
    product_name: str = ''
    driver: str = ''
    iommu_group: tuple[str, ...] = ()

    @property
    def slot_key(self) -> str:
        return self.bdf.rsplit('.', 1)[0]

    @property
    def description(self) -> str:
        parts = [self.vendor_name.strip(), self.product_name.strip()]
        return ' '.join(p for p in parts if p).strip()


@dataclass(frozen=True)
class PCIReadiness:
    bdf: str
    status: str
    primary: PCIDevice | None = None
    companions: tuple[PCIDevice, ...] = ()
    unexpected: tuple[PCIDevice, ...] = ()
    issues: tuple[str, ...] = ()
    recommendations: tuple[str, ...] = ()
    iommu_enabled: bool = False


@dataclass(frozen=True)
class GPUCandidate:
    index: int
    name: str
    primary_bdf: str
    companion_bdfs: tuple[str, ...] = ()
    driver: str = ''
    readiness_status: str = ''
    summary: str = ''


def normalize_bdf(bdf: str) -> str:
    text = str(bdf or '').strip().lower()
    if not text:
        raise RuntimeError('PCI BDF must be non-empty.')
    parts = text.split(':')
    if len(parts) == 2:
        text = f'0000:{text}'
    pieces = text.split(':')
    if len(pieces) != 3 or '.' not in pieces[2]:
        raise RuntimeError(
            f'Invalid PCI BDF: {bdf!r}. Expected domain:bus:slot.function.'
        )
    domain, bus, slot_fn = pieces
    slot, fn = slot_fn.split('.', 1)
    if not (
        len(domain) == 4
        and len(bus) == 2
        and len(slot) == 2
        and len(fn) == 1
    ):
        raise RuntimeError(
            f'Invalid PCI BDF: {bdf!r}. Expected domain:bus:slot.function.'
        )
    return f'{domain}:{bus}:{slot}.{fn}'


def maybe_bdf_to_nodedev_name(bdf: str) -> str:
    normalized = normalize_bdf(bdf)
    return f"pci_{normalized.replace(':', '_').replace('.', '_')}"


def _read_sysfs_text(path: Path) -> str:
    try:
        return path.read_text(encoding='utf-8').strip()
    except Exception:
        return ''


def _device_sysfs_path(bdf: str) -> Path:
    return Path('/sys/bus/pci/devices') / normalize_bdf(bdf)


def _parse_libvirt_pci_device_xml(xml_text: str) -> dict[str, str]:
    info: dict[str, str] = {}
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return info

    cap = root.find(".//capability[@type='pci']")
    if cap is None:
        return info

    vendor = cap.find('vendor')
    if vendor is not None:
        info['vendor_id'] = vendor.get('id', '') or ''
        info['vendor_name'] = (vendor.text or '').strip()
    product = cap.find('product')
    if product is not None:
        info['device_id'] = product.get('id', '') or ''
        info['product_name'] = (product.text or '').strip()
    iommu = cap.find(".//iommuGroup")
    if iommu is not None:
        info['iommu_group_number'] = iommu.get('number', '') or ''
    driver = root.find(".//driver/name")
    if driver is not None and (driver.text or '').strip():
        info['driver'] = (driver.text or '').strip()
    return info


def _probe_nodedev_xml(bdf: str) -> str:
    mgr = CommandManager.current()
    return (
        mgr.submit(
            virsh_system_cmd('nodedev-dumpxml', maybe_bdf_to_nodedev_name(bdf)),
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Inspect PCI node device {normalize_bdf(bdf)} via libvirt',
        )
        .result()
        .stdout
    )


def inspect_pci_device(bdf: str) -> PCIDevice:
    normalized = normalize_bdf(bdf)
    dev_path = _device_sysfs_path(normalized)
    if not dev_path.exists():
        raise RuntimeError(f'PCI device not found on host: {normalized}')

    xml_info = _parse_libvirt_pci_device_xml(_probe_nodedev_xml(normalized))
    iommu_group_dir = dev_path / 'iommu_group' / 'devices'
    iommu_group = tuple(
        sorted(p.name for p in iommu_group_dir.iterdir())
    ) if iommu_group_dir.exists() else ()
    driver_link = dev_path / 'driver'
    driver = driver_link.resolve().name if driver_link.exists() else ''
    if not driver:
        driver = xml_info.get('driver', '')
    return PCIDevice(
        bdf=normalized,
        nodedev_name=maybe_bdf_to_nodedev_name(normalized),
        class_code=_read_sysfs_text(dev_path / 'class').lower(),
        vendor_id=(_read_sysfs_text(dev_path / 'vendor') or '').lower(),
        device_id=(_read_sysfs_text(dev_path / 'device') or '').lower(),
        vendor_name=xml_info.get('vendor_name', ''),
        product_name=xml_info.get('product_name', ''),
        driver=driver,
        iommu_group=iommu_group,
    )


def _is_display_device(device: PCIDevice) -> bool:
    return device.class_code in {'0x030000', '0x030200', '0x038000'}


def _is_companion_device(primary: PCIDevice, candidate: PCIDevice) -> bool:
    if not _is_display_device(primary):
        return False
    if candidate.slot_key != primary.slot_key:
        return False
    if (
        primary.vendor_id
        and candidate.vendor_id
        and candidate.vendor_id != primary.vendor_id
    ):
        return False
    if candidate.class_code not in {'0x040300', '0x040380'}:
        return False
    text = f'{candidate.vendor_name} {candidate.product_name}'.lower()
    # Keep this conservative: a likely HDMI/DP audio function on the same GPU
    # package is okay, but generic audio devices should still block.
    return any(
        token in text for token in ('hdmi', 'display audio', 'high definition')
    )


def resolve_passthrough_set_for_gpu(
    bdf: str,
) -> tuple[list[PCIDevice], list[PCIDevice]]:
    primary = inspect_pci_device(bdf)
    members = [inspect_pci_device(item) for item in (primary.iommu_group or ())]
    if not members:
        members = [primary]
    companions: list[PCIDevice] = []
    unexpected: list[PCIDevice] = []
    seen: set[str] = set()
    for device in members:
        if device.bdf == primary.bdf or _is_companion_device(primary, device):
            if device.bdf not in seen:
                companions.append(device)
                seen.add(device.bdf)
        else:
            unexpected.append(device)
    companions.sort(key=lambda d: d.bdf)
    unexpected.sort(key=lambda d: d.bdf)
    return companions, unexpected


def _check_iommu_enabled() -> bool:
    groups_dir = Path('/sys/kernel/iommu_groups')
    if not groups_dir.exists():
        return False
    try:
        return any(groups_dir.iterdir())
    except Exception:
        return False


def assess_device_readiness(bdf: str) -> PCIReadiness:
    normalized = normalize_bdf(bdf)
    mgr = CommandManager.current()
    issues: list[str] = []
    recommendations: list[str] = []
    with IntentScope(
        mgr,
        'Check PCI passthrough readiness',
        why=(
            'GPU passthrough should fail clearly before mutating VM config when '
            'the host PCI topology or binding state is not convincingly ready.'
        ),
        role='read',
    ):
        with PlanScope(
            mgr,
            f'Inspect PCI device {normalized}',
            why=(
                'Probe the host PCI device, its IOMMU group, and current driver '
                'binding without changing host state.'
            ),
            approval_scope=f'pci-check:{normalized}',
        ):
            primary = inspect_pci_device(normalized)
            companions, unexpected = resolve_passthrough_set_for_gpu(normalized)
            iommu_enabled = _check_iommu_enabled()
    if not _is_display_device(primary):
        issues.append(
            f'{normalized} is not a display-class PCI device; this first pass only supports GPU-style passthrough.'
        )
    if not iommu_enabled:
        issues.append(
            'IOMMU support is not convincingly enabled on this host.'
        )
        recommendations.append(
            'Enable VT-d/AMD-Vi and verify /sys/kernel/iommu_groups is populated before retrying.'
        )
    if not primary.iommu_group:
        issues.append(
            f'{normalized} does not expose an IOMMU group in sysfs.'
        )
    if unexpected:
        issues.append(
            'The device shares an IOMMU group with non-companion devices that would also need passthrough.'
        )
        recommendations.append(
            'Use a different GPU or move hardware so the group contains only the GPU and its expected audio companion.'
        )
    missing_vfio = [
        device.bdf for device in companions if device.driver != 'vfio-pci'
    ]
    if missing_vfio:
        issues.append(
            'One or more passthrough devices are not bound to vfio-pci: '
            + ', '.join(missing_vfio)
        )
        recommendations.append(
            'Bind every passthrough device in the resolved set to vfio-pci before attaching it to a VM.'
        )
    status = 'ready_persistent_restart'
    if issues:
        status = 'manual_steps_required'
    return PCIReadiness(
        bdf=normalized,
        status=status,
        primary=primary,
        companions=tuple(companions),
        unexpected=tuple(unexpected),
        issues=tuple(issues),
        recommendations=tuple(recommendations),
        iommu_enabled=iommu_enabled,
    )


def render_readiness_report(report: PCIReadiness) -> str:
    primary = report.primary
    lines = [
        f'PCI device: {report.bdf}',
        f'Status: {report.status}',
    ]
    if primary is not None:
        summary = primary.description or primary.bdf
        lines.extend(
            [
                f'Primary: {summary}',
                f'Driver: {primary.driver or "(unbound)"}',
                f'IOMMU enabled: {"yes" if report.iommu_enabled else "no"}',
            ]
        )
    if report.companions:
        lines.append('Passthrough set:')
        lines.extend(f'  - {device.bdf}' for device in report.companions)
    if report.unexpected:
        lines.append('Unexpected IOMMU-group members:')
        lines.extend(
            f'  - {device.bdf} {device.description}'.rstrip()
            for device in report.unexpected
        )
    if report.issues:
        lines.append('Blocking issues:')
        lines.extend(f'  - {item}' for item in report.issues)
    if report.recommendations:
        lines.append('Recommendations:')
        lines.extend(
            f'  - {textwrap.fill(item, width=78, subsequent_indent="    ")}'
            for item in report.recommendations
        )
    return '\n'.join(lines)


def _iter_display_device_bdfs() -> list[str]:
    device_root = Path('/sys/bus/pci/devices')
    if not device_root.exists():
        return []
    found: list[str] = []
    for path in sorted(device_root.iterdir(), key=lambda p: p.name):
        if _read_sysfs_text(path / 'class').lower() in {
            '0x030000',
            '0x030200',
            '0x038000',
        }:
            found.append(path.name.lower())
    return found


def _candidate_name(primary: PCIDevice) -> str:
    name = primary.description.strip()
    return name or f'GPU {primary.bdf}'


def list_gpu_candidates() -> list[GPUCandidate]:
    candidates: list[GPUCandidate] = []
    for index, bdf in enumerate(_iter_display_device_bdfs()):
        primary = inspect_pci_device(bdf)
        companions, _unexpected = resolve_passthrough_set_for_gpu(primary.bdf)
        readiness = assess_device_readiness(primary.bdf)
        companion_bdfs = tuple(
            device.bdf for device in companions if device.bdf != primary.bdf
        )
        name = _candidate_name(primary)
        candidates.append(
            GPUCandidate(
                index=index,
                name=name,
                primary_bdf=primary.bdf,
                companion_bdfs=companion_bdfs,
                driver=primary.driver,
                readiness_status=readiness.status,
                summary=f'[{index}] {name} ({primary.bdf})',
            )
        )
    return candidates


def render_gpu_candidate_list(candidates: list[GPUCandidate]) -> str:
    if not candidates:
        return 'No GPU candidates were detected.'
    lines = ['Detected GPUs:', '']
    for candidate in candidates:
        lines.append(f'  [{candidate.index}] {candidate.name}')
        lines.append(f'      graphics: {candidate.primary_bdf}')
        if candidate.companion_bdfs:
            lines.append(
                '      companion: ' + ', '.join(candidate.companion_bdfs)
            )
        else:
            lines.append('      companion: (none)')
        lines.append(f'      driver: {candidate.driver or "(unbound)"}')
        if candidate.readiness_status:
            lines.append(f'      readiness: {candidate.readiness_status}')
        lines.append('')
    return '\n'.join(lines).rstrip()


def _render_gpu_selection_help(
    candidates: list[GPUCandidate], *, vm_name: str = ''
) -> str:
    lines = [render_gpu_candidate_list(candidates), '', 'Choose a GPU by running one of:']
    if candidates:
        example = candidates[0]
        vm_part = f' --vm {vm_name}' if vm_name else ''
        lines.append(
            f'  aivm vm gpu attach {example.index}{vm_part}'
        )
        lines.append(
            f'  aivm vm gpu attach "{example.name}"{vm_part}'
        )
        lines.append(
            f'  aivm vm gpu attach {example.primary_bdf}{vm_part}'
        )
    else:
        lines.append('  aivm host pci check 0000:65:00.0')
    return '\n'.join(lines)


def _choose_gpu_candidate_interactive(
    candidates: list[GPUCandidate],
) -> GPUCandidate:
    print(render_gpu_candidate_list(candidates))
    while True:
        raw = input('Select GPU number: ').strip()
        if not raw.isdigit():
            print('Please enter a number.')
            continue
        choice = int(raw)
        for candidate in candidates:
            if candidate.index == choice:
                return candidate
        print(f'Please enter a number between 0 and {len(candidates) - 1}.')


def resolve_gpu_selector(
    selector: str | None,
    *,
    interactive: bool,
    vm_name: str = '',
) -> GPUCandidate:
    candidates = list_gpu_candidates()
    text = str(selector or '').strip()
    if not candidates:
        raise RuntimeError(
            'No GPU candidates were detected on this host.'
        )
    if not text:
        if interactive:
            return _choose_gpu_candidate_interactive(candidates)
        raise RuntimeError(
            'GPU selector is required in non-interactive mode.\n'
            + _render_gpu_selection_help(candidates, vm_name=vm_name)
        )

    try:
        normalized = normalize_bdf(text)
    except RuntimeError:
        normalized = ''
    if normalized:
        for candidate in candidates:
            if candidate.primary_bdf == normalized:
                return candidate
        raise RuntimeError(
            f'GPU BDF not found among detected GPU candidates: {normalized}\n'
            + _render_gpu_selection_help(candidates, vm_name=vm_name)
        )

    if text.isdigit():
        idx = int(text)
        for candidate in candidates:
            if candidate.index == idx:
                return candidate
        raise RuntimeError(
            f'GPU index out of range: {idx}\n'
            + _render_gpu_selection_help(candidates, vm_name=vm_name)
        )

    lowered = text.lower()
    exact = [candidate for candidate in candidates if candidate.name.lower() == lowered]
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        raise RuntimeError(
            f'Ambiguous GPU name selector: {text!r}\n'
            + _render_gpu_selection_help(candidates, vm_name=vm_name)
        )

    substring = [
        candidate for candidate in candidates if lowered in candidate.name.lower()
    ]
    if len(substring) == 1:
        return substring[0]
    if len(substring) > 1:
        raise RuntimeError(
            f'Ambiguous GPU selector: {text!r}\n'
            + _render_gpu_selection_help(candidates, vm_name=vm_name)
        )

    raise RuntimeError(
        f'No detected GPU matched selector: {text!r}\n'
        + _render_gpu_selection_help(candidates, vm_name=vm_name)
    )
