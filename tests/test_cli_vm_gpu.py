"""Focused tests for GPU passthrough selector, host prep, and stable attach."""

from __future__ import annotations

from pathlib import Path

import pytest

from aivm.cli.host import HostModalCLI, HostPCICheckCLI
from aivm.cli.vm import (
    GPUAttachCLI,
    GPUDetachCLI,
    _choose_gpu_attach_strategy,
    _render_gpu_drift_report,
)
from aivm.commands import CommandManager
from aivm.config import AgentVMConfig, load as load_cfg, save as save_cfg
from aivm.host_pci import (
    apply_vfio_boot_prep,
    remove_vfio_boot_prep,
    render_initramfs_bind_script,
    render_modules_load_conf,
)
from aivm.pci import GPUCandidate, PCIDevice, _is_companion_device, resolve_gpu_selector
from aivm.store import Store, load_store, save_store, upsert_vm
from aivm.vm.hostdev import compute_hostdev_drift, render_hostdev_xml
from aivm.vm.lifecycle import create_or_start_vm


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


def _activate_manager() -> None:
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))


def test_passthrough_config_and_store_roundtrip(tmp_path: Path) -> None:
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True
    cfg.passthrough.persistent_hostdev_applied = False
    cfg.passthrough.selector_label = 'NVIDIA GeForce RTX 3090'
    cfg_path = tmp_path / '.aivm.toml'
    save_cfg(cfg_path, cfg)
    loaded_cfg = load_cfg(cfg_path)
    assert loaded_cfg.passthrough.host_prepare_mode == 'vfio-boot'
    assert loaded_cfg.passthrough.host_prepare_applied is True
    assert loaded_cfg.passthrough.selector_label == 'NVIDIA GeForce RTX 3090'

    store_path = tmp_path / 'config.toml'
    _write_store(store_path, cfg)
    loaded_store = load_store(store_path)
    assert loaded_store.vms[0].cfg.passthrough.host_prepare_applied is True


def test_host_cli_modal_wiring() -> None:
    assert HostModalCLI.pci.check is HostPCICheckCLI


def test_render_hostdev_xml_only_uses_source_pci_addresses() -> None:
    xml = render_hostdev_xml(['0000:65:00.0'])
    assert "domain='0x0000'" in xml or 'domain="0x0000"' in xml
    assert '<address type=' not in xml


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


def test_numeric_bdf_and_substring_selectors(monkeypatch) -> None:
    monkeypatch.setattr('aivm.pci.list_gpu_candidates', _fake_candidates)
    assert resolve_gpu_selector('1', interactive=False).primary_bdf == '0000:b3:00.0'
    assert (
        resolve_gpu_selector('0000:65:00.0', interactive=False).primary_bdf
        == '0000:65:00.0'
    )
    assert (
        resolve_gpu_selector('RTX 4090', interactive=False).primary_bdf
        == '0000:b3:00.0'
    )


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


def test_strategy_prompt_options(monkeypatch) -> None:
    cfg = _make_cfg()
    candidate = _fake_candidates()[0]
    companions = [
        PCIDevice(bdf='0000:65:00.0', nodedev_name='n0'),
        PCIDevice(bdf='0000:65:00.1', nodedev_name='n1'),
    ]
    for raw, expected in [('1', 'stable'), ('2', 'hotplug'), ('3', 'record-only'), ('4', 'cancel')]:
        monkeypatch.setattr('builtins.input', lambda prompt='', raw=raw: raw)
        assert (
            _choose_gpu_attach_strategy(
                cfg=cfg,
                candidate=candidate,
                companions=companions,
                interactive=True,
                auto_yes=False,
            )
            == expected
        )


def test_hotplug_path_raises_not_implemented(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector', lambda *a, **k: _fake_candidates()[0]
    )
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type('Report', (), {'issues': ('One or more passthrough devices are not bound to vfio-pci: 0000:65:00.0',), 'status': 'manual_steps_required'})(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [
                PCIDevice(bdf='0000:65:00.0', nodedev_name='n0', driver='nvidia'),
                PCIDevice(bdf='0000:65:00.1', nodedev_name='n1', driver='snd_hda_intel'),
            ],
            [],
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt='': '2')
    _activate_manager()
    with pytest.raises(NotImplementedError, match='Stable boot-time attachment'):
        GPUAttachCLI.main(argv=False, config=str(cfg_path), vm=cfg.vm.name)


