"""Auto-detection of host defaults such as SSH identity and network parameters."""

from __future__ import annotations

import ipaddress
import os
import shlex
from fnmatch import fnmatch
from pathlib import Path

from loguru import logger

from .config import AgentVMConfig
from .util import expand, run_cmd, which

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
            res = run_cmd(
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
        res = run_cmd(['ip', '-4', 'route', 'show'], check=True, capture=True)
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

    if len(cfg.network.bridge) > 15:
        cfg.network.bridge = 'virbr-aivm'
    log.trace('Finish automatic default detection')
    return cfg
