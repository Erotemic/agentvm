"""Shared VM configuration drift detection module.

This module provides a clean separation between drift detection and drift handling.
Its job is to:
  * compute desired VM/share shape from config + attachment intent
  * compute actual VM/share shape from libvirt
  * diff them
  * return structured drift information

Callers decide what to do with the drift information (warn, auto-heal, report, etc.).

This avoids duplicating drift logic across status, vm up warnings, and attachment
reconcile flows.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import virsh_system_cmd
from ..store import Store, find_attachments_for_vm
from .share import (
    SHARED_ROOT_VIRTIOFS_TAG,
    AttachmentMode,
    ResolvedAttachment,
    align_attachment_tag_with_mappings,
    vm_share_mappings,
)


def _shared_root_host_dir(cfg: AgentVMConfig) -> Path:
    """Get the shared-root host directory for a VM.

    Note: This is a simple path computation based on config, not a libvirt query.
    """
    return Path(cfg.paths.base_dir) / cfg.vm.name / 'shared-root'


@dataclass(frozen=True)
class DriftItem:
    """A single drift item describing a mismatch between expected and actual state."""

    key: str
    """A stable identifier for this drift item (e.g., 'cpus', 'ram_mb', 'share_mappings')."""

    expected: object
    """The expected/desired value from config."""

    actual: object
    """The actual value observed from libvirt."""

    reason: str = ''
    """Optional human-readable explanation for why this drift exists."""


@dataclass(frozen=True)
class DriftReport:
    """Structured drift report for a VM configuration.

    Attributes:
        available: Whether the report is available. False when libvirt queries fail
            due to permissions or other transient issues. True when the report was
            successfully generated (even if drift is detected).
        summary: A short human-readable summary of the drift status.
        items: Tuple of DriftItem instances describing specific drifts.
        diag: Optional diagnostic information for troubleshooting.
    """

    available: bool
    summary: str
    items: tuple[DriftItem, ...] = ()
    diag: str = ''

    @property
    def ok(self) -> bool | None:
        """Return True if no drift, False if drift exists, None if unavailable."""
        if not self.available:
            return None
        return len(self.items) == 0


def parse_dominfo_hardware(dominfo_text: str) -> tuple[int | None, int | None]:
    """Parse CPU and memory from virsh dominfo output.

    virsh dominfo typically reports memory in KiB, but may include unit suffixes.
    This function normalizes all values to MiB.

    Args:
        dominfo_text: Raw stdout from `virsh dominfo <vm_name>`.

    Returns:
        A tuple of (cpus, max_mem_mib) where:
            - cpus: Number of vCPUs or None if parsing fails
            - max_mem_mib: Max memory in MiB or None if parsing fails
    """
    cpus = None
    max_mem_mib = None
    for line in (dominfo_text or '').splitlines():
        if ':' not in line:
            continue
        key, val = [x.strip() for x in line.split(':', 1)]
        low = key.lower()
        if low in {'cpu(s)', 'cpus'}:
            m = re.search(r'(\d+)', val)
            if m:
                cpus = int(m.group(1))
        elif low.startswith('max memory'):
            # Match number with optional unit suffix (KiB, MiB, GiB, KB, MB, GB)
            m = re.search(r'(\d+)\s*(kib|kb|mib|mb|gib|gb)?', val, flags=re.I)
            if m:
                amount = int(m.group(1))
                unit = (
                    m.group(2) or 'kib'
                ).lower()  # Default to KiB if no unit
                if unit in ('kib', 'kb'):
                    max_mem_mib = amount // 1024
                elif unit in ('mib', 'mb'):
                    max_mem_mib = amount
                elif unit in ('gib', 'gb'):
                    max_mem_mib = amount * 1024
    return cpus, max_mem_mib


def read_actual_vm_hardware(
    cfg: AgentVMConfig, *, use_sudo: bool
) -> tuple[int | None, int | None, str, str]:
    """Read actual VM hardware from libvirt via virsh dominfo.

    Args:
        cfg: The VM configuration.
        use_sudo: Whether to run virsh commands with sudo.

    Returns:
        A tuple of (cpus, max_mem_mib, error_type, error_detail).
        cpus/max_mem_mib are None if the VM is not defined or query fails.
        error_type is one of: 'not_found', 'permission', 'other', or '' on success.
        error_detail contains the raw error message.
    """
    cmd = virsh_system_cmd('dominfo', cfg.vm.name)
    res = CommandManager.current().run(
        cmd, sudo=use_sudo, check=False, capture=True
    )
    if res.code != 0:
        # Check both stderr and stdout for error messages
        raw = (res.stderr or res.stdout or 'virsh dominfo failed').strip()
        # Classify the error type
        if 'not found' in raw.lower() or 'no domain' in raw.lower():
            return None, None, 'not_found', raw
        elif 'permission' in raw.lower() or 'denied' in raw.lower():
            return None, None, 'permission', raw
        else:
            return None, None, 'other', raw
    cpus, max_mem_mib = parse_dominfo_hardware(res.stdout)
    return cpus, max_mem_mib, '', ''


def expected_mapping_for_attachment(
    cfg: AgentVMConfig, attachment: 'ResolvedAttachment'
) -> tuple[str, str] | None:
    """Compute the expected virtiofs mapping for an attachment.

    Args:
        cfg: The VM configuration.
        attachment: The resolved attachment.

    Returns:
        A tuple of (host_source, tag) or None if attachment mode doesn't use virtiofs.
    """
    if attachment.mode == AttachmentMode.SHARED:
        return attachment.source_dir, attachment.tag
    if attachment.mode == AttachmentMode.SHARED_ROOT:
        return str(_shared_root_host_dir(cfg)), SHARED_ROOT_VIRTIOFS_TAG
    return None


def attachment_has_mapping(
    cfg: AgentVMConfig,
    att: 'ResolvedAttachment',
    mappings: list[tuple[str, str]],
) -> bool:
    """Check if an attachment has a matching mapping in the VM.

    For shared mode, checks source_dir and tag directly.
    For shared-root mode, checks the canonical shared-root host dir and tag.

    Args:
        cfg: The VM configuration (needed for shared-root path resolution).
        att: The attachment to check.
        mappings: List of (source, tag) tuples from the VM.

    Returns:
        True if a matching mapping exists, False otherwise.
    """
    if att.mode == AttachmentMode.SHARED_ROOT:
        expected_src = str(_shared_root_host_dir(cfg))
        expected_tag = SHARED_ROOT_VIRTIOFS_TAG
        return any(
            src == expected_src and tag == expected_tag for src, tag in mappings
        )
    return any(
        src == att.source_dir and tag == att.tag for src, tag in mappings
    )


def read_actual_vm_mappings(
    cfg: AgentVMConfig, *, use_sudo: bool
) -> tuple[list[tuple[str, str]] | None, str]:
    """Read current VM share mappings from libvirt.

    Args:
        cfg: The VM configuration.
        use_sudo: Whether to run virsh commands with sudo.

    Returns:
        A tuple of (mappings, error_or_empty).
        mappings is None if the VM is not defined or query fails.
        error_or_empty contains error message if available=False, else empty.
    """
    mappings = vm_share_mappings(cfg, use_sudo=use_sudo)
    if mappings is None:
        return None, 'Failed to read VM share mappings'
    return mappings, ''


def hardware_drift_report(cfg: AgentVMConfig, *, use_sudo: bool) -> DriftReport:
    """Compute a drift report for VM hardware (CPU, RAM).

    This compares the desired hardware from config against the actual
    hardware reported by libvirt.

    Args:
        cfg: The VM configuration.
        use_sudo: Whether to run virsh commands with sudo.

    Returns:
        A DriftReport with hardware drift items if available.
    """
    cur_cpus, cur_mem_mib, error_type, error_detail = read_actual_vm_hardware(
        cfg, use_sudo=use_sudo
    )

    # If the command failed, classify the error
    if error_type:
        if error_type == 'not_found':
            summary = 'VM not defined'
        elif error_type == 'permission':
            summary = 'Permission denied (try with --sudo)'
        else:
            summary = 'Libvirt query failed'
        return DriftReport(
            available=False,
            summary=summary,
            diag=error_detail,
        )

    items: list[DriftItem] = []
    parse_issues: list[str] = []

    # Check CPU drift
    want_cpus = int(cfg.vm.cpus)
    if cur_cpus is None:
        parse_issues.append('CPU count could not be parsed from libvirt')
    elif cur_cpus != want_cpus:
        items.append(
            DriftItem(
                key='cpus',
                expected=want_cpus,
                actual=cur_cpus,
                reason='Config specifies different vCPU count than current VM',
            )
        )

    # Check RAM drift
    want_mem_mib = int(cfg.vm.ram_mb)
    if cur_mem_mib is None:
        parse_issues.append('RAM could not be parsed from libvirt')
    elif cur_mem_mib != want_mem_mib:
        items.append(
            DriftItem(
                key='ram_mb',
                expected=want_mem_mib,
                actual=cur_mem_mib,
                reason='Config specifies different RAM than current VM',
            )
        )

    # If we have parse issues but no drift items, report as unavailable
    if parse_issues and not items:
        return DriftReport(
            available=False,
            summary='Hardware info could not be parsed',
            diag='; '.join(parse_issues),
        )

    if not items:
        return DriftReport(
            available=True,
            summary='Hardware matches config',
            items=(),
        )

    # Partial drift: report drift but include parse issues in diag
    if parse_issues:
        return DriftReport(
            available=True,
            summary=f'{len(items)} hardware setting(s) differ from config',
            items=tuple(items),
            diag='; '.join(parse_issues),
        )

    return DriftReport(
        available=True,
        summary=f'{len(items)} hardware setting(s) differ from config',
        items=tuple(items),
    )


def attachment_drift_report(
    cfg: AgentVMConfig,
    attachment: ResolvedAttachment,
    *,
    host_src: Path,
    use_sudo: bool,
) -> DriftReport:
    """Compute a drift report for a specific attachment's share mapping.

    This checks whether the VM currently has the expected virtiofs mapping
    for the given attachment.

    Note: This function uses tag alignment for compatibility with existing
    VM mappings. For strict config drift detection, use strict_expected_mapping
    instead.

    Args:
        cfg: The VM configuration.
        attachment: The resolved attachment.
        host_src: The host source path (used for tag alignment).
        use_sudo: Whether to run virsh commands with sudo.

    Returns:
        A DriftReport with share mapping drift items if available.
    """
    mappings, error = read_actual_vm_mappings(cfg, use_sudo=use_sudo)

    if mappings is None:
        return DriftReport(
            available=False,
            summary='VM not defined or libvirt query unavailable',
            diag=error,
        )

    # Align tag first to get the canonical expected mapping (compatibility mode)
    aligned = align_attachment_tag_with_mappings(attachment, host_src, mappings)

    # Use the aligned attachment to compute the expected mapping
    virtiofs_mapping = expected_mapping_for_attachment(cfg, aligned)
    if virtiofs_mapping is None:
        return DriftReport(
            available=True,
            summary='No virtiofs mapping expected for this attachment',
            items=(),
        )

    req_src, req_tag = virtiofs_mapping
    has_mapping = any(
        src == req_src and tag == req_tag for src, tag in mappings
    )

    if has_mapping:
        return DriftReport(
            available=True,
            summary='Share mapping present',
            items=(),
        )

    # Compute what the VM currently has that might be close
    current_for_src = [(src, tag) for src, tag in mappings if src == req_src]

    return DriftReport(
        available=True,
        summary='Share mapping missing from VM',
        items=(
            DriftItem(
                key='share_mapping',
                expected=(req_src, req_tag),
                actual=current_for_src if current_for_src else [],
                reason='VM does not have the expected virtiofs share mapping',
            ),
        ),
    )


def _compare_expected_vs_actual_mappings(
    expected_mappings: list[tuple[str, str]],
    actual_mappings: list[tuple[str, str]],
    *,
    reason_missing: str,
    reason_extra: str,
) -> list[DriftItem]:
    """Compare expected vs actual mappings and return drift items.

    This helper performs a two-way set diff to detect both missing expected
    mappings and unexpected extra mappings.

    Args:
        expected_mappings: List of expected (source, tag) tuples.
        actual_mappings: List of actual (source, tag) tuples from the VM.
        reason_missing: Reason string for missing mappings.
        reason_extra: Reason string for extra mappings.

    Returns:
        A list of DriftItem instances describing the drift.
    """
    items: list[DriftItem] = []
    expected_set = set(expected_mappings)
    actual_set = set(actual_mappings)

    # Missing expected mappings (iterate over set difference to avoid duplicates)
    for req_src, req_tag in sorted(expected_set - actual_set):
        current_for_src = [
            (src, tag) for src, tag in actual_mappings if src == req_src
        ]
        items.append(
            DriftItem(
                key=f'share_mapping:{req_tag or "unnamed"}',
                expected=(req_src, req_tag),
                actual=current_for_src if current_for_src else [],
                reason=reason_missing,
            )
        )

    # Unexpected extra mappings (in VM but not in expected mappings)
    for src, tag in sorted(actual_set - expected_set):
        items.append(
            DriftItem(
                key=f'share_mapping:extra:{tag or "unnamed"}',
                expected=None,
                actual=(src, tag),
                reason=reason_extra,
            )
        )

    return items


def vm_config_drift_report(
    cfg: AgentVMConfig,
    *,
    use_sudo: bool,
    expected_mappings: list[tuple[str, str]] | None = None,
) -> DriftReport:
    """Compute a combined drift report for VM config (hardware + share mappings).

    This is the main entry point for getting a complete drift picture.
    It combines hardware drift and share mapping drift into a single report.

    Args:
        cfg: The VM configuration.
        use_sudo: Whether to run virsh commands with sudo.
        expected_mappings: Optional list of expected (source, tag) mappings.
            If provided, these are used instead of computing from attachments.

    Returns:
        A DriftReport with combined drift items.
    """
    # Get hardware drift
    hw_report = hardware_drift_report(cfg, use_sudo=use_sudo)
    if not hw_report.available:
        return hw_report

    # Get share mapping drift
    if expected_mappings is not None:
        # Use provided expected mappings
        mappings, error = read_actual_vm_mappings(cfg, use_sudo=use_sudo)
        if mappings is None:
            return DriftReport(
                available=False,
                summary='VM not defined or libvirt query unavailable',
                diag=error,
            )

        items: list[DriftItem] = list(hw_report.items)

        # Use shared helper for two-way set diff
        items.extend(
            _compare_expected_vs_actual_mappings(
                expected_mappings,
                mappings,
                reason_missing='Expected share mapping not present in VM',
                reason_extra='Unexpected share mapping found in VM (not in expected mappings)',
            )
        )
    else:
        # No expected mappings provided, just report hardware
        items = list(hw_report.items)

    if not items:
        return DriftReport(
            available=True,
            summary='VM config matches libvirt',
            items=(),
        )

    return DriftReport(
        available=True,
        summary=f'{len(items)} drift item(s) detected',
        items=tuple(items),
    )


def desired_saved_vm_mappings(
    cfg: AgentVMConfig, reg: Store
) -> list[tuple[str, str]]:
    """Derive expected VM share mappings from saved attachments in config store.

    This helper centralizes the logic for computing what share mappings a VM
    should have based on its saved attachment records.

    Args:
        cfg: The VM configuration.
        reg: The loaded config store/registry.

    Returns:
        A sorted list of unique (source, tag) tuples representing expected mappings.
    """
    desired: set[tuple[str, str]] = set()
    for att in find_attachments_for_vm(reg, cfg.vm.name):
        mode = att.mode
        if mode in ('shared', 'shared-root'):
            if mode == 'shared':
                # For shared mode, use host_path (the store field) and tag from attachment
                src = att.host_path
                tag = att.tag
                if src:  # Only add non-empty sources
                    desired.add((src, tag))
            elif mode == 'shared-root':
                # For shared-root mode, use canonical path and tag
                desired.add(
                    (str(_shared_root_host_dir(cfg)), SHARED_ROOT_VIRTIOFS_TAG)
                )
    # Return sorted list for stable ordering
    return sorted(desired, key=lambda x: (x[0], x[1]))


def saved_vm_drift_report(
    cfg: AgentVMConfig,
    reg: Store,
    *,
    use_sudo: bool,
) -> DriftReport:
    """Compute drift report comparing saved VM config against actual libvirt state.

    This is the preferred entry point for status and other callers that want to
    check whether saved VM configuration matches the actual libvirt state.
    It includes both hardware drift and share mapping drift.

    Args:
        cfg: The VM configuration.
        reg: The loaded config store/registry.
        use_sudo: Whether to run virsh commands with sudo.

    Returns:
        A DriftReport with drift items if available.
    """
    # Derive expected mappings from saved attachments and delegate to the main entry point
    expected_mappings = desired_saved_vm_mappings(cfg, reg)
    return vm_config_drift_report(
        cfg,
        use_sudo=use_sudo,
        expected_mappings=expected_mappings,
    )
