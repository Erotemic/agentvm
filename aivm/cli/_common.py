"""Shared CLI resolution, sudo confirmation, and config-store helpers."""

from __future__ import annotations

import os
import sys
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Self, cast

import scriptconfig as scfg
from loguru import logger

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..detect import detect_ssh_identity
from ..store import (
    find_attachments,
    find_vm,
    load_store,
    materialize_vm_cfg,
    save_store,
    store_path,
    upsert_network,
    upsert_vm_with_network,
)
from ..util import (
    which,
)

log = logger
_LAST_LOGGING_STATE: tuple[str, bool, int] | None = None
_CURRENT_YES_SUDO: ContextVar[bool] = ContextVar(
    'aivm_current_yes_sudo', default=False
)
_CURRENT_AUTO_APPROVE_READONLY_SUDO: ContextVar[bool] = ContextVar(
    'aivm_current_auto_approve_readonly_sudo', default=True
)


class _BaseCommand(scfg.DataConfig):
    """Base options shared by all commands."""

    __special_options__ = False

    config: Any = scfg.Value(
        None,
        help='Path to global aivm config store (default: ~/.config/aivm/config.toml).',
    )
    verbose: Any = scfg.Value(
        0,
        short_alias=['v'],
        isflag='counter',
        help='Increase verbosity (-v, -vv).',
    )
    yes: Any = scfg.Value(
        False,
        isflag=True,
        help='Auto-approve interactive confirmations.',
    )
    yes_sudo: Any = scfg.Value(
        False,
        isflag=True,
        help='Auto-approve sudo confirmation prompts only.',
    )

    @classmethod
    def cli(cls, *args: Any, **kwargs: Any) -> Self:  # type: ignore
        # NOTE: these getattrs are to make the type checker happy, and we
        # should try to make this less ugly
        parsed = cast(Self, super().cli(*args, **kwargs))
        cfg_verbosity = _resolve_cfg_verbosity(getattr(parsed, 'config', None))
        cfg_yes_sudo = _resolve_cfg_yes_sudo(getattr(parsed, 'config', None))
        cfg_auto_approve_readonly_sudo = (
            _resolve_cfg_auto_approve_readonly_sudo(
                getattr(parsed, 'config', None)
            )
        )
        effective_yes_sudo = bool(
            getattr(parsed, 'yes_sudo', False)
            or getattr(parsed, 'yes', False)
            or cfg_yes_sudo
        )
        setattr(parsed, 'yes_sudo', effective_yes_sudo)
        _CURRENT_YES_SUDO.set(effective_yes_sudo)
        _CURRENT_AUTO_APPROVE_READONLY_SUDO.set(
            bool(cfg_auto_approve_readonly_sudo)
        )
        CommandManager.activate(
            CommandManager(
                yes=bool(getattr(parsed, 'yes', False)),
                yes_sudo=bool(effective_yes_sudo),
                auto_approve_readonly_sudo=bool(cfg_auto_approve_readonly_sudo),
            )
        )
        args_verbose = int(getattr(parsed, 'verbose', 0) or 0)
        _setup_logging(args_verbose, cfg_verbosity)
        log.trace(
            'Parsed command {} with config={} verbose={} yes={} yes_sudo={} auto_approve_readonly_sudo={}',
            cls.__name__,
            getattr(parsed, 'config', None),
            args_verbose,
            bool(getattr(parsed, 'yes', False)),
            bool(getattr(parsed, 'yes_sudo', False)),
            bool(cfg_auto_approve_readonly_sudo),
        )
        return parsed


def _cfg_path(p: str | None) -> Path:
    return Path(p).expanduser().resolve() if p else store_path().resolve()


def _resolve_cfg_verbosity(config_opt: str | None) -> int:
    cfg_verbosity = 1
    try:
        path = _cfg_path(config_opt)
        if path.exists():
            reg = load_store(path)
            behavior_verbose = int(getattr(reg.behavior, 'verbose', 1) or 1)
            if behavior_verbose != 1:
                cfg_verbosity = behavior_verbose
            elif reg.active_vm:
                rec = find_vm(reg, reg.active_vm)
                if rec is not None:
                    cfg_verbosity = int(rec.cfg.verbosity)
            elif reg.defaults is not None:
                cfg_verbosity = int(reg.defaults.verbosity)
    except Exception:
        cfg_verbosity = 1
    return cfg_verbosity


