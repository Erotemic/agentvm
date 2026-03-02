"""Probe and rendering logic for host/VM/network/firewall status reporting."""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from .config import AgentVMConfig
from .host import check_commands
from .runtime import require_ssh_identity, ssh_base_args, virsh_system_cmd
from .store import load_store, store_path
from .util import run_cmd, which
from .vm import get_ip_cached, vm_share_mappings


@dataclass(frozen=True)
class ProbeOutcome:
    ok: bool | None
    detail: str
    diag: str = ''


def status_line(ok: bool | None, label: str, detail: str = '') -> str:
    icon = '✅' if ok is True else ('➖' if ok is None else '❌')
    suffix = f' - {detail}' if detail else ''
    return f'{icon} {label}{suffix}'


def clip(text: str, *, max_lines: int = 60) -> str:
    lines = (text or '').strip().splitlines()
    if len(lines) <= max_lines:
        return '\n'.join(lines)
    keep: list[str] = list(lines[:max_lines])
    keep.append(f'... ({len(lines) - max_lines} more lines)')
    return '\n'.join(keep)


def probe_runtime_environment() -> ProbeOutcome:
    """Best-effort detection of whether we are on bare metal or in a VM."""
    diag_lines: list[str] = []
    if which('systemd-detect-virt'):
        det = run_cmd(
            ['systemd-detect-virt'], sudo=False, check=False, capture=True
        )
        raw = (det.stdout or det.stderr).strip()
        if raw:
            diag_lines.append(f'systemd-detect-virt: {raw} (code={det.code})')
        kind = (det.stdout or '').strip().lower()
        if kind and kind != 'none':
            return ProbeOutcome(
                True,
                f'virtualized guest ({kind})',
                '\n'.join(diag_lines),
            )
        if det.code == 0 and kind == 'none':
            return ProbeOutcome(
                True,
                'host system (no virtualization detected)',
                '\n'.join(diag_lines),
            )
    else:
        diag_lines.append('systemd-detect-virt unavailable')

    cpuinfo_path = Path('/proc/cpuinfo')
    if cpuinfo_path.exists():
        try:
            cpuinfo = cpuinfo_path.read_text(encoding='utf-8', errors='ignore')
        except Exception:
            cpuinfo = ''
        if cpuinfo:
            has_hypervisor_flag = False
            for line in cpuinfo.splitlines():
                low = line.strip().lower()
                if low.startswith('flags') or low.startswith('features'):
                    if 'hypervisor' in low.split():
                        has_hypervisor_flag = True
                        break
            diag_lines.append(
                f'cpuinfo_hypervisor_flag={"yes" if has_hypervisor_flag else "no"}'
            )
            if has_hypervisor_flag:
                return ProbeOutcome(
                    True,
                    'virtualized guest (cpu hypervisor flag)',
                    '\n'.join(diag_lines),
                )

    dmi_path = Path('/sys/class/dmi/id/product_name')
    if dmi_path.exists():
        try:
            product_name = dmi_path.read_text(
                encoding='utf-8', errors='ignore'
            ).strip()
        except Exception:
            product_name = ''
        if product_name:
            diag_lines.append(f'dmi_product_name={product_name}')
            lower = product_name.lower()
            vm_signals = (
                'kvm',
                'qemu',
                'vmware',
                'virtualbox',
                'bochs',
                'xen',
                'hyper-v',
            )
            if any(sig in lower for sig in vm_signals):
                return ProbeOutcome(
                    True,
                    f'virtualized guest ({product_name})',
                    '\n'.join(diag_lines),
                )

    return ProbeOutcome(
        None,
        'unable to determine host vs guest',
        '\n'.join(diag_lines),
    )


