"""Host default detection for first-run/bootstrap ergonomics.

Detection is best-effort and advisory: callers may still override all values via
config or CLI flags.
"""

from __future__ import annotations

import ipaddress
import os
import shlex
from fnmatch import fnmatch
from pathlib import Path

from loguru import logger

from .commands import CommandManager
from .config import AgentVMConfig
from .resource_checks import (
    host_cpu_count,
    host_free_disk_gb,
    host_mem_total_mb,
)
from .util import expand, which

log = logger


def _expand_identity_path(raw: str) -> str:
    # `%d` is commonly used in ssh config for local user's home directory.
    return expand(raw.replace('%d', '~'))


def _detect_identity_from_ssh_config() -> tuple[str, str]:
    cfg_path = Path(expand('~/.ssh/config'))
    if not cfg_path.exists():
        return '', ''
    target_host = 'aivm-default-probe'
    applies = True
    candidates: list[str] = []
    for raw_line in cfg_path.read_text(encoding='utf-8').splitlines():
        line = raw_line.strip()
        if not line or line.startswith('#'):
            continue
        try:
            parts = shlex.split(line, comments=True)
        except Exception:
            parts = line.split()
        if not parts:
            continue
        key = parts[0].lower()
        vals = parts[1:]
        if key == 'host':
            pos = [p for p in vals if not p.startswith('!')]
            neg = [p[1:] for p in vals if p.startswith('!')]
            pos_match = (
                any(fnmatch(target_host, p) for p in pos) if pos else False
            )
            neg_match = any(fnmatch(target_host, p) for p in neg)
            applies = pos_match and not neg_match
            continue
        if key == 'identityfile' and applies and vals:
            candidates.append(_expand_identity_path(vals[0]))
    for ident in candidates:
        if os.path.exists(ident):
            pub = ident + '.pub'
            return ident, pub if os.path.exists(pub) else ''
    return '', ''


def detect_ssh_identity() -> tuple[str, str]:
    log.debug('detecting detect_ssh_identity')
    ident, pub = _detect_identity_from_ssh_config()
    if ident:
        return ident, pub
    if which('ssh'):
        try:
            res = CommandManager.current().run(
                ['ssh', '-G', 'unknown@doesnt.exist'], check=True, capture=True
            )
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0].lower() == 'identityfile':
                    ident = _expand_identity_path(parts[1])
                    pub = ident + '.pub'
                    if os.path.exists(ident):
                        return ident, pub if os.path.exists(pub) else ''
        except Exception:
            pass
    preferred = ['id_ed25519', 'id_rsa']
    ssh_dir = Path(expand('~/.ssh'))
    for name in preferred:
        p = ssh_dir / name
        if p.exists():
            pub = str(p) + '.pub'
            return str(p), pub if os.path.exists(pub) else ''

    # Fallback for custom key names like id_<org>_ed25519.
    if ssh_dir.exists():
        for p in sorted(ssh_dir.glob('id_*')):
            if p.is_dir():
                continue
            n = p.name
            if n.endswith('.pub') or n.endswith('-cert.pub'):
                continue
            if n.endswith('.pem') or n.endswith('.ppk'):
                continue
            pub = str(p) + '.pub'
            return str(p), pub if os.path.exists(pub) else ''
    return '', ''


def existing_ipv4_routes() -> list[ipaddress.IPv4Network]:
    log.debug('introspecting existing_ipv4_routes')
    if which('ip') is None:
        log.warning('ip command not found; skipping route introspection')
        return []
    try:
        res = CommandManager.current().run(
            ['ip', '-4', 'route', 'show'], check=True, capture=True
        )
    except Exception as ex:
        log.warning('Failed to inspect host routes: {}', ex)
        return []
    nets: list[ipaddress.IPv4Network] = []
    for line in res.stdout.splitlines():
        tok = line.split()[0]
        if '/' in tok:
            try:
                net = ipaddress.ip_network(tok, strict=False)
                if isinstance(net, ipaddress.IPv4Network):
                    nets.append(net)
            except Exception:
                continue
    return nets


