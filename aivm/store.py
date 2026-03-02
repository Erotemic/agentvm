"""Single-file config store for defaults, networks, VMs, and attachments."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomllib
import ubelt as ub
from loguru import logger as log

from .config import AgentVMConfig, FirewallConfig, NetworkConfig


@dataclass
class VMEntry:
    name: str
    network_name: str
    cfg: AgentVMConfig


@dataclass
class NetworkEntry:
    name: str
    network: NetworkConfig = field(default_factory=NetworkConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)


@dataclass
class AttachmentEntry:
    host_path: str
    vm_name: str
    mode: str = 'shared'
    guest_dst: str = ''
    tag: str = ''


@dataclass
class Store:
    schema_version: int = 5
    active_vm: str = ''
    defaults: AgentVMConfig | None = None
    networks: list[NetworkEntry] = field(default_factory=list)
    vms: list[VMEntry] = field(default_factory=list)
    attachments: list[AttachmentEntry] = field(default_factory=list)


def _appdir(appname: str, kind: str) -> Path:
    p = ub.Path.appdir(appname, type=kind).ensuredir()
    return Path(p)


def store_path() -> Path:
    return _appdir('aivm', 'config') / 'config.toml'


def _norm_dir(path: str | Path) -> str:
    p = Path(path).expanduser()
    try:
        return str(p.resolve())
    except Exception:
        return str(p.absolute())


def _cfg_from_dict(raw: dict) -> AgentVMConfig:
    cfg = AgentVMConfig()
    for section in (
        'vm',
        'network',
        'firewall',
        'image',
        'provision',
        'sync',
        'paths',
    ):
        body = raw.get(section, None)
        if isinstance(body, dict):
            obj = getattr(cfg, section)
            for k, v in body.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
    if 'verbosity' in raw:
        cfg.verbosity = int(raw['verbosity'])
    return cfg


def _toml_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def _emit_toml_kv(lines: list[str], key: str, val: object) -> None:
    if isinstance(val, bool):
        lines.append(f'{key} = {"true" if val else "false"}')
    elif isinstance(val, int):
        lines.append(f'{key} = {val}')
    elif isinstance(val, list):
        parts = [f'"{_toml_escape(str(item))}"' for item in val]
        lines.append(f'{key} = [{", ".join(parts)}]')
    else:
        lines.append(f'{key} = "{_toml_escape(str(val))}"')


def load_store(path: Path | None = None) -> Store:
    log.trace(f'Start load store {path}')
    fpath = path or store_path()
    if not fpath.exists():
        log.trace('Finish load store: does not exist, returning default')
        return Store()
    raw = tomllib.loads(fpath.read_text(encoding='utf-8'))
    reg = Store()
    reg.schema_version = int(raw.get('schema_version', 5))
    reg.active_vm = str(raw.get('active_vm', '')).strip()
    defaults_raw = raw.get('defaults', None)
    if isinstance(defaults_raw, dict):
        reg.defaults = _cfg_from_dict(defaults_raw).expanded_paths()

    for item in raw.get('networks', []):
        if not isinstance(item, dict):
            continue
        net = NetworkConfig()
        fw = FirewallConfig()
        net_raw = item.get('network', None)
        if isinstance(net_raw, dict):
            for k, v in net_raw.items():
                if hasattr(net, k):
                    setattr(net, k, v)
        fw_raw = item.get('firewall', None)
        if isinstance(fw_raw, dict):
            for k, v in fw_raw.items():
                if hasattr(fw, k):
                    setattr(fw, k, v)
        name = str(item.get('name', '')).strip()
        if not name:
            name = str(net.name or '').strip()
        if not name:
            continue
        net.name = name
        reg.networks.append(NetworkEntry(name=name, network=net, firewall=fw))

    for item in raw.get('vms', []):
        if not isinstance(item, dict):
            continue
        name = str(item.get('name', '')).strip()
        if not name:
            continue
        cfg = _cfg_from_dict(item).expanded_paths()
        cfg.vm.name = name
        network_name = str(item.get('network_name', '')).strip()
        if not network_name:
            network_name = str(cfg.network.name or '').strip()
        if not network_name:
            network_name = 'aivm-net'
        reg.vms.append(VMEntry(name=name, network_name=network_name, cfg=cfg))

    for item in raw.get('attachments', []):
        if not isinstance(item, dict):
            continue
        host_path = str(item.get('host_path', '')).strip()
        vm_name = str(item.get('vm_name', '')).strip()
        if not host_path or not vm_name:
            continue
        reg.attachments.append(
            AttachmentEntry(
                host_path=_norm_dir(host_path),
                vm_name=vm_name,
                mode=str(item.get('mode', 'shared') or 'shared'),
                guest_dst=str(item.get('guest_dst', '')).strip(),
                tag=str(item.get('tag', '')).strip(),
            )
        )
    log.trace('Finish load store')
    return reg


def save_store(reg: Store, path: Path | None = None) -> Path:
    fpath = path or store_path()
    fpath.parent.mkdir(parents=True, exist_ok=True)
    lines: list[str] = [f'schema_version = {reg.schema_version}']
    lines.append(f'active_vm = "{_toml_escape(reg.active_vm)}"')
    lines.append('')

    if reg.defaults is not None:
        d = asdict(reg.defaults)
        verbosity = int(d.get('verbosity', 1))
        if verbosity != 1:
            lines.append('[defaults]')
            lines.append(f'verbosity = {verbosity}')
            lines.append('')
        for section in (
            'vm',
            'network',
            'firewall',
            'image',
            'provision',
            'sync',
            'paths',
        ):
            body = d.get(section, {})
            if not isinstance(body, dict):
                continue
            lines.append(f'[defaults.{section}]')
            for k, v in body.items():
                _emit_toml_kv(lines, k, v)
            lines.append('')

    for net in sorted(reg.networks, key=lambda n: n.name):
        lines.append('[[networks]]')
        lines.append(f'name = "{_toml_escape(net.name)}"')
        net_d = asdict(net.network)
        lines.append('[networks.network]')
        for k, v in net_d.items():
            if k == 'name':
                continue
            _emit_toml_kv(lines, k, v)
        fw_d = asdict(net.firewall)
        lines.append('[networks.firewall]')
        for k, v in fw_d.items():
            _emit_toml_kv(lines, k, v)
        lines.append('')

    for vm in sorted(reg.vms, key=lambda v: v.name):
        lines.append('[[vms]]')
        lines.append(f'name = "{_toml_escape(vm.name)}"')
        lines.append(f'network_name = "{_toml_escape(vm.network_name)}"')
        d = asdict(vm.cfg)
        verbosity = int(d.get('verbosity', 1))
        if verbosity != 1:
            lines.append(f'verbosity = {verbosity}')
        for section in ('vm', 'image', 'provision', 'sync', 'paths'):
            body = d.get(section, {})
            if not isinstance(body, dict):
                continue
            lines.append(f'[vms.{section}]')
            for k, v in body.items():
                _emit_toml_kv(lines, k, v)
        lines.append('')

    for att in sorted(reg.attachments, key=lambda a: (a.host_path, a.vm_name)):
        lines.append('[[attachments]]')
        lines.append(f'host_path = "{_toml_escape(att.host_path)}"')
        lines.append(f'vm_name = "{_toml_escape(att.vm_name)}"')
        lines.append(f'mode = "{_toml_escape(att.mode)}"')
        lines.append(f'guest_dst = "{_toml_escape(att.guest_dst)}"')
        lines.append(f'tag = "{_toml_escape(att.tag)}"')
        lines.append('')

    fpath.write_text('\n'.join(lines).rstrip() + '\n', encoding='utf-8')
    return fpath


def upsert_vm(reg: Store, cfg: AgentVMConfig) -> None:
    upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
    upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)


def upsert_vm_with_network(
    reg: Store, cfg: AgentVMConfig, *, network_name: str
) -> None:
    cfg = cfg.expanded_paths()
    name = cfg.vm.name
    net_name = str(network_name or '').strip()
    if not net_name:
        net_name = str(cfg.network.name or '').strip() or 'aivm-net'
    rec = VMEntry(name=name, network_name=net_name, cfg=cfg)
    existing = [v for v in reg.vms if v.name == name]
    if existing:
        i = reg.vms.index(existing[0])
        reg.vms[i] = rec
    else:
        reg.vms.append(rec)
    reg.active_vm = name


def find_vm(reg: Store, vm_name: str) -> VMEntry | None:
    for rec in reg.vms:
        if rec.name == vm_name:
            return rec
    return None


def find_network(reg: Store, network_name: str) -> NetworkEntry | None:
    for rec in reg.networks:
        if rec.name == network_name:
            return rec
    return None


def upsert_network(
    reg: Store,
    *,
    network: NetworkConfig,
    firewall: FirewallConfig | None = None,
    name: str | None = None,
) -> None:
    net_name = str(name or network.name or '').strip()
    if not net_name:
        raise RuntimeError('network name must be non-empty')
    net = NetworkConfig(**asdict(network))
    net.name = net_name
    fw = (
        FirewallConfig(**asdict(firewall))
        if firewall is not None
        else FirewallConfig()
    )
    rec = NetworkEntry(name=net_name, network=net, firewall=fw)
    existing = [n for n in reg.networks if n.name == net_name]
    if existing:
        i = reg.networks.index(existing[0])
        reg.networks[i] = rec
    else:
        reg.networks.append(rec)


def remove_network(reg: Store, network_name: str) -> bool:
    existing = [n for n in reg.networks if n.name == network_name]
    if not existing:
        return False
    reg.networks = [n for n in reg.networks if n.name != network_name]
    return True


def network_users(reg: Store, network_name: str) -> list[str]:
    return sorted(v.name for v in reg.vms if v.network_name == network_name)


def materialize_vm_cfg(reg: Store, vm_name: str) -> AgentVMConfig:
    vm = find_vm(reg, vm_name)
    if vm is None:
        raise RuntimeError(f'VM not found in config store: {vm_name}')
    net = find_network(reg, vm.network_name)
    if net is None:
        raise RuntimeError(
            f"VM '{vm_name}' references unknown network '{vm.network_name}'. "
            'Define it under [[networks]].'
        )
    cfg = vm.cfg.expanded_paths()
    cfg.network = NetworkConfig(**asdict(net.network))
    cfg.firewall = FirewallConfig(**asdict(net.firewall))
    cfg.network.name = net.name
    return cfg


def remove_vm(
    reg: Store, vm_name: str, *, remove_attachments: bool = True
) -> bool:
    existing = [v for v in reg.vms if v.name == vm_name]
    if not existing:
        return False
    reg.vms = [v for v in reg.vms if v.name != vm_name]
    if remove_attachments:
        reg.attachments = [a for a in reg.attachments if a.vm_name != vm_name]
    if reg.active_vm == vm_name:
        reg.active_vm = reg.vms[0].name if reg.vms else ''
    return True


def upsert_attachment(
    reg: Store,
    *,
    host_path: str | Path,
    vm_name: str,
    mode: str = 'shared',
    guest_dst: str = '',
    tag: str = '',
    force: bool = False,
) -> None:
    norm = _norm_dir(host_path)
    conflict = [
        a
        for a in reg.attachments
        if a.host_path == norm and a.vm_name != vm_name
    ]
    if conflict and not force:
        vm_names = ', '.join(sorted({a.vm_name for a in conflict}))
        raise RuntimeError(
            f'Host folder already attached to other VM(s): {vm_names}. '
            'Use --force to override this safety check.'
        )
    if force and conflict:
        reg.attachments = [a for a in reg.attachments if a.host_path != norm]
    existing = [
        a
        for a in reg.attachments
        if a.host_path == norm and a.vm_name == vm_name
    ]
    rec = AttachmentEntry(
        host_path=norm,
        vm_name=vm_name,
        mode=mode,
        guest_dst=guest_dst,
        tag=tag,
    )
    if existing:
        i = reg.attachments.index(existing[0])
        reg.attachments[i] = rec
    else:
        reg.attachments.append(rec)


def find_attachment(
    reg: Store, host_path: str | Path
) -> AttachmentEntry | None:
    norm = _norm_dir(host_path)
    for att in reg.attachments:
        if att.host_path == norm:
            return att
    return None
