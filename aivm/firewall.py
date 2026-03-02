"""Nftables rule generation and lifecycle helpers for VM network isolation."""

from __future__ import annotations

import xml.etree.ElementTree as ET

from loguru import logger

from .config import AgentVMConfig
from .runtime import virsh_system_cmd
from .util import run_cmd

log = logger


def _normalize_port_list(ports: list[int]) -> list[int]:
    seen: set[int] = set()
    out: list[int] = []
    for raw in ports or []:
        try:
            p = int(raw)
        except Exception as ex:
            raise RuntimeError(f'Invalid firewall port value: {raw!r}') from ex
        if p < 1 or p > 65535:
            raise RuntimeError(
                f'Invalid firewall port {p}; expected range 1..65535.'
            )
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
    return out


def _effective_bridge_and_gateway(cfg: AgentVMConfig) -> tuple[str, str]:
    """Prefer live libvirt network metadata over potentially stale config."""
    bridge = cfg.network.bridge
    gateway = cfg.network.gateway_ip
    res = run_cmd(
        virsh_system_cmd('net-dumpxml', cfg.network.name),
        sudo=True,
        check=False,
        capture=True,
    )
    if res.code != 0 or not (res.stdout or '').strip():
        return bridge, gateway
    try:
        root = ET.fromstring(res.stdout)
    except Exception:
        return bridge, gateway
    br_node = root.find('./bridge')
    ip_node = root.find('./ip')
    live_bridge = (
        br_node.attrib.get('name', '').strip() if br_node is not None else ''
    )
    live_gateway = (
        ip_node.attrib.get('address', '').strip() if ip_node is not None else ''
    )
    if live_bridge and live_bridge != bridge:
        log.warning(
            'Firewall bridge differs from config: config={} live={}. Using live value.',
            bridge,
            live_bridge,
        )
        bridge = live_bridge
    if live_gateway and live_gateway != gateway:
        log.warning(
            'Firewall gateway differs from config: config={} live={}. Using live value.',
            gateway,
            live_gateway,
        )
        gateway = live_gateway
    return bridge, gateway


def _nft_script(cfg: AgentVMConfig) -> str:
    table = cfg.firewall.table
    br, gw = _effective_bridge_and_gateway(cfg)
    blocks = list(cfg.firewall.block_cidrs) + list(
        cfg.firewall.extra_block_cidrs or []
    )
    seen = set()
    blocks2 = []
    for b in blocks:
        b = b.strip()
        if not b or b in seen:
            continue
        seen.add(b)
        blocks2.append(b)
    block_set = ', '.join(blocks2)
    allow_tcp = _normalize_port_list(cfg.firewall.allow_tcp_ports)
    allow_udp = _normalize_port_list(cfg.firewall.allow_udp_ports)
    host_allow_lines: list[str] = []
    blocked_allow_lines: list[str] = []
    if allow_tcp:
        ports = ', '.join(str(p) for p in allow_tcp)
        host_allow_lines.append(
            f'    iifname "{br}" tcp dport {{{ports}}} accept'
        )
        blocked_allow_lines.append(
            f'    iifname "{br}" ip daddr {{{block_set}}} tcp dport {{{ports}}} accept'
        )
    if allow_udp:
        ports = ', '.join(str(p) for p in allow_udp)
        host_allow_lines.append(
            f'    iifname "{br}" udp dport {{{ports}}} accept'
        )
        blocked_allow_lines.append(
            f'    iifname "{br}" ip daddr {{{block_set}}} udp dport {{{ports}}} accept'
        )
    host_allow = '\n'.join(host_allow_lines)
    blocked_allow = '\n'.join(blocked_allow_lines)
    if host_allow:
        host_allow = host_allow + '\n'
    if blocked_allow:
        blocked_allow = blocked_allow + '\n'
    return f"""
table inet {table} {{
  chain input {{
    type filter hook input priority 0; policy accept;
    ct state established,related accept
    # DHCP client traffic may be broadcast (255.255.255.255), not just gateway-directed.
    iifname "{br}" udp dport {{67,68}} accept
    iifname "{br}" ip daddr {gw} udp dport 53 accept
    iifname "{br}" ip daddr {gw} tcp dport 53 accept
    iifname "{br}" ip daddr {gw} icmp type echo-request accept
{host_allow}    # All other VM->host traffic on bridge is denied by default.
    iifname "{br}" drop
  }}
  chain forward {{
    type filter hook forward priority 0; policy accept;
    ct state established,related accept
{blocked_allow}    # Default blocklist for VM->LAN/private ranges.
    iifname "{br}" ip daddr {{{block_set}}} drop
    iifname "{br}" accept
  }}
}}
"""


def apply_firewall(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    log.debug('Applying nftables firewall rules')
    if not cfg.firewall.enabled:
        log.info('Firewall disabled in config; skipping.')
        return
    script = _nft_script(cfg)
    table = cfg.firewall.table
    if dry_run:
        log.info('DRYRUN: nft -f - <<EOF\\n{}\\nEOF', script.rstrip())
        return
    run_cmd(
        ['nft', 'delete', 'table', 'inet', table],
        sudo=True,
        check=False,
        capture=True,
    )
    run_cmd(
        ['nft', '-f', '-'],
        sudo=True,
        check=True,
        capture=True,
        input_text=script,
    )
    log.info('Firewall rules applied (table=inet {}).', table)


def firewall_status(cfg: AgentVMConfig) -> str:
    table = cfg.firewall.table
    res = run_cmd(
        ['nft', 'list', 'table', 'inet', table],
        sudo=True,
        check=False,
        capture=True,
    )
    return res.stdout + (res.stderr or '')


def remove_firewall(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    table = cfg.firewall.table
    if dry_run:
        log.info('DRYRUN: nft delete table inet {}', table)
        return
    run_cmd(
        ['nft', 'delete', 'table', 'inet', table],
        sudo=True,
        check=False,
        capture=True,
    )
    log.info('Firewall removed (table=inet {}).', table)