def test_stable_path_with_non_vfio_gpu_records_intent_and_can_prepare_host(
    monkeypatch, tmp_path: Path
) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    prepared: list[list[str]] = []

    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector', lambda *a, **k: _fake_candidates()[0]
    )
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type(
            'Report',
            (),
            {
                'issues': ('One or more passthrough devices are not bound to vfio-pci: 0000:65:00.0',),
                'status': 'manual_steps_required',
            },
        )(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: (
            [
                PCIDevice(bdf='0000:65:00.0', nodedev_name='n0', driver='nvidia'),
                PCIDevice(bdf='0000:65:00.1', nodedev_name='n1', driver='snd_hda_intel'),
            ],
            [],
        ),
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    answers = iter(['1', 'y'])
    monkeypatch.setattr('builtins.input', lambda prompt='': next(answers))
    monkeypatch.setattr(
        'aivm.cli.vm.apply_vfio_boot_prep',
        lambda vm_name, bdfs: prepared.append(list(bdfs)),
    )
    _activate_manager()

    assert GPUAttachCLI.main(argv=False, config=str(cfg_path), vm=cfg.vm.name) == 0
    stored = load_store(cfg_path).vms[0].cfg
    assert stored.passthrough.pci_devices == ['0000:65:00.0', '0000:65:00.1']
    assert stored.passthrough.host_prepare_mode == 'vfio-boot'
    assert stored.passthrough.host_prepare_applied is True
    assert stored.passthrough.persistent_hostdev_applied is False
    assert prepared == [['0000:65:00.0', '0000:65:00.1']]


def test_record_only_strategy_makes_no_host_changes(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _write_store(cfg_path)
    monkeypatch.setattr(
        'aivm.cli.vm._load_cfg_with_path', lambda *a, **k: (cfg, cfg_path)
    )
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_gpu_selector', lambda *a, **k: _fake_candidates()[0]
    )
    monkeypatch.setattr(
        'aivm.cli.vm.assess_device_readiness',
        lambda bdf: type('Report', (), {'issues': (), 'status': 'ready_persistent_restart'})(),
    )
    monkeypatch.setattr('aivm.cli.vm.render_readiness_report', lambda report: '')
    monkeypatch.setattr(
        'aivm.cli.vm.resolve_passthrough_set_for_gpu',
        lambda bdf: ([PCIDevice(bdf='0000:65:00.0', nodedev_name='n0', driver='vfio-pci')], []),
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt='': '3')
    called = {'host_prep': 0}
    monkeypatch.setattr(
        'aivm.cli.vm.apply_vfio_boot_prep',
        lambda *a, **k: called.__setitem__('host_prep', called['host_prep'] + 1),
    )
    _activate_manager()
    assert GPUAttachCLI.main(argv=False, config=str(cfg_path), vm=cfg.vm.name) == 0
    assert called['host_prep'] == 0
    stored = load_store(cfg_path).vms[0].cfg
    assert stored.passthrough.host_prepare_mode == 'none'
    assert stored.passthrough.host_prepare_applied is False


def test_host_prep_helpers_render_expected_content() -> None:
    modules = render_modules_load_conf('gpu-vm', ['0000:65:00.0', '0000:65:00.1'])
    script = render_initramfs_bind_script('gpu-vm', ['0000:65:00.0', '0000:65:00.1'])
    assert 'Managed by aivm for VM gpu-vm' in modules
    assert 'vfio-pci' in modules
    assert '0000:65:00.0' in script
    assert 'driver_override' in script


def test_host_prep_apply_and_remove_use_expected_commands(monkeypatch) -> None:
    calls: list[list[str]] = []
    _activate_manager()

    class Proc:
        def __init__(self, returncode=0, stdout='', stderr=''):
            self.returncode = returncode
            self.stdout = stdout
            self.stderr = stderr

    monkeypatch.setattr('aivm.commands.os.geteuid', lambda: 0)
    monkeypatch.setattr(
        'aivm.commands.subprocess.run',
        lambda cmd, **kwargs: calls.append(list(cmd)) or Proc(0, '', ''),
    )
    apply_vfio_boot_prep('gpu-vm', ['0000:65:00.0'])
    remove_vfio_boot_prep('gpu-vm')
    assert any(cmd[:2] == ['install', '-D'] for cmd in calls)
    assert any(cmd[:1] == ['update-initramfs'] for cmd in calls)
    assert any(cmd[:2] == ['rm', '-f'] for cmd in calls)


def test_detach_undo_clears_intent_and_host_prep(monkeypatch, tmp_path: Path) -> None:
    cfg_path = tmp_path / 'config.toml'
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True
    cfg.passthrough.persistent_hostdev_applied = True
    _write_store(cfg_path, cfg)
    removed: list[str] = []
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
        'aivm.cli.vm.remove_vfio_boot_prep',
        lambda vm_name: removed.append(vm_name),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.detach_hostdev_persistent',
        lambda vm_name, bdfs: detached.append(list(bdfs)) or list(bdfs),
    )
    monkeypatch.setattr('aivm.cli.vm.vm_is_running', lambda vm_name: False)
    _activate_manager()

    assert GPUDetachCLI.main(
        argv=False, config=str(cfg_path), vm=cfg.vm.name, bdf='0000:65:00.0'
    ) == 0
    stored = load_store(cfg_path).vms[0].cfg
    assert stored.passthrough.pci_devices == []
    assert stored.passthrough.host_prepare_mode == 'none'
    assert stored.passthrough.host_prepare_applied is False
    assert removed == ['gpu-vm']
    assert detached == [['0000:65:00.0', '0000:65:00.1']]


