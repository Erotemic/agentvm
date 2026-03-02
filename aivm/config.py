"""Configuration data model and TOML serialization helpers for VM settings."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomllib

from .util import expand

DEFAULT_UBUNTU_NOBLE_IMG_URL = 'https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img'


@dataclass
class NetworkConfig:
    name: str = 'aivm-net'
    bridge: str = 'virbr-aivm'
    subnet_cidr: str = '10.77.0.0/24'
    gateway_ip: str = '10.77.0.1'
    dhcp_start: str = '10.77.0.100'
    dhcp_end: str = '10.77.0.200'


@dataclass
class FirewallConfig:
    enabled: bool = True
    table: str = 'aivm_sandbox'
    block_cidrs: list[str] = field(
        default_factory=lambda: [
            '0.0.0.0/8',
            '10.0.0.0/8',
            '100.64.0.0/10',
            '127.0.0.0/8',
            '169.254.0.0/16',
            '172.16.0.0/12',
            '192.168.0.0/16',
            '224.0.0.0/4',
            '240.0.0.0/4',
        ]
    )
    extra_block_cidrs: list[str] = field(default_factory=list)
    allow_tcp_ports: list[int] = field(default_factory=list)
    allow_udp_ports: list[int] = field(default_factory=list)


@dataclass
class ImageConfig:
    ubuntu_img_url: str = DEFAULT_UBUNTU_NOBLE_IMG_URL
    cache_name: str = 'noble-base.img'
    redownload: bool = False


@dataclass
class VMConfig:
    name: str = 'aivm-2404'
    user: str = 'agent'
    cpus: int = 4
    ram_mb: int = 8192
    disk_gb: int = 40
    allow_password_login: bool = False
    password: str = 'agent'


@dataclass
class ProvisionConfig:
    enabled: bool = True
    install_docker: bool = True
    packages: list[str] = field(
        default_factory=lambda: [
            'git',
            'jq',
            'ripgrep',
            'fd-find',
            'tmux',
            'htop',
            'unzip',
            'ca-certificates',
            'curl',
        ]
    )


@dataclass
class SyncConfig:
    enabled: bool = False
    overwrite: bool = True
    paths: list[str] = field(
        default_factory=lambda: [
            '~/.gitconfig',
            '~/.gitignore',
            '~/.config/Code/User/settings.json',
            '~/.config/Code/User/keybindings.json',
            '~/.tmux.conf',
            '~/.bashrc',
        ]
    )


@dataclass
class PathsConfig:
    base_dir: str = '/var/lib/libvirt/aivm'
    state_dir: str = '~/.cache/aivm'
    ssh_identity_file: str = ''
    ssh_pubkey_path: str = ''


@dataclass
class AgentVMConfig:
    vm: VMConfig = field(default_factory=VMConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    provision: ProvisionConfig = field(default_factory=ProvisionConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    verbosity: int = 1

    def expanded_paths(self) -> 'AgentVMConfig':
        self.paths.base_dir = expand(self.paths.base_dir)
        self.paths.state_dir = expand(self.paths.state_dir)
        self.paths.ssh_identity_file = (
            expand(self.paths.ssh_identity_file)
            if self.paths.ssh_identity_file
            else ''
        )
        self.paths.ssh_pubkey_path = (
            expand(self.paths.ssh_pubkey_path)
            if self.paths.ssh_pubkey_path
            else ''
        )
        self.sync.paths = [expand(p) for p in self.sync.paths]
        return self


def _toml_escape(s: str) -> str:
    return s.replace('\\', '\\\\').replace('"', '\\"')


def dump_toml(cfg: AgentVMConfig) -> str:
    d = asdict(cfg)
    lines: list[str] = []
    verbosity = d.get('verbosity', 1)
    if isinstance(verbosity, int) and verbosity != 1:
        lines.append(f'verbosity = {verbosity}')
        lines.append('')
    for section, body in d.items():
        if section == 'verbosity':
            continue
        if isinstance(body, dict):
            lines.append(f'[{section}]')
            for k, v in body.items():
                if isinstance(v, bool):
                    lines.append(f'{k} = {"true" if v else "false"}')
                elif isinstance(v, int):
                    lines.append(f'{k} = {v}')
                elif isinstance(v, list):
                    parts = [f'"{_toml_escape(str(item))}"' for item in v]
                    lines.append(f'{k} = [{", ".join(parts)}]')
                else:
                    lines.append(f'{k} = "{_toml_escape(str(v))}"')
            lines.append('')
    return '\n'.join(lines).rstrip() + '\n'


def load(path: Path) -> AgentVMConfig:
    raw = tomllib.loads(path.read_text(encoding='utf-8'))
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
        if section in raw and isinstance(raw[section], dict):
            sec = raw[section]
            obj = getattr(cfg, section)
            for k, v in sec.items():
                if hasattr(obj, k):
                    setattr(obj, k, v)
    if 'verbosity' in raw:
        cfg.verbosity = raw['verbosity']
    return cfg


def save(path: Path, cfg: AgentVMConfig) -> None:
    path.write_text(dump_toml(cfg), encoding='utf-8')