def probe_network(cfg: AgentVMConfig, *, use_sudo: bool) -> ProbeOutcome:
    info = run_cmd(
        virsh_system_cmd('net-info', cfg.network.name),
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if info.code != 0:
        raw_detail = (info.stderr or info.stdout or '').strip()
        detail = raw_detail.lower()
        if 'permission denied' in detail or 'authentication failed' in detail:
            return ProbeOutcome(
                None,
                f'{cfg.network.name} unavailable (run status --sudo for privileged checks)',
            )
        if not use_sudo:
            return ProbeOutcome(
                None,
                f'{cfg.network.name} probe inconclusive without sudo ({raw_detail or "unknown error"})',
            )
        return ProbeOutcome(False, f'{cfg.network.name} not defined')
    active = False
    autostart = False
    for line in (info.stdout or '').splitlines():
        if ':' not in line:
            continue
        key, val = [x.strip().lower() for x in line.split(':', 1)]
        if key == 'active':
            active = val == 'yes'
        elif key == 'autostart':
            autostart = val == 'yes'
    if active:
        return ProbeOutcome(
            True,
            f'{cfg.network.name} active (autostart={"yes" if autostart else "no"})',
        )
    return ProbeOutcome(False, f'{cfg.network.name} defined but inactive')


def probe_firewall(cfg: AgentVMConfig, *, use_sudo: bool) -> ProbeOutcome:
    if not cfg.firewall.enabled:
        return ProbeOutcome(None, 'disabled in config')
    res = run_cmd(
        ['nft', 'list', 'table', 'inet', cfg.firewall.table],
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if res.code == 0:
        return ProbeOutcome(True, f'table inet {cfg.firewall.table} present')
    detail = (res.stderr or res.stdout or '').strip().lower()
    if 'operation not permitted' in detail or 'permission denied' in detail:
        return ProbeOutcome(
            None, 'requires privileges (run status --sudo for firewall checks)'
        )
    return ProbeOutcome(False, f'table inet {cfg.firewall.table} missing')


def probe_vm_state(
    cfg: AgentVMConfig, *, use_sudo: bool
) -> tuple[ProbeOutcome, bool]:
    dom = run_cmd(
        virsh_system_cmd('dominfo', cfg.vm.name),
        sudo=use_sudo,
        check=False,
        capture=True,
    )
    if dom.code != 0:
        raw_detail = (dom.stderr or dom.stdout or '').strip()
        detail = raw_detail.lower()
        if 'permission denied' in detail or 'authentication failed' in detail:
            return (
                ProbeOutcome(
                    None,
                    f'{cfg.vm.name} unavailable (run status --sudo for privileged checks)',
                ),
                False,
            )
        if not use_sudo:
            return (
                ProbeOutcome(
                    None,
                    f'{cfg.vm.name} probe inconclusive without sudo ({raw_detail or "unknown error"})',
                ),
                False,
            )
        return ProbeOutcome(False, f'{cfg.vm.name} not defined'), False
    state = run_cmd(
        virsh_system_cmd('domstate', cfg.vm.name),
        sudo=use_sudo,
        check=False,
        capture=True,
    ).stdout.strip()
    return ProbeOutcome(
        'running' in state.lower(), f'{cfg.vm.name} state={state}'
    ), True


def probe_ssh_ready(cfg: AgentVMConfig, ip: str) -> ProbeOutcome:
    try:
        ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    except Exception as ex:
        return ProbeOutcome(False, str(ex), '')
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            batch_mode=True,
            connect_timeout=3,
            strict_host_key_checking='no',
            user_known_hosts_file='/dev/null',
        ),
        f'{cfg.vm.user}@{ip}',
        'true',
    ]
    res = run_cmd(cmd, sudo=False, check=False, capture=True)
    detail = 'ready' if res.code == 0 else 'not ready'
    diag = (res.stdout + '\n' + res.stderr).strip()
    return ProbeOutcome(res.code == 0, detail, diag)


