"""Libvirt network lifecycle helpers for the managed NAT network.

The functions here are intentionally idempotent-oriented (ensure/destroy/status)
so higher-level CLI flows can compose them safely.
"""

from __future__ import annotations

import ipaddress
import tempfile
import textwrap

from loguru import logger

from .commands import CommandManager
from .config import AgentVMConfig
from .util import which

log = logger


def _route_overlap(target_cidr: str) -> str | None:
    target = ipaddress.ip_network(target_cidr, strict=False)
    if which('ip') is None:
        log.warning('ip command not found; skipping route overlap check')
        return None
    try:
        res = CommandManager.current().run(
            ['ip', '-4', 'route', 'show'], check=True, capture=True
        )
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

    mgr = CommandManager.current()
    with mgr.intent(
        f'Ensure libvirt network {name}',
        why=(
            'Managed VMs rely on a known NAT bridge, gateway, and DHCP range '
            'before VM definitions or firewall rules can work predictably.'
        ),
        role='modify',
    ):
        if dry_run:
            exists = False
        else:
            with mgr.step(
                'Inspect managed network state',
                why='Check whether the target libvirt network already exists.',
                approval_scope=f'network-probe:{name}',
            ):
                exists_probe = mgr.submit(
                    ['virsh', 'net-info', name],
                    check=False,
                    capture=True,
                    sudo=True,
                    role='read',
                    summary=f'Check whether libvirt network {name} exists',
                )
            exists = exists_probe.code == 0
        if exists and not recreate:
            log.info('Network exists: {}', name)
            return

        xml = textwrap.dedent(
            f"""\
            <network>
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
        )
        if dry_run:
            log.info(
                'DRYRUN: define network {} on {} (bridge={})',
                name,
                subnet,
                bridge,
            )
            return

        with tempfile.NamedTemporaryFile('w', delete=False) as f:
            f.write(xml)
            tmp = f.name
        with mgr.step(
            'Define and start managed libvirt network',
            why=(
                'Create the configured NAT network definition, enable autostart, '
                'and bring it online for VM use.'
            ),
            approval_scope=f'network-ensure:{name}',
        ):
            if exists and recreate:
                mgr.submit(
                    ['virsh', 'net-destroy', name],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=True,
                    summary=f'Stop existing libvirt network {name}',
                )
                mgr.submit(
                    ['virsh', 'net-undefine', name],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=True,
                    summary=f'Remove existing libvirt network definition {name}',
                )
            mgr.submit(
                ['virsh', 'net-define', tmp],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary=f'Define libvirt network {name} from generated XML',
                detail=f'bridge={bridge} subnet={subnet} gateway={gw}',
            )
            mgr.submit(
                ['virsh', 'net-autostart', name],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary=f'Enable autostart for libvirt network {name}',
            )
            mgr.submit(
                ['virsh', 'net-start', name],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary=f'Start libvirt network {name}',
            )
        log.info('Network ready: {} (bridge={})', name, bridge)


def network_status(cfg: AgentVMConfig) -> str:
    name = cfg.network.name
    mgr = CommandManager.current()
    info = mgr.run(
        ['virsh', 'net-info', name],
        sudo=True,
        role='read',
        check=False,
        capture=True,
    )
    dump = mgr.run(
        ['virsh', 'net-dumpxml', name],
        sudo=True,
        role='read',
        check=False,
        capture=True,
    )
    return info.stdout + '\n' + dump.stdout


def destroy_network(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    name = cfg.network.name
    if dry_run:
        log.info(
            'DRYRUN: virsh net-destroy {}; virsh net-undefine {}', name, name
        )
        return
    mgr = CommandManager.current()
    mgr.run(
        ['virsh', 'net-destroy', name],
        sudo=True,
        role='modify',
        check=False,
        capture=True,
    )
    mgr.run(
        ['virsh', 'net-undefine', name],
        sudo=True,
        role='modify',
        check=False,
        capture=True,
    )
    log.info('Network removed: {}', name)