def _resolve_cfg_yes_sudo(config_opt: str | None) -> bool:
    cfg_yes_sudo = False
    try:
        path = _cfg_path(config_opt)
        if path.exists():
            reg = load_store(path)
            cfg_yes_sudo = bool(reg.behavior.yes_sudo)
    except Exception:
        cfg_yes_sudo = False
    return cfg_yes_sudo


def _resolve_cfg_auto_approve_readonly_sudo(config_opt: str | None) -> bool:
    auto_approve_readonly_sudo = True
    try:
        path = _cfg_path(config_opt)
        if path.exists():
            reg = load_store(path)
            auto_approve_readonly_sudo = bool(
                getattr(reg.behavior, 'auto_approve_readonly_sudo', True)
            )
    except Exception:
        auto_approve_readonly_sudo = True
    return auto_approve_readonly_sudo


def _setup_logging(args_verbose: int, cfg_verbosity: int) -> None:
    global _LAST_LOGGING_STATE
    effective_verbosity = args_verbose if args_verbose > 0 else cfg_verbosity
    level = 'WARNING'
    if effective_verbosity == 1:
        level = 'INFO'
    elif effective_verbosity == 2:
        level = 'DEBUG'
    elif effective_verbosity >= 3:
        level = 'TRACE'
    colorize = sys.stderr.isatty() and os.getenv('NO_COLOR') is None
    state = (level, colorize, id(sys.stderr))
    if _LAST_LOGGING_STATE == state:
        return
    logger.remove()
    logger.add(
        sys.stderr,
        level=level,
        colorize=colorize,
        format='<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>',
    )
    _LAST_LOGGING_STATE = state
    log.debug(
        'Logging configured at {} (effective_verbosity={}, colorize={})',
        level,
        effective_verbosity,
        colorize,
    )


def _hydrate_runtime_defaults(cfg: AgentVMConfig) -> bool:
    changed = False
    have_ident = bool((cfg.paths.ssh_identity_file or '').strip())
    have_pub = bool((cfg.paths.ssh_pubkey_path or '').strip())
    if have_ident and have_pub:
        return False
    ident, pub = detect_ssh_identity()
    if not have_ident and ident:
        cfg.paths.ssh_identity_file = ident
        changed = True
    if not have_pub and pub:
        cfg.paths.ssh_pubkey_path = pub
        changed = True
    if changed:
        log.debug(
            'Hydrated runtime defaults for vm={} ssh_identity_file={} ssh_pubkey_path={}',
            cfg.vm.name,
            cfg.paths.ssh_identity_file or '(empty)',
            cfg.paths.ssh_pubkey_path or '(empty)',
        )
    return changed


def _default_aivm_identity_paths() -> tuple[Path, Path]:
    priv = Path.home() / '.ssh' / 'id_aivm_ed25519'
    return priv, Path(str(priv) + '.pub')