def probe_provisioned(cfg: AgentVMConfig, ip: str) -> ProbeOutcome:
    if not cfg.provision.enabled:
        return ProbeOutcome(None, 'disabled in config', '')
    try:
        ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    except Exception as ex:
        return ProbeOutcome(False, str(ex), '')
    needed = list(cfg.provision.packages)
    if cfg.provision.install_docker:
        needed.extend(['docker.io', 'docker-compose-v2'])
    quoted = ' '.join(f"'{p}'" for p in needed)
    remote = (
        'set -e; '
        f'for p in {quoted}; do '
        "dpkg-query -W -f='${Status}' \"$p\" 2>/dev/null | grep -q 'install ok installed' || exit 10; "
        'done'
    )
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            batch_mode=True,
            connect_timeout=4,
            strict_host_key_checking='no',
            user_known_hosts_file='/dev/null',
        ),
        f'{cfg.vm.user}@{ip}',
        remote,
    ]
    res = run_cmd(cmd, sudo=False, check=False, capture=True)
    if res.code == 0:
        return ProbeOutcome(True, 'configured packages appear present', '')
    diag = (res.stdout + '\n' + res.stderr).strip()
    return ProbeOutcome(False, 'one or more configured packages missing', diag)


def render_status(
    cfg: AgentVMConfig,
    path: Path,
    *,
    detail: bool = False,
    use_sudo: bool = False,
) -> str:
    lines: list[str] = ['🧭 AgentVM Status', f'📄 Config: {path}', '']
    done = 0
    total = 0

    missing, missing_opt = check_commands()
    host_ok = len(missing) == 0
    total += 1
    done += int(host_ok)
    host_detail = (
        'all required commands found'
        if host_ok
        else f'missing: {", ".join(missing)}'
    )
    if missing_opt:
        host_detail += f' (optional missing: {", ".join(missing_opt)})'
    lines.append(status_line(host_ok, 'Host dependencies', host_detail))

    env = probe_runtime_environment()
    lines.append(status_line(env.ok, 'Runtime environment', env.detail))

    net = probe_network(cfg, use_sudo=use_sudo)
    if net.ok is not None:
        total += 1
        done += int(net.ok)
    lines.append(status_line(net.ok, 'Libvirt network', net.detail))

    fw = probe_firewall(cfg, use_sudo=use_sudo)
    if fw.ok is not None:
        total += 1
        done += int(fw.ok)
    lines.append(status_line(fw.ok, 'Firewall', fw.detail))

    base_img = (
        Path(cfg.paths.base_dir) / cfg.vm.name / 'images' / cfg.image.cache_name
    )
    img_ok = (
        run_cmd(
            ['test', '-f', str(base_img)],
            sudo=use_sudo,
            check=False,
            capture=True,
        ).code
        == 0
    )
    if use_sudo:
        total += 1
        done += int(img_ok)
        lines.append(status_line(img_ok, 'Base image cache', str(base_img)))
    else:
        lines.append(
            status_line(
                None, 'Base image cache', f'skipped without --sudo ({base_img})'
            )
        )

    vm_out, vm_defined = probe_vm_state(cfg, use_sudo=use_sudo)
    if vm_out.ok is not None:
        total += 1
        done += int(vm_out.ok)
    lines.append(status_line(vm_out.ok, 'VM state', vm_out.detail))

    share_mappings: list[tuple[str, str]] = []
    if vm_defined:
        share_mappings = vm_share_mappings(cfg, use_sudo=use_sudo)
    if vm_defined and share_mappings:
        lines.append(
            status_line(
                True,
                'VM shared folders',
                f'{len(share_mappings)} mapping(s) configured (use --detail to inspect host paths)',
            )
        )
    elif vm_defined:
        lines.append(status_line(None, 'VM shared folders', 'none detected'))
    else:
        lines.append(status_line(None, 'VM shared folders', 'VM not defined'))

    ip = get_ip_cached(cfg)
    ip_ok = bool(ip) and bool(vm_defined)
    if vm_out.ok is not None:
        total += 1
        done += int(ip_ok)
    if ip and not vm_defined:
        lines.append(
            status_line(False, 'Cached VM IP', f'{ip} (stale: VM not defined)')
        )
        ip = None
    else:
        lines.append(
            status_line(bool(ip), 'Cached VM IP', ip or 'no cached IP yet')
        )

    ssh = ProbeOutcome(False, 'VM/IP not ready', '')
    if vm_out.ok is True and ip:
        ssh = probe_ssh_ready(cfg, ip)
    total += 1
    done += int(bool(ssh.ok))
    lines.append(status_line(bool(ssh.ok), 'SSH readiness', ssh.detail))

    if vm_out.ok is True and ip and ssh.ok:
        prov = probe_provisioned(cfg, ip)
    else:
        prov = ProbeOutcome(
            None,
            'waiting for SSH'
            if cfg.provision.enabled
            else 'disabled in config',
            '',
        )
    if prov.ok is not None:
        total += 1
        done += int(bool(prov.ok))
    lines.append(status_line(prov.ok, 'Provisioning', prov.detail))

    lines.append('')
    lines.append(f'📊 Progress: {done}/{total} checks complete')
    if not use_sudo:
        lines.append(
            'ℹ️ Some privileged checks are skipped/limited without --sudo.'
        )

    if detail:
        lines.append('')
        lines.append('🔬 Detailed Diagnostics')
        lines.append('')
        lines.append('Host')
        lines.append(
            f'- required missing: {", ".join(missing) if missing else "(none)"}'
        )
        lines.append(
            f'- optional missing: {", ".join(missing_opt) if missing_opt else "(none)"}'
        )
        lines.append(f'- runtime environment: {env.detail}')
        if env.diag:
            lines.append('- runtime diagnostics:')
            lines.append('```text')
            lines.append(clip(env.diag))
            lines.append('```')
        lines.append('')

        net_info = run_cmd(
            virsh_system_cmd('net-info', cfg.network.name),
            sudo=use_sudo,
            check=False,
            capture=True,
        )
        net_xml = run_cmd(
            virsh_system_cmd('net-dumpxml', cfg.network.name),
            sudo=use_sudo,
            check=False,
            capture=True,
        )
        lines.append(f'Network ({cfg.network.name})')
        lines.append('```text')
        lines.append(
            clip(
                (net_info.stdout + '\n' + net_info.stderr).strip()
                or '(no output)'
            )
        )
        lines.append('```')
        if net_xml.code == 0 and net_xml.stdout.strip():
            lines.append('```xml')
            lines.append(clip(net_xml.stdout, max_lines=80))
            lines.append('```')
        lines.append('')

        lines.append(f'Firewall (inet {cfg.firewall.table})')
        if cfg.firewall.enabled:
            fw_raw = run_cmd(
                ['nft', 'list', 'table', 'inet', cfg.firewall.table],
                sudo=use_sudo,
                check=False,
                capture=True,
            )
            lines.append('```text')
            lines.append(
                clip(
                    (fw_raw.stdout + '\n' + fw_raw.stderr).strip()
                    or '(no output)'
                )
            )
            lines.append('```')
        else:
            lines.append('- disabled in config')
        lines.append('')

        lines.append('Image')
        img_stat = run_cmd(
            ['bash', '-lc', f'ls -lh {base_img} 2>&1'],
            sudo=use_sudo,
            check=False,
            capture=True,
        )
        lines.append('```text')
        lines.append(
            clip(
                (img_stat.stdout + '\n' + img_stat.stderr).strip()
                or '(no output)'
            )
        )
        lines.append('```')
        lines.append('')

        lines.append(f'VM ({cfg.vm.name})')
        for cmd in (
            virsh_system_cmd('dominfo', cfg.vm.name),
            virsh_system_cmd('domstate', cfg.vm.name),
            virsh_system_cmd('domiflist', cfg.vm.name),
            virsh_system_cmd('domifaddr', cfg.vm.name),
            virsh_system_cmd('net-dhcp-leases', cfg.network.name),
        ):
            vm_raw = run_cmd(cmd, sudo=use_sudo, check=False, capture=True)
            lines.append(f'`{" ".join(cmd)}`')
            lines.append('```text')
            lines.append(
                clip(
                    (vm_raw.stdout + '\n' + vm_raw.stderr).strip()
                    or '(no output)'
                )
            )
            lines.append('```')
        lines.append('Filesystem shares')
        if share_mappings:
            for src, tag in share_mappings:
                lines.append(f'- host_src: {src or "(none)"}')
                lines.append(f'  tag: {tag or "(none)"}')
        else:
            lines.append('- none detected')
        lines.append('')

        ip_file = Path(cfg.paths.state_dir) / cfg.vm.name / f'{cfg.vm.name}.ip'
        lines.append('Cache')
        lines.append(f'- ip file: {ip_file}')
        if ip_file.exists():
            lines.append(
                f'- ip value: {ip_file.read_text(encoding="utf-8").strip() or "(empty)"}'
            )
        else:
            lines.append('- ip value: (missing)')
        lines.append('')

        lines.append('SSH probe')
        if ssh.diag:
            lines.append('```text')
            lines.append(clip(ssh.diag))
            lines.append('```')
        else:
            lines.append('- no probe output')
        lines.append('')

        lines.append('Provision probe')
        if prov.diag:
            lines.append('```text')
            lines.append(clip(prov.diag))
            lines.append('```')
        else:
            lines.append('- no probe output')

        cfg_arg = shlex.quote(str(path))
        next_steps: list[str] = []
        if missing:
            next_steps.append(f'aivm host install_deps --config {cfg_arg}')
        if net.ok is False:
            next_steps.append(f'aivm host net create --config {cfg_arg}')
        if cfg.firewall.enabled and fw.ok is not True:
            next_steps.append(f'aivm host fw apply --config {cfg_arg}')
        if (not img_ok) and (vm_out.ok is not True):
            next_steps.append(f'aivm host image_fetch --config {cfg_arg}')
        if not vm_out.ok:
            next_steps.append(f'aivm vm create --config {cfg_arg}')
        if vm_out.ok and not ip:
            next_steps.append(f'aivm vm wait_ip --config {cfg_arg}')
        if vm_out.ok and ip and not ssh.ok:
            next_steps.append(f'aivm vm status --config {cfg_arg}')
        if prov.ok is False:
            next_steps.append(f'aivm vm provision --config {cfg_arg}')
        if next_steps:
            lines.append('')
            lines.append('🛠️ Suggested Next Commands')
            for cmd in next_steps:
                lines.append(f'- `{cmd}`')
    return '\n'.join(lines)


