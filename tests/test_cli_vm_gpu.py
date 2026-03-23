"""Focused tests for the first-pass GPU passthrough CLI and helpers."""

from __future__ import annotations

from pathlib import Path

import pytest

from aivm.cli.host import HostModalCLI, HostPCICheckCLI
from aivm.cli.vm import GPUAttachCLI, GPUDetachCLI, _render_gpu_drift_report
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig, load as load_cfg, save as save_cfg
from aivm.pci import (
    GPUCandidate,
    PCIDevice,
    _is_companion_device,
    resolve_gpu_selector,
)
from aivm.store import Store, load_store, save_store, upsert_vm
from aivm.vm.hostdev import (
    compute_hostdev_drift,
    ensure_hostdev_persistent,
    render_hostdev_xml,
)


def _make_cfg(vm_name: str = 'gpu-vm') -> AgentVMConfig:
    cfg = AgentVMConfig()
    cfg.vm.name = vm_name
    cfg.network.name = 'gpu-net'
    return cfg


def _write_store(path: Path, cfg: AgentVMConfig | None = None) -> AgentVMConfig:
    cfg = cfg or _make_cfg()
    store = Store()
    upsert_vm(store, cfg)
    save_store(store, path)
    return cfg


def test_passthrough_config_and_store_roundtrip(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg_path = tmp_path / '.aivm.toml'
    save_cfg(cfg_path, cfg)
    loaded_cfg = load_cfg(cfg_path)
    assert loaded_cfg.passthrough.pci_devices == cfg.passthrough.pci_devices

    store_path = tmp_path / 'config.toml'
    _write_store(store_path, cfg)
    loaded_store = load_store(store_path)
    rec = loaded_store.vms[0]
    assert rec.cfg.passthrough.pci_devices == cfg.passthrough.pci_devices


def test_host_cli_modal_wiring() -> None:
    assert HostModalCLI.pci.check is HostPCICheckCLI


def test_render_hostdev_xml_only_uses_source_pci_addresses() -> None:
    xml = render_hostdev_xml(['0000:65:00.0'])
    assert "domain='0x0000'" in xml or 'domain="0x0000"' in xml
    assert "slot='0x00'" in xml or 'slot="0x00"' in xml
    assert '<address type=' not in xml


def test_attach_unpacks_companion_tuple_semantics(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    applied: list[list[str]] = []
    seen_bdfs: list[str] = []

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector',
        lambda *a, **k: GPUCandidate(
            index=0,
            name='NVIDIA RTX 3090',
            primary_bdf='0000:65:00.0',
            companion_bdfs=('0000:65:00.1',),
            driver='vfio-pci',
            readiness_status='ready_persistent_restart',
            summary='gpu0',
        ),
    )
    def fake_assess(bdf):
        seen_bdfs.append(bdf)
        return type(
            'Report',
            (),
            {
                'status': 'ready_persistent_restart',
                'bdf': bdf,
                'primary': None,
                'companions': (),
                'unexpected': (),
                'issues': (),
                'recommendations': (),
                'iommu_enabled': True,
            },
        )()

    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        fake_assess,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.render_readiness_report', lambda report: report.status
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [
                PCIDevice(bdf='0000:65:00.0', nodedev_name='n0'),
                PCIDevice(bdf='0000:65:00.1', nodedev_name='n1'),
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_hostdev_persistent',
        lambda vm_name, bdfs: applied.append(list(bdfs)) or list(bdfs),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    assert GPUAttachCLI.main(
        argv=False, config=str(cfg_path), vm=cfg.vm.name, selector='0'
    ) == 0
    assert seen_bdfs == ['0000:65:00.0']
    assert applied == [['0000:65:00.0', '0000:65:00.1']]
    mutated = load_store(cfg_path)
    assert mutated.vms[0].cfg.passthrough.pci_devices == [
        '0000:65:00.0',
        '0000:65:00.1',
    ]


def test_attach_uses_resolved_cfg_path(monkeypatch, tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg_path = tmp_path / 'custom.toml'
    seen: list[Path] = []
    store = Store()
    upsert_vm(store, cfg)

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector',
        lambda *a, **k: GPUCandidate(
            index=0,
            name='NVIDIA RTX 3090',
            primary_bdf='0000:65:00.0',
            companion_bdfs=(),
            driver='vfio-pci',
            readiness_status='ready_persistent_restart',
            summary='gpu0',
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type('Report', (), {'status': 'ready_persistent_restart'})(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: ([PCIDevice(bdf='0000:65:00.0', nodedev_name='n0')], []),
    )
    monkeypatch.setattr('aivm.cli.vm.ensure_hostdev_persistent', lambda *a, **k: [])
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    monkeypatch.setattr(
        'aivm.cli.vm.load_store', lambda path=None: seen.append(path) or store
    )
    monkeypatch.setattr('aivm.cli.vm.save_store', lambda reg, path=None: path)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    GPUAttachCLI.main(
        argv=False, config=str(cfg_path), vm=cfg.vm.name, selector='0'
    )
    assert cfg_path in seen


def test_detach_uses_resolved_cfg_path(monkeypatch, tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg_path = tmp_path / 'custom.toml'
    seen: list[Path] = []
    store = Store()
    upsert_vm(store, cfg)

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [
                PCIDevice(bdf='0000:65:00.0', nodedev_name='n0'),
                PCIDevice(bdf='0000:65:00.1', nodedev_name='n1'),
            ],
            [],
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.detach_hostdev_persistent', lambda *a, **k: [])
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    monkeypatch.setattr(
        'aivm.cli.vm.load_store', lambda path=None: seen.append(path) or store
    )
    monkeypatch.setattr('aivm.cli.vm.save_store', lambda reg, path=None: path)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    GPUDetachCLI.main(
        argv=False, config=str(cfg_path), vm=cfg.vm.name, bdf='0000:65:00.0'
    )
    assert cfg_path in seen


def test_detach_uses_full_resolved_set(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    _write_store(cfg_path, cfg)
    detached: list[list[str]] = []

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [
                PCIDevice(bdf='0000:65:00.0', nodedev_name='n0'),
                PCIDevice(bdf='0000:65:00.1', nodedev_name='n1'),
            ],
            [],
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.detach_hostdev_persistent',
        lambda vm_name, bdfs: detached.append(list(bdfs)) or list(bdfs),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    GPUDetachCLI.main(
        argv=False, config=str(cfg_path), vm=cfg.vm.name, bdf='0000:65:00.0'
    )
    assert detached == [['0000:65:00.0', '0000:65:00.1']]
    mutated = load_store(cfg_path)
    assert mutated.vms[0].cfg.passthrough.pci_devices == []


def test_unexpected_devices_block_attach_and_detach(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    saved_before = cfg_path.read_text(encoding='utf-8')
    hostdev_calls: list[str] = []

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector',
        lambda *a, **k: GPUCandidate(
            index=0,
            name='NVIDIA RTX 3090',
            primary_bdf='0000:65:00.0',
            companion_bdfs=(),
            driver='vfio-pci',
            readiness_status='ready_persistent_restart',
            summary='gpu0',
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type(
            'Report',
            (),
            {
                'status': 'ready_persistent_restart',
                'bdf': bdf,
                'primary': None,
                'companions': (),
                'unexpected': (),
                'issues': (),
                'recommendations': (),
                'iommu_enabled': True,
            },
        )(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [PCIDevice(bdf='0000:65:00.0', nodedev_name='n0')],
            [PCIDevice(bdf='0000:65:00.2', nodedev_name='n2')],
        ),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_hostdev_persistent',
        lambda *a, **k: hostdev_calls.append('attach') or [],
    )
    monkeypatch.setattr(
        'aivm.cli.vm.detach_hostdev_persistent',
        lambda *a, **k: hostdev_calls.append('detach') or [],
    )
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    with pytest.raises(RuntimeError, match='unexpected devices'):
        GPUAttachCLI.main(
            argv=False, config=str(cfg_path), vm=cfg.vm.name
        )
    with pytest.raises(RuntimeError, match='unexpected devices'):
        GPUDetachCLI.main(
            argv=False, config=str(cfg_path), vm=cfg.vm.name, bdf='0000:65:00.0'
        )
    assert hostdev_calls == []
    assert cfg_path.read_text(encoding='utf-8') == saved_before


def test_hostdev_helper_uses_code_not_returncode(monkeypatch) -> None:
    class Result:
        code = 0
        stdout = "<domain><devices/></domain>"
        stderr = ''

        @property
        def returncode(self):  # pragma: no cover - should never be touched
            raise AssertionError('returncode should not be used')

    class Handle:
        def result(self):
            return Result()

    class FakeManager:
        def submit(self, *args, **kwargs):
            return Handle()

    monkeypatch.setattr('aivm.vm.hostdev.CommandManager.current', lambda: FakeManager())
    assert ensure_hostdev_persistent('vmx', ['0000:65:00.0']) == ['0000:65:00.0']


def test_companion_detection_is_conservative() -> None:
    gpu = PCIDevice(
        bdf='0000:65:00.0',
        nodedev_name='n0',
        class_code='0x030000',
        vendor_id='0x10de',
        vendor_name='NVIDIA',
        product_name='RTX',
    )
    arbitrary_audio = PCIDevice(
        bdf='0000:65:00.1',
        nodedev_name='n1',
        class_code='0x040300',
        vendor_id='0x10de',
        vendor_name='NVIDIA',
        product_name='Audio endpoint',
    )
    hdmi_audio = PCIDevice(
        bdf='0000:65:00.1',
        nodedev_name='n1',
        class_code='0x040300',
        vendor_id='0x10de',
        vendor_name='NVIDIA',
        product_name='High Definition Audio Controller',
    )
    assert _is_companion_device(gpu, arbitrary_audio) is False
    assert _is_companion_device(gpu, hdmi_audio) is True


def test_drift_reporting_distinguishes_declared_persistent_and_live() -> None:
    drift = compute_hostdev_drift(
        declared=['0000:65:00.0'],
        persistent=['0000:65:00.1'],
        live=['0000:65:00.2'],
    )
    text = _render_gpu_drift_report('vmx', drift)
    assert 'Declared passthrough devices:' in text
    assert 'Persistent libvirt hostdevs:' in text
    assert 'Live libvirt hostdevs:' in text
    assert 'Declared but missing from persistent: 0000:65:00.0' in text
    assert 'Persistent but not declared: 0000:65:00.1' in text
    assert 'Live-only hostdevs: 0000:65:00.2' in text


def _fake_candidates() -> list[GPUCandidate]:
    return [
        GPUCandidate(
            index=0,
            name='NVIDIA GeForce RTX 3090',
            primary_bdf='0000:65:00.0',
            companion_bdfs=('0000:65:00.1',),
            driver='nvidia',
            readiness_status='manual_steps_required',
            summary='gpu0',
        ),
        GPUCandidate(
            index=1,
            name='NVIDIA GeForce RTX 4090',
            primary_bdf='0000:b3:00.0',
            companion_bdfs=('0000:b3:00.1',),
            driver='vfio-pci',
            readiness_status='ready_persistent_restart',
            summary='gpu1',
        ),
    ]


def test_missing_selector_interactive_prompts_for_selection(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    monkeypatch.setattr('builtins.input', lambda prompt='': '1')
    chosen = resolve_gpu_selector(None, interactive=True, vm_name='myvm')
    assert chosen.primary_bdf == '0000:b3:00.0'


def test_missing_selector_noninteractive_lists_options(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    with pytest.raises(RuntimeError, match='GPU selector is required') as ex:
        resolve_gpu_selector(None, interactive=False, vm_name='myvm')
    text = str(ex.value)
    assert 'Detected GPUs:' in text
    assert 'aivm vm gpu attach 0 --vm myvm' in text
    assert 'aivm vm gpu attach "NVIDIA GeForce RTX 3090" --vm myvm' in text


def test_gpu_attach_missing_selector_interactive_prompts(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    applied: list[list[str]] = []

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    monkeypatch.setattr('builtins.input', lambda prompt='': '1')
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type('Report', (), {'status': 'ready_persistent_restart'})(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: ([PCIDevice(bdf='0000:b3:00.0', nodedev_name='n0')], []),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.ensure_hostdev_persistent',
        lambda vm_name, bdfs: applied.append(list(bdfs)) or list(bdfs),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    assert GPUAttachCLI.main(argv=False, config=str(cfg_path), vm=cfg.vm.name) == 0
    assert applied == [['0000:b3:00.0']]


def test_gpu_attach_missing_selector_noninteractive_fails_helpfully(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: False)
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))

    assert GPUAttachCLI.main(argv=False, config=str(cfg_path), vm=cfg.vm.name) == 1
    text = capsys.readouterr().out
    assert 'Detected GPUs:' in text
    assert 'aivm vm gpu attach 0 --vm gpu-vm' in text


def test_numeric_selector_resolves_by_index(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    chosen = resolve_gpu_selector('1', interactive=False)
    assert chosen.primary_bdf == '0000:b3:00.0'


def test_bdf_selector_still_works(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    chosen = resolve_gpu_selector('0000:65:00.0', interactive=False)
    assert chosen.name == 'NVIDIA GeForce RTX 3090'


def test_unique_substring_selector_works(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    chosen = resolve_gpu_selector('RTX 4090', interactive=False)
    assert chosen.primary_bdf == '0000:b3:00.0'


def test_ambiguous_substring_selector_fails_clearly(monkeypatch) -> None:
    candidates = _fake_candidates()
    candidates[1] = GPUCandidate(
        index=1,
        name='NVIDIA GeForce RTX 3090 Ti',
        primary_bdf='0000:b3:00.0',
        companion_bdfs=('0000:b3:00.1',),
        driver='vfio-pci',
        readiness_status='ready_persistent_restart',
        summary='gpu1',
    )
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', lambda: candidates)
    with pytest.raises(RuntimeError, match='Ambiguous GPU selector'):
        resolve_gpu_selector('RTX 3090', interactive=False, vm_name='myvm')
