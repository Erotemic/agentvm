"""Tests for runtime environment detection surfaced in status output."""

from __future__ import annotations

from aivm.status import (
    ProbeOutcome,
    probe_runtime_environment,
    render_global_status,
)
from aivm.store import Store
from aivm.util import CmdResult


def test_probe_runtime_environment_from_systemd_detect_virt_guest(
    monkeypatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: '/usr/bin/' + cmd)
    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(0, 'kvm\n', ''),
    )
    out = probe_runtime_environment()
    assert out.ok is True
    assert 'virtualized guest' in out.detail
    assert 'kvm' in out.detail


def test_probe_runtime_environment_from_systemd_detect_virt_host(
    monkeypatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: '/usr/bin/' + cmd)
    monkeypatch.setattr(
        'aivm.status.run_cmd',
        lambda *a, **k: CmdResult(0, 'none\n', ''),
    )
    out = probe_runtime_environment()
    assert out.ok is True
    assert 'host system' in out.detail


def test_probe_runtime_environment_cpuinfo_hypervisor_fallback(
    monkeypatch,
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
    monkeypatch,
) -> None:
    monkeypatch.setattr('aivm.status.which', lambda cmd: None)
    monkeypatch.setattr('aivm.status.Path.exists', lambda self: False)
    out = probe_runtime_environment()
    assert out.ok is None
    assert 'unable to determine' in out.detail


def test_render_global_status_includes_runtime_environment(
    monkeypatch,
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
