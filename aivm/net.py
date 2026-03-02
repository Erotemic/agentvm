"""Libvirt network create/destroy/status helpers for the managed NAT network."""

from __future__ import annotations

import ipaddress
import tempfile

from loguru import logger

from .config import AgentVMConfig
from .util import run_cmd, which

log = logger


def _route_overlap(target_cidr: str) -> str | None:
    target = ipaddress.ip_network(target_cidr, strict=False)
    if which('ip') is None:
        log.warning('ip command not found; skipping route overlap check')
        return None
    try:
        res = run_cmd(['ip', '-4', 'route', 'show'], check=True, capture=True)
    except Exception as ex:
        log.warning('Unable to inspect routes for overlap checks: {}', ex)
        return None
    for line in res.stdout.splitlines():
        tok = line.split()[0]
        if '/' in tok:
            try:
                n = ipaddress.ip_network(tok, strict=False)
            except Exception:
                continue
            if target.overlaps(n) and str(n) != str(target):
                return str(n)
    return None


def ensure_network(
    cfg: AgentVMConfig, *, recreate: bool = False, dry_run: bool = False
) -> None:
    log.debug('Ensuring libvirt network {} exists', cfg.network.name)
    name = cfg.network.name
    bridge = cfg.network.bridge
    subnet = cfg.network.subnet_cidr
    gw = cfg.network.gateway_ip
    dhcp_start = cfg.network.dhcp_start
    dhcp_end = cfg.network.dhcp_end

    subnet_net = ipaddress.ip_network(subnet, strict=False)
    prefix = subnet_net.prefixlen

    if len(bridge) > 15:
        raise RuntimeError(
            f'Bridge name too long ({len(bridge)} > 15): {bridge}'
        )

    overlap = _route_overlap(subnet)
    if overlap:
        raise RuntimeError(
            f'NET_SUBNET_CIDR {subnet} overlaps existing route {overlap}. Pick a different subnet.'
        )

    if dry_run:
        exists = False
    else:
        exists = (
            run_cmd(
                ['virsh', 'net-info', name],
                check=False,
                capture=True,
                sudo=True,
            ).code
            == 0
        )
    if exists and not recreate:
        log.info('Network exists: {}', name)
        return
    if exists and recreate:
        if dry_run:
            log.info(
                'DRYRUN: virsh net-destroy {}; virsh net-undefine {}',
                name,
                name,
            )
        else:
            run_cmd(
                ['virsh', 'net-destroy', name],
                sudo=True,
                check=False,
                capture=True,
            )
            run_cmd(
                ['virsh', 'net-undefine', name],
                sudo=True,
                check=False,
                capture=True,
            )

    xml = f"""<network>
  <name>{name}</name>
  <forward mode='nat'/>
  <bridge name='{bridge}' stp='on' delay='0'/>
  <ip address='{gw}' prefix='{prefix}'>
    <dhcp>
      <range start='{dhcp_start}' end='{dhcp_end}'/>
    </dhcp>
  </ip>
</network>
"""
    if dry_run:
        log.info(
            'DRYRUN: define network {} on {} (bridge={})', name, subnet, bridge
        )
        return

    with tempfile.NamedTemporaryFile('w', delete=False) as f:
        f.write(xml)
        tmp = f.name
    run_cmd(['virsh', 'net-define', tmp], sudo=True, check=True, capture=True)
    run_cmd(
        ['virsh', 'net-autostart', name], sudo=True, check=True, capture=True
    )
    run_cmd(['virsh', 'net-start', name], sudo=True, check=True, capture=True)
    log.info('Network ready: {} (bridge={})', name, bridge)


def network_status(cfg: AgentVMConfig) -> str:
    name = cfg.network.name
    info = run_cmd(
        ['virsh', 'net-info', name], sudo=True, check=False, capture=True
    )
    dump = run_cmd(
        ['virsh', 'net-dumpxml', name], sudo=True, check=False, capture=True
    )
    return info.stdout + '\n' + dump.stdout


def destroy_network(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    name = cfg.network.name
    if dry_run:
        log.info(
            'DRYRUN: virsh net-destroy {}; virsh net-undefine {}', name, name
        )
        return
    run_cmd(
        ['virsh', 'net-destroy', name], sudo=True, check=False, capture=True
    )
    run_cmd(
        ['virsh', 'net-undefine', name], sudo=True, check=False, capture=True
    )
    log.info('Network removed: {}', name)
