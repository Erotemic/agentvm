"""Tests for restart-required detection in the `aivm code` flow."""

from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path

from aivm.cli._common import PreparedSession
from aivm.cli.vm import (
    VMCodeCLI,
    _pending_passthrough_requirement,
    _restart_requirement_prompt_text,
    _restart_vm_for_code_open,
)
from aivm.commands import CommandError, CommandManager, CommandResult
from aivm.config import AgentVMConfig
from aivm.vm.hostdev import restart_required_for_code_open


def _activate_manager() -> None:
    CommandManager.activate(CommandManager(yes=True, yes_sudo=True))


def _session(tmp_path: Path) -> PreparedSession:
    cfg = AgentVMConfig()
    cfg.vm.name = 'gpu-vm'
    return PreparedSession(
        cfg=cfg,
        cfg_path=tmp_path / 'config.toml',
        host_src=tmp_path,
        attachment_mode='shared',
        share_source_dir=str(tmp_path),
        share_tag='tag',
        share_guest_dst='/workspace/project',
        ip='10.0.0.2',
        reg_path=tmp_path / 'config.toml',
        meta_path=None,
    )


def test_restart_required_when_persistent_hostdev_missing_from_live(monkeypatch) -> None:
    monkeypatch.setattr('aivm.vm.hostdev.vm_is_running', lambda vm_name: True)
    monkeypatch.setattr(
        'aivm.vm.hostdev.domain_hostdevs_persistent',
        lambda vm_name: ['0000:65:00.0'],
    )
    monkeypatch.setattr('aivm.vm.hostdev.domain_hostdevs_live', lambda vm_name: [])
    requirement = restart_required_for_code_open('gpu-vm')
    assert requirement.required is True
    assert 'missing persistent PCI hostdevs' in requirement.reasons[0]


def test_restart_not_required_when_live_and_persistent_match(monkeypatch) -> None:
    monkeypatch.setattr('aivm.vm.hostdev.vm_is_running', lambda vm_name: True)
    monkeypatch.setattr(
        'aivm.vm.hostdev.domain_hostdevs_persistent',
        lambda vm_name: ['0000:65:00.0'],
    )
    monkeypatch.setattr(
        'aivm.vm.hostdev.domain_hostdevs_live',
        lambda vm_name: ['0000:65:00.0'],
    )
    requirement = restart_required_for_code_open('gpu-vm')
    assert requirement.required is False


def test_restart_not_required_when_vm_stopped(monkeypatch) -> None:
    monkeypatch.setattr('aivm.vm.hostdev.vm_is_running', lambda vm_name: False)
    requirement = restart_required_for_code_open('gpu-vm')
    assert requirement.required is False


def test_pending_passthrough_requirement_when_declared_not_applied() -> None:
    cfg = AgentVMConfig()
    cfg.vm.name = 'gpu-vm'
    cfg.passthrough.pci_devices = ['0000:03:00.0', '0000:03:00.1']
    cfg.passthrough.host_prepare_mode = 'vfio-boot'
    cfg.passthrough.host_prepare_applied = True
    cfg.passthrough.persistent_hostdev_applied = False
    requirement = _pending_passthrough_requirement(cfg)
    assert requirement.required is True
    assert 'persistent VM definition' in requirement.reasons[0]


def test_restart_prompt_question_is_final_line() -> None:
    prompt = _restart_requirement_prompt_text(
        type(
            'Requirement',
            (),
            {
                'reasons': (
                    'Declared GPU passthrough has not been written into the persistent VM definition yet: 0000:03:00.0',
                ),
            },
        )()
    )
    lines = prompt.splitlines()
    assert lines[-1] == 'Restart the VM now and continue opening VS Code? [y/N]'
    assert lines[-2] == ''


def test_code_open_decline_restart_warns_and_still_launches(
    monkeypatch, tmp_path: Path, capsys
) -> None:
    session = _session(tmp_path)
    launched: list[list[str]] = []
    restarted: list[str] = []
    _activate_manager()

    monkeypatch.setattr(
        'aivm.cli.vm._prepare_attached_session', lambda **kwargs: session
    )
    monkeypatch.setattr(
        'aivm.cli.vm.restart_required_for_code_open',
        lambda vm_name: type(
            'Requirement',
            (),
            {
                'required': True,
                'reasons': ('Live guest is missing persistent PCI hostdevs: 0000:65:00.0',),
                'summary': 'restart required',
            },
        )(),
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt='': 'n')
    monkeypatch.setattr('aivm.cli.vm.which', lambda cmd: '/usr/bin/code')
    monkeypatch.setattr(
        'aivm.cli.vm._upsert_ssh_config_entry',
        lambda *a, **k: (Path('/tmp/ssh-config'), False),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._restart_vm_for_code_open',
        lambda *a, **k: restarted.append('restart') or session,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.run_cmd',
        lambda cmd, **kwargs: launched.append(list(cmd))
        or type('Result', (), {'code': 0, 'stdout': '', 'stderr': ''})(),
    )

    assert VMCodeCLI.main(argv=False, host_src=str(tmp_path), yes=False) == 0
    text = capsys.readouterr().out
    assert 'Pending VM configuration changes are not active until the domain is cold-restarted' in text
    assert restarted == []
    assert launched and launched[0][0] == 'code'


