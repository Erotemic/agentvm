"""Configuration schema and TOML serialization helpers.

These dataclasses describe user-facing config knobs. ``store.py`` composes them
into the global config registry format.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path

import tomllib
from loguru import logger as log

from .util import expand

# TODO(design): replace the ad-hoc URL + hash globals here with a network asset
# dataclass/registry that can describe the primary URL, SHA256, mirrors,
# torrent magnet, and IPFS CID for a pinned image artifact.
# Pinned daily image path so URL and hash are coupled to a specific artifact.
DEFAULT_UBUNTU_NOBLE_IMG_URL = 'https://cloud-images.ubuntu.com/noble/20260225/noble-server-cloudimg-amd64.img'
SUPPORTED_IMAGE_SHA256 = {
    DEFAULT_UBUNTU_NOBLE_IMG_URL: '7aa6d9f5e8a3a55c7445b138d31a73d1187871211b2b7da9da2e1a6cbf169b21',
}


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
    # TODO(design): shift from name-based cache identity toward digest-based
    # identity so content-addressable image fallback paths are first-class.
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
class BehaviorConfig:
    yes_sudo: bool = False
    auto_approve_readonly_sudo: bool = True
    verbose: int = 1


@dataclass
class PassthroughConfig:
    pci_devices: list[str] = field(default_factory=list)
    host_prepare_mode: str = 'none'
    host_prepare_applied: bool = False
    persistent_hostdev_applied: bool = False
    selector_label: str = ''


@dataclass
class AgentVMConfig:
    vm: VMConfig = field(default_factory=VMConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    firewall: FirewallConfig = field(default_factory=FirewallConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    provision: ProvisionConfig = field(default_factory=ProvisionConfig)
    sync: SyncConfig = field(default_factory=SyncConfig)
    paths: PathsConfig = field(default_factory=PathsConfig)
    passthrough: PassthroughConfig = field(default_factory=PassthroughConfig)
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
        'passthrough',
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
    log.info('Writing VM config to {}', path)
    path.write_text(dump_toml(cfg), encoding='utf-8')
