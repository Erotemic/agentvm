"""CLI help and command-tree rendering utilities."""

from __future__ import annotations

import shlex
import textwrap
from pathlib import Path

import scriptconfig as scfg
import ubelt as ub

from ..store import find_vm, load_store
from ._common import _BaseCommand, _cfg_path


class PlanCLI(_BaseCommand):
    """Show the recommended end-to-end setup command sequence."""

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        path = _cfg_path(args.config)
        default_path = _cfg_path(None)
        cfg_flag = (
            f' --config {shlex.quote(str(path))}'
            if path != default_path
            else ''
        )
        steps = textwrap.dedent(f"""
        🗺️  AgentVM Plan
        📄 Config: {path}

        Suggested flow:

        1. ⚙️ Initialize config store
           aivm config init{cfg_flag}
        2. 🔎 Preflight checks
           aivm host doctor{cfg_flag}
           aivm status{cfg_flag}
           aivm status{cfg_flag} --detail
        3. 🌐 Host network
           aivm host net create{cfg_flag}
        4. 🔥 Optional firewall isolation (recommended)
           aivm host fw apply{cfg_flag}
        5. 📦 Base image
           aivm host image_fetch{cfg_flag}
        6. 🖥️ VM lifecycle
           aivm vm create{cfg_flag}
           aivm vm wait_ip{cfg_flag}
        7. 🔑 Access
           aivm vm ssh_config{cfg_flag}   # VS Code Remote-SSH
        8. 🧰 Optional provisioning (docker + dev tools)
           aivm vm provision{cfg_flag}
        9. 🧩 Optional settings sync from host user profile
           aivm vm sync_settings{cfg_flag}
        10. 🧑‍💻 Optional VS Code one-shot open (share + remote launch)
           aivm vm code{cfg_flag} --host_src . --sync_settings
        """).strip()
        print(steps)
        return 0


class HelpTreeCLI(_BaseCommand):
    """Print the expanded aivm command tree."""

    @classmethod
    def main(cls, argv=True, **kwargs):
        cls.cli(argv=argv, data=kwargs)
        from .main import AgentVMModalCLI

        print(_render_command_tree(AgentVMModalCLI))
        return 0


class HelpRawCLI(_BaseCommand):
    """Print direct system-tool commands equivalent to common aivm checks."""

    vm = scfg.Value(
        '',
        help='Optional VM name override.',
    )
    host_src = scfg.Value(
        '.',
        help='Host folder for attachment/share inspection context.',
    )

    @classmethod
    def main(cls, argv=True, **kwargs):
        args = cls.cli(argv=argv, data=kwargs)
        vm_name, net_name, fw_table = _resolve_raw_targets(
            config_opt=args.config,
            vm_opt=str(args.vm or '').strip(),
            host_src=Path(args.host_src).resolve(),
        )
        lines = textwrap.dedent(
            f"""
            # aivm help raw
            # Direct system-tool probes for the current managed context.
            # Mapping: VM={vm_name} | network={net_name} | firewall_table={fw_table}

            # List vms
            sudo virsh list

            # List networks
            sudo virsh net-list --all

            # VM domain lifecycle/state (maps to: aivm vm status / wait_ip)
            sudo virsh dominfo {shlex.quote(vm_name)}
            sudo virsh domstate {shlex.quote(vm_name)}
            sudo virsh dumpxml {shlex.quote(vm_name)}

            # Inspect a network
            sudo virsh net-info {shlex.quote(net_name)}
            sudo virsh net-dumpxml {shlex.quote(net_name)}
            sudo virsh net-dhcp-leases {shlex.quote(net_name)}

            # Network state of a VM
            sudo virsh domiflist {shlex.quote(vm_name)}
            sudo virsh domifaddr {shlex.quote(vm_name)}

            # Firewall table inspection (maps to: aivm host fw status)
            sudo nft list table inet {shlex.quote(fw_table)}

            # Image + VM disk files (maps to: aivm host image_fetch / vm create)
            sudo ls -lh /var/lib/libvirt/aivm/{shlex.quote(vm_name)}/images
            sudo qemu-img info /var/lib/libvirt/aivm/{shlex.quote(vm_name)}/images/{shlex.quote(vm_name)}.qcow2

            # SSH readiness probe (maps to: aivm status SSH readiness)
            # Replace <VM_IP> with DHCP lease / cached VM IP.
            ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null agent@<VM_IP> true

            # Host dependency checks (maps to: aivm host doctor)
            command -v virsh virt-install qemu-img cloud-localds nft ssh curl

            # Runtime environment detection (maps to: aivm status runtime environment)
            systemd-detect-virt
            grep -m1 -E '^(flags|Features)' /proc/cpuinfo
            """
        ).strip()
        print(ub.highlight_code(lines, lexer_name='bash'))
        return 0


class HelpModalCLI(scfg.ModalCLI):
    """Help and discovery commands."""

    plan = PlanCLI
    tree = HelpTreeCLI
    raw = HelpRawCLI


def _resolve_raw_targets(
    *,
    config_opt: str | None,
    vm_opt: str,
    host_src: Path,
) -> tuple[str, str, str]:
    vm_name = vm_opt or 'aivm-2404'
    net_name = 'aivm-net'
    fw_table = 'aivm_sandbox'
    reg = load_store(_cfg_path(config_opt))
    rec = None
    if vm_opt:
        rec = find_vm(reg, vm_opt)
    elif reg.active_vm:
        rec = find_vm(reg, reg.active_vm)
    elif len(reg.vms) == 1:
        rec = reg.vms[0]
    else:
        att = next(
            (a for a in reg.attachments if Path(a.host_path) == host_src),
            None,
        )
        if att is not None:
            rec = find_vm(reg, att.vm_name)
    if rec is not None:
        vm_name = rec.name
        net_name = rec.network_name or net_name
        net = next((n for n in reg.networks if n.name == net_name), None)
        if net is not None and net.firewall.table:
            fw_table = net.firewall.table
    return vm_name, net_name, fw_table


def _iter_modal_members(
    modal_cls: type[scfg.ModalCLI],
) -> list[tuple[str, type]]:
    members: list[tuple[str, type]] = []
    for name, val in modal_cls.__dict__.items():
        if name.startswith('_'):
            continue
        if not isinstance(val, type):
            continue
        if issubclass(val, scfg.ModalCLI) or issubclass(val, scfg.DataConfig):
            members.append((name, val))
    return members


def _short_help_line(cls: type) -> str:
    doc = (getattr(cls, '__doc__', '') or '').strip()
    if not doc:
        return ''
    return doc.splitlines()[0].strip()


def _render_command_tree(
    modal_cls: type[scfg.ModalCLI], prefix: str = 'aivm'
) -> str:
    root_help = _short_help_line(modal_cls)
    root_line = f'{prefix} - {root_help}' if root_help else prefix
    lines: list[str] = [root_line]

    def walk(cls: type[scfg.ModalCLI], parent: str, indent: str) -> None:
        members = _iter_modal_members(cls)
        for idx, (name, subcls) in enumerate(members):
            last = idx == len(members) - 1
            branch = '└── ' if last else '├── '
            path = f'{parent} {name}'
            help_line = _short_help_line(subcls)
            if help_line:
                lines.append(f'{indent}{branch}{path} - {help_line}')
            else:
                lines.append(f'{indent}{branch}{path}')
            if issubclass(subcls, scfg.ModalCLI):
                walk(subcls, path, indent + ('    ' if last else '│   '))

    walk(modal_cls, prefix, '')
    return '\n'.join(lines)