def test_vm_start_can_apply_declared_passthrough_after_host_prep(monkeypatch) -> None:
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True
    ensured: list[list[str]] = []
    started: list[list[str]] = []

    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *_a, **_k: True)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.ensure_hostdev_persistent',
        lambda vm_name, bdfs: ensured.append(list(bdfs)) or list(bdfs),
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle.assess_device_readiness',
        lambda bdf: type(
            'Report',
            (),
            {
                'status': 'ready_persistent_restart',
                'primary': type(
                    'Primary', (), {'class_code': '0x030000' if bdf.endswith('.0') else '0x040300'}
                )(),
            },
        )(),
    )
    def fake_run_cmd(cmd, **kwargs):
        started.append(list(cmd))
        if cmd[:2] == ['virsh', 'domstate']:
            return type(
                'Result', (), {'code': 0, 'stdout': 'shut off\n', 'stderr': ''}
            )()
        return type('Result', (), {'code': 0, 'stdout': '', 'stderr': ''})()

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    _activate_manager()
    create_or_start_vm(cfg, dry_run=False, recreate=False)
    assert ensured == [['0000:65:00.0', '0000:65:00.1']]
    assert any(cmd[:2] == ['virsh', 'start'] for cmd in started)


def test_vm_start_blocks_when_declared_gpu_readiness_is_bad(monkeypatch) -> None:
    cfg = _make_cfg()
    cfg.passthrough.pci_devices = ['0000:65:00.0', '0000:65:00.1']
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True

    monkeypatch.setattr('aivm.vm.lifecycle.vm_exists', lambda *_a, **_k: True)
    monkeypatch.setattr(
        'aivm.vm.lifecycle.ensure_hostdev_persistent',
        lambda vm_name, bdfs: list(bdfs),
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle.assess_device_readiness',
        lambda bdf: type(
            'Report',
            (),
            {
                'status': 'manual_steps_required',
                'primary': type('Primary', (), {'class_code': '0x030000'})(),
            },
        )(),
    )
    monkeypatch.setattr(
        'aivm.vm.lifecycle.render_readiness_report',
        lambda report: 'PCI device: 0000:65:00.0\nBlocking issues:\n  - not bound to vfio-pci',
    )

    def fake_run_cmd(cmd, **kwargs):
        if cmd[:2] == ['virsh', 'domstate']:
            return type(
                'Result', (), {'code': 0, 'stdout': 'shut off\n', 'stderr': ''}
            )()
        return type('Result', (), {'code': 0, 'stdout': '', 'stderr': ''})()

    monkeypatch.setattr('aivm.vm.lifecycle.run_cmd', fake_run_cmd)
    _activate_manager()
    with pytest.raises(RuntimeError, match='Declared GPU passthrough is not ready'):
        create_or_start_vm(cfg, dry_run=False, recreate=False)


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
    cfg = _make_cfg('vmx')
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True
    drift = compute_hostdev_drift(
        declared=['0000:65:00.0'],
        persistent=['0000:65:00.1'],
        live=['0000:65:00.2'],
    )
    text = _render_gpu_drift_report(cfg, drift)
    assert 'Host prepare mode: vfio-boot' in text
    assert 'Persistent hostdev applied: no' in text
    assert 'Declared but missing from persistent: 0000:65:00.0' in text