def render_global_status() -> str:
    lines: list[str] = ['🧭 AIVM Global Status', '']
    missing, missing_opt = check_commands()
    host_ok = len(missing) == 0
    host_detail = (
        'all required commands found'
        if host_ok
        else f'missing: {", ".join(missing)}'
    )
    if missing_opt:
        host_detail += f' (optional missing: {", ".join(missing_opt)})'
    lines.append(status_line(host_ok, 'Host dependencies', host_detail))
    env = probe_runtime_environment()
    lines.append(status_line(env.ok, 'Runtime environment', env.detail))

    reg_path = store_path()
    reg = load_store(reg_path)
    lines.append(status_line(True, 'Config store', str(reg_path)))
    lines.append('')
    lines.append('📦 Managed Resources')
    lines.append(f'- VMs: {len(reg.vms)}')
    lines.append(f'- Folder attachments: {len(reg.attachments)}')
    if reg.vms:
        vm_names = ', '.join(sorted(v.name for v in reg.vms[:8]))
        extra = '' if len(reg.vms) <= 8 else f' (+{len(reg.vms) - 8} more)'
        lines.append(f'- VM names: {vm_names}{extra}')

    lines.append('')
    lines.append('ℹ️ No in-scope VM config found for this directory.')
    lines.append(
        'Use `aivm config init`, then `aivm vm create`, or run '
        '`aivm status --vm <name>`.'
    )
    return '\n'.join(lines)