def _maybe_offer_create_ssh_identity(
    cfg: AgentVMConfig,
    *,
    yes: bool,
    prompt_reason: str,
) -> bool:
    """Offer to create a dedicated aivm SSH keypair when none is configured."""
    ident = (cfg.paths.ssh_identity_file or '').strip()
    pub = (cfg.paths.ssh_pubkey_path or '').strip()
    ident_path = Path(ident).expanduser() if ident else None
    pub_path = Path(pub).expanduser() if pub else None
    ident_ok = ident_path is not None and ident_path.exists()
    pub_ok = pub_path is not None and pub_path.exists()
    if ident_ok and pub_ok:
        return False

    # Do not override a partially configured custom path automatically.
    if ident or pub:
        return False

    default_priv, default_pub = _default_aivm_identity_paths()
    if default_priv.exists() and default_pub.exists():
        cfg.paths.ssh_identity_file = str(default_priv)
        cfg.paths.ssh_pubkey_path = str(default_pub)
        return True

    if which('ssh-keygen') is None:
        log.warning(
            'ssh-keygen not found; cannot create dedicated aivm SSH identity.'
        )
        return False

    if yes:
        approved = True
    else:
        if not sys.stdin.isatty():
            return False
        ans = (
            input(
                'No SSH identity/public key was detected for aivm VM access. '
                f'Create a dedicated keypair now at {default_priv}? [Y/n]: '
            )
            .strip()
            .lower()
        )
        approved = ans in {'', 'y', 'yes'}
    if not approved:
        return False

    mgr = CommandManager.current()
    comment = f'aivm@{os.uname().nodename}'
    with mgr.intent(
        'Create SSH identity',
        why='A VM SSH keypair is required for guest access and provisioning.',
        role='modify',
    ):
        with mgr.step(
            'Create dedicated aivm SSH keypair',
            why=prompt_reason,
            approval_scope='aivm-ssh-identity',
        ):
            mgr.submit(
                ['mkdir', '-p', str(default_priv.parent)],
                sudo=False,
                role='modify',
                summary='Create ~/.ssh directory if missing',
                detail=f'target={default_priv.parent}',
            )
            mgr.submit(
                ['chmod', '700', str(default_priv.parent)],
                sudo=False,
                role='modify',
                summary='Ensure ~/.ssh directory permissions',
                detail=f'target={default_priv.parent}',
            )
            mgr.submit(
                [
                    'ssh-keygen',
                    '-q',
                    '-t',
                    'ed25519',
                    '-f',
                    str(default_priv),
                    '-N',
                    '',
                    '-C',
                    comment,
                ],
                sudo=False,
                role='modify',
                summary='Generate dedicated aivm SSH keypair',
                detail=f'private={default_priv} public={default_pub}',
            )
    cfg.paths.ssh_identity_file = str(default_priv)
    cfg.paths.ssh_pubkey_path = str(default_pub)
    log.info(
        'Configured dedicated aivm SSH identity for vm={} private={} public={}',
        cfg.vm.name,
        default_priv,
        default_pub,
    )
    return True


def _choose_vm_interactive(options: list[str], *, reason: str) -> str:
    if not sys.stdin.isatty():
        raise RuntimeError(
            f'VM selection is ambiguous ({reason}). Re-run with --vm.'
        )
    print(f'Multiple VMs match ({reason}). Select one:')
    for idx, item in enumerate(options, start=1):
        print(f'  {idx}. {item}')
    while True:
        raw = input('Select VM number: ').strip()
        if not raw.isdigit():
            print('Please enter a number.')
            continue
        choice = int(raw)
        if 1 <= choice <= len(options):
            return options[choice - 1]
        print(f'Please enter a number between 1 and {len(options)}.')


def _resolve_vm_name(
    *,
    config_opt: str | None,
    vm_opt: str,
    host_src: Path | None,
) -> tuple[str, Path]:
    """Resolve a VM name using CLI intent precedence.

    Precedence is deliberate:
    explicit ``--vm`` > folder attachment mapping > active VM > single VM >
    interactive selection. This keeps one-command workflows predictable while
    still allowing explicit override.
    """
    log.trace(
        'Resolving VM name config_opt={} vm_opt={} host_src={}',
        config_opt,
        vm_opt,
        host_src,
    )
    store_path = _cfg_path(config_opt)
    reg = load_store(store_path)

    if vm_opt:
        if find_vm(reg, vm_opt) is None:
            raise RuntimeError(f'VM not found in config store: {vm_opt}')
        return vm_opt, store_path

    if host_src is not None:
        atts = find_attachments(reg, host_src)
        if atts:
            attached_vm_names = sorted(
                {
                    att.vm_name
                    for att in atts
                    if find_vm(reg, att.vm_name) is not None
                }
            )
            if len(attached_vm_names) == 1:
                return attached_vm_names[0], store_path
            if attached_vm_names:
                if reg.active_vm in attached_vm_names:
                    return reg.active_vm, store_path
                if not sys.stdin.isatty():
                    vm_names = ', '.join(attached_vm_names)
                    raise RuntimeError(
                        'Host folder is attached to multiple VMs: '
                        f'{vm_names}. Re-run with --vm.'
                    )
                chosen = _choose_vm_interactive(
                    attached_vm_names,
                    reason=(
                        f'folder {host_src} is attached to '
                        f'{len(attached_vm_names)} VMs'
                    ),
                )
                return chosen, store_path

    if reg.active_vm and find_vm(reg, reg.active_vm) is not None:
        return reg.active_vm, store_path

    if len(reg.vms) == 1:
        return reg.vms[0].name, store_path

    if len(reg.vms) > 1:
        chosen = _choose_vm_interactive(
            [r.name for r in sorted(reg.vms, key=lambda x: x.name)],
            reason=f'{len(reg.vms)} configured VMs',
        )
        return chosen, store_path

    raise RuntimeError(
        f'No VM definitions found in config store: {store_path}. '
        'Run `aivm config init` then `aivm vm create` first.'
    )