def test_code_open_accept_restart_invokes_restart_before_code(
    monkeypatch, tmp_path: Path
) -> None:
    session = _session(tmp_path)
    events: list[str] = []
    _activate_manager()

    monkeypatch.setattr(
        'aivm.cli.vm._prepare_attached_session', lambda **kwargs: session
    )
    monkeypatch.setattr(
        'aivm.cli.vm.restart_required_for_code_open',
        lambda vm_name: type(
            'Requirement',
            (),
            {'required': True, 'reasons': ('hostdev drift',), 'summary': 'restart required'},
        )(),
    )
    monkeypatch.setattr('aivm.cli.vm.sys.stdin.isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda prompt='': 'y')
    monkeypatch.setattr('aivm.cli.vm.which', lambda cmd: '/usr/bin/code')
    monkeypatch.setattr(
        'aivm.cli.vm._upsert_ssh_config_entry',
        lambda *a, **k: (Path('/tmp/ssh-config'), False),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._restart_vm_for_code_open',
        lambda current, **kwargs: events.append('restart') or session,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.run_cmd',
        lambda cmd, **kwargs: events.append('code')
        or type('Result', (), {'code': 0, 'stdout': '', 'stderr': ''})(),
    )

    assert VMCodeCLI.main(argv=False, host_src=str(tmp_path), yes=False) == 0
    assert events == ['restart', 'code']


def test_restart_path_applies_declared_passthrough_before_reboot(
    monkeypatch, tmp_path: Path
) -> None:
    session = _session(tmp_path)
    session.cfg.passthrough.pci_devices = ['0000:03:00.0', '0000:03:00.1']
    session.cfg.passthrough.host_prepare_mode = 'vfio-boot'
    session.cfg.passthrough.host_prepare_applied = True
    session.cfg.passthrough.persistent_hostdev_applied = False
    events: list[str] = []
    _activate_manager()

    monkeypatch.setattr(
        'aivm.cli.vm._confirm_sudo_block',
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._maybe_prepare_declared_gpu_hostdevs',
        lambda cfg, dry_run=False: events.append('apply-persistent'),
    )
    monkeypatch.setattr(
        'aivm.cli.vm._record_vm',
        lambda cfg, cfg_path=None: events.append('record-vm') or tmp_path / 'config.toml',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.IntentScope',
        lambda *args, **kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.PlanScope',
        lambda *args, **kwargs: nullcontext(),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.virsh_system_cmd',
        lambda *args: list(args),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.vm_is_running',
        lambda vm_name: False,
    )
    monkeypatch.setattr(
        'aivm.cli.vm.wait_for_ip',
        lambda cfg, timeout_s=0, dry_run=False: '10.0.0.9',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.wait_for_ssh',
        lambda cfg, ip, timeout_s=0, dry_run=False: None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *args, **kwargs: type(
            'Attachment',
            (),
            {
                'mode': 'git',
                'source_dir': str(tmp_path),
                'tag': 'tag',
                'guest_dst': '/workspace/project',
            },
        )(),
    )
    monkeypatch.setattr(
        'aivm.cli.vm.CommandManager.current',
        lambda: type(
            'Mgr',
            (),
            {
                'submit': lambda self, *a, **k: type(
                    'Submission',
                    (),
                    {
                        'result': lambda self: type(
                            'Result',
                            (),
                            {'code': 0, 'stdout': '', 'stderr': ''},
                        )()
                    },
                )()
            },
        )(),
    )

    updated = _restart_vm_for_code_open(session, yes=True)

    assert events[:2] == ['apply-persistent', 'record-vm']
    assert updated.ip == '10.0.0.9'
    assert session.cfg.passthrough.persistent_hostdev_applied is True


def test_restart_path_tolerates_domain_already_active_start_race(
    monkeypatch, tmp_path: Path
) -> None:
    session = _session(tmp_path)
    _activate_manager()
    submit_calls: list[list[str]] = []
    active_states = iter([False, False, True])

    monkeypatch.setattr('aivm.cli.vm._confirm_sudo_block', lambda **kwargs: None)
    monkeypatch.setattr('aivm.cli.vm.IntentScope', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr('aivm.cli.vm.PlanScope', lambda *args, **kwargs: nullcontext())
    monkeypatch.setattr('aivm.cli.vm.virsh_system_cmd', lambda *args: list(args))
    monkeypatch.setattr('aivm.cli.vm.vm_is_active', lambda vm_name: next(active_states))
    monkeypatch.setattr('aivm.cli.vm.vm_domstate', lambda vm_name: 'shut off')
    monkeypatch.setattr(
        'aivm.cli.vm.wait_for_ip',
        lambda cfg, timeout_s=0, dry_run=False: '10.0.0.9',
    )
    monkeypatch.setattr(
        'aivm.cli.vm.wait_for_ssh',
        lambda cfg, ip, timeout_s=0, dry_run=False: None,
    )
    monkeypatch.setattr(
        'aivm.cli.vm._resolve_attachment',
        lambda *args, **kwargs: type(
            'Attachment',
            (),
            {
                'mode': 'git',
                'source_dir': str(tmp_path),
                'tag': 'tag',
                'guest_dst': '/workspace/project',
            },
        )(),
    )

    class _Mgr:
        def submit(self, cmd, **kwargs):
            submit_calls.append(list(cmd))
            return type(
                'Submission',
                (),
                {
                    'result': lambda self: type(
                        'Result',
                        (),
                        {
                            'code': 1 if cmd and cmd[0] == 'start' else 0,
                            'stdout': '',
                            'stderr': (
                                'error: Domain is already active'
                                if cmd and cmd[0] == 'start'
                                else ''
                            ),
                        },
                    )()
                },
            )()

    monkeypatch.setattr('aivm.cli.vm.CommandManager.current', lambda: _Mgr())

    updated = _restart_vm_for_code_open(session, yes=True)

    assert ['shutdown', 'gpu-vm'] in submit_calls
    assert ['start', 'gpu-vm'] in submit_calls
    assert updated.ip == '10.0.0.9'