def pick_free_subnet(preferred: list[str]) -> str:
    routes = existing_ipv4_routes()
    for cidr in preferred:
        cand = ipaddress.ip_network(cidr, strict=False)
        if not any(cand.overlaps(r) for r in routes):
            log.debug(
                'Selected free subnet {} from preferred candidates.',
                cidr,
            )
            return cidr
    log.warning(
        'No preferred free subnet found; falling back to first candidate {}.',
        preferred[0],
    )
    return preferred[0]


def _recommend_vm_resources(
    *,
    host_cpus: int | None,
    host_mem_total_mb: int | None,
    host_free_disk: float | None,
) -> tuple[int, int, int]:
    """Choose conservative VM defaults based on host resource tiers."""
    cpus = 4
    if host_cpus is not None:
        if host_cpus <= 2:
            cpus = 1
        elif host_cpus <= 4:
            cpus = 2
        elif host_cpus <= 8:
            cpus = 4
        elif host_cpus <= 16:
            cpus = 6
        else:
            cpus = 8

    ram_mb = 8192
    if host_mem_total_mb is not None:
        if host_mem_total_mb <= 4096:
            ram_mb = 2048
        elif host_mem_total_mb <= 8192:
            ram_mb = 3072
        elif host_mem_total_mb <= 16384:
            ram_mb = 4096
        elif host_mem_total_mb <= 32768:
            ram_mb = 8192
        else:
            ram_mb = 12288

    disk_gb = 40
    if host_free_disk is not None:
        if host_free_disk <= 32:
            disk_gb = 16
        elif host_free_disk <= 64:
            disk_gb = 24
        elif host_free_disk <= 128:
            disk_gb = 32
        elif host_free_disk <= 256:
            disk_gb = 40
        else:
            disk_gb = 64

    return cpus, ram_mb, disk_gb


def auto_defaults(cfg: AgentVMConfig, *, project_dir: Path) -> AgentVMConfig:
    log.trace('Start automatic default detection')
    ident, pub = detect_ssh_identity()
    if not cfg.paths.ssh_identity_file and ident:
        cfg.paths.ssh_identity_file = ident
    if not cfg.paths.ssh_pubkey_path and pub:
        cfg.paths.ssh_pubkey_path = pub

    preferred = [
        '10.77.0.0/24',
        '10.78.0.0/24',
        '10.79.0.0/24',
        '10.88.0.0/24',
        '10.99.0.0/24',
        '192.168.77.0/24',
        '192.168.88.0/24',
    ]
    subnet = pick_free_subnet(preferred)
    cfg.network.subnet_cidr = subnet
    net = ipaddress.ip_network(subnet, strict=False)
    base = int(net.network_address)
    cfg.network.gateway_ip = str(ipaddress.IPv4Address(base + 1))
    cfg.network.dhcp_start = str(ipaddress.IPv4Address(base + 100))
    cfg.network.dhcp_end = str(ipaddress.IPv4Address(base + 200))

    # Keep default sizing practical across laptops/workstations/servers.
    host_cpus = host_cpu_count()
    host_mem_mb = host_mem_total_mb()
    host_free_disk = host_free_disk_gb(Path(cfg.paths.base_dir).expanduser())
    cpus, ram_mb, disk_gb = _recommend_vm_resources(
        host_cpus=host_cpus,
        host_mem_total_mb=host_mem_mb,
        host_free_disk=host_free_disk,
    )
    cfg.vm.cpus = cpus
    cfg.vm.ram_mb = ram_mb
    cfg.vm.disk_gb = disk_gb
    log.info(
        'Detected VM defaults from host resources: cpus={} ram_mb={} disk_gb={} '
        '(host_cpus={} host_mem_total_mb={} host_free_disk_gb={})',
        cpus,
        ram_mb,
        disk_gb,
        host_cpus,
        host_mem_mb,
        f'{host_free_disk:.1f}' if host_free_disk is not None else 'unknown',
    )

    if len(cfg.network.bridge) > 15:
        cfg.network.bridge = 'virbr-aivm'
    log.trace('Finish automatic default detection')
    return cfg