def _load_cfg_with_path(
    config_path: str | None,
    *,
    vm_opt: str = '',
    host_src: Path | None = None,
    hydrate_runtime_defaults: bool = True,
    persist_runtime_defaults: bool = True,
) -> tuple[AgentVMConfig, Path]:
    log.trace(
        'Loading cfg with path config_path={} vm_opt={} host_src={}',
        config_path,
        vm_opt,
        host_src,
    )
    vm_name, store_path = _resolve_vm_name(
        config_opt=config_path,
        vm_opt=vm_opt,
        host_src=host_src,
    )
    reg = load_store(store_path)
    rec = find_vm(reg, vm_name)
    if rec is None:
        raise RuntimeError(f'VM not found in config store: {vm_name}')
    cfg = materialize_vm_cfg(reg, vm_name)
    changed = (
        _hydrate_runtime_defaults(cfg) if hydrate_runtime_defaults else False
    )
    if changed and persist_runtime_defaults:
        upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
        upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
        save_store(
            reg,
            store_path,
            reason=(
                f'Persist hydrated runtime defaults discovered while loading '
                f'VM {cfg.vm.name}.'
            ),
        )
    return cfg, store_path


def _load_cfg(config_path: str | None, *, vm_opt: str = '') -> AgentVMConfig:
    cfg, _ = _load_cfg_with_path(
        config_path,
        vm_opt=vm_opt,
        host_src=Path.cwd(),
    )
    return cfg


def _resolve_cfg_fallback(
    config_opt: str | None, *, vm_opt: str = ''
) -> tuple[AgentVMConfig, Path]:
    return _load_cfg_with_path(
        config_opt,
        vm_opt=vm_opt,
        host_src=Path.cwd(),
    )


def _record_vm(
    cfg: AgentVMConfig,
    store_file: Path | None = None,
    *,
    reason: str = '',
) -> Path:
    target = store_file or store_path()
    reg = load_store(target)
    upsert_network(reg, network=cfg.network, firewall=cfg.firewall)
    upsert_vm_with_network(reg, cfg, network_name=cfg.network.name)
    why = reason.strip() or f'Persist managed VM record for {cfg.vm.name}.'
    return save_store(reg, target, reason=why)


def _resolve_cfg_for_code(
    *,
    config_opt: str | None,
    vm_opt: str,
    host_src: Path,
) -> tuple[AgentVMConfig, Path]:
    """Resolve VM config for folder-oriented flows (``code``/``ssh``/``attach``)."""
    return _load_cfg_with_path(
        config_opt,
        vm_opt=vm_opt,
        host_src=host_src,
    )


@dataclass
class PreparedSession:
    cfg: AgentVMConfig
    cfg_path: Path
    host_src: Path
    attachment_mode: str
    share_source_dir: str
    share_tag: str
    share_guest_dst: str
    ip: str | None
    reg_path: Path | None
    meta_path: Path | None


__all__ = [name for name in globals() if not name.startswith('__')]
