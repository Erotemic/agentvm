"""Guest-side attachment helpers: symlinks, git clone, and SSH config management."""

from __future__ import annotations

import hashlib
import re
import shlex
from pathlib import Path, PurePosixPath

from loguru import logger

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args
from ..util import ensure_dir
from ..vm import ensure_share_mounted
from ..vm import ssh_config as mk_ssh_config
from ..vm.share import ResolvedAttachment
from .resolve import (
    ATTACHMENT_ACCESS_RO,
    ATTACHMENT_MODE_GIT,
    ATTACHMENT_MODE_SHARED,
    ATTACHMENT_MODE_SHARED_ROOT,
    _compute_mirror_home_symlink,
    _default_primary_guest_dst,
    _host_symlink_lexical_path,
)
from .shared_root import (
    _ensure_shared_root_guest_bind,
    _ensure_shared_root_host_bind,
    _ensure_shared_root_vm_mapping,
)

log = logger


def _ensure_guest_symlink(
    cfg: AgentVMConfig,
    ip: str,
    *,
    symlink_path: str,
    target_path: str,
) -> None:
    """Safely ensure a symlink exists at symlink_path pointing to target_path on the guest.

    Safety rules:
    - path does not exist: create symlink
    - already a correct symlink: no-op
    - empty directory: remove and replace with symlink
    - non-empty directory: warn and skip
    - regular file: warn and skip
    - symlink to wrong target: warn and skip
    """
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    link_q = shlex.quote(symlink_path)
    tgt_q = shlex.quote(target_path)
    parent_q = shlex.quote(str(PurePosixPath(symlink_path).parent))
    # Use sudo for both mkdir and ln -s so companion symlinks work even when
    # the parent dir is not writable by the guest user (e.g. /home/joncrall/...).
    script = (
        'set -euo pipefail; '
        f'if [ -L {link_q} ]; then '
        f'  cur=$(readlink {link_q}); '
        f'  if [ "$cur" = {tgt_q} ]; then exit 0; fi; '
        f'  echo "aivm-symlink-warn: {symlink_path} is a symlink to $cur not {target_path}; skipping" >&2; '
        'exit 3; '
        f'elif [ -d {link_q} ]; then '
        f'  if [ -n "$(find {link_q} -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then '
        f'    echo "aivm-symlink-warn: {symlink_path} is a non-empty directory; skipping" >&2; '
        '    exit 4; '
        '  fi; '
        f'  sudo -n rmdir {link_q}; '
        f'  sudo -n mkdir -p {parent_q}; '
        f'  sudo -n ln -s {tgt_q} {link_q}; '
        f'elif [ -e {link_q} ]; then '
        f'  echo "aivm-symlink-warn: {symlink_path} is a regular file; skipping" >&2; '
        'exit 5; '
        'else '
        f'  sudo -n mkdir -p {parent_q}; '
        f'  sudo -n ln -s {tgt_q} {link_q}; '
        'fi'
    )
    cmd = [
        'ssh',
        *ssh_base_args(ident, strict_host_key_checking='accept-new'),
        f'{cfg.vm.user}@{ip}',
        script,
    ]
    res = CommandManager.current().run(cmd, sudo=False, check=False, capture=True)
    if res.code not in (0, 3, 4, 5):
        log.warning(
            'Guest symlink setup failed for {} -> {}: {}',
            symlink_path,
            target_path,
            (res.stderr or res.stdout or '').strip(),
        )
        return
    stderr = (res.stderr or '').strip()
    if stderr and 'aivm-symlink-warn' in stderr:
        log.warning('{}', stderr.replace('aivm-symlink-warn: ', ''))


def _apply_guest_derived_symlinks(
    cfg: AgentVMConfig,
    ip: str,
    host_src: Path,
    attachment: ResolvedAttachment,
    *,
    mirror_home: bool,
) -> None:
    """Create companion and mirror-home symlinks in the guest after attachment.

    Three cases are handled:
    1. Companion symlink: if host_src is a symlink on the host, create a guest
       symlink at the lexical path pointing to the resolved guest_dst.
    2. Mirror-home (lexical): if mirror_home is enabled and the lexical host
       path is under the host home, create a symlink under the guest home at
       the same relative position.
    3. Mirror-home (resolved): when host_src is a symlink, also apply the
       mirror-home rule independently to the resolved host path, so both the
       lexical and resolved relative paths under the guest home point to guest_dst.
    """
    guest_dst = attachment.guest_dst
    lexical = _host_symlink_lexical_path(host_src)

    # 1. Companion symlink at the lexical guest path -> resolved guest_dst.
    if lexical is not None and lexical != guest_dst:
        _ensure_guest_symlink(
            cfg,
            ip,
            symlink_path=lexical,
            target_path=guest_dst,
        )

    if not mirror_home:
        return

    # 2. Mirror-home for the lexical host path.
    is_default_dst = guest_dst == _default_primary_guest_dst(host_src)
    mirror_path = _compute_mirror_home_symlink(
        cfg, host_src, guest_dst, is_default_dst=is_default_dst
    )
    if mirror_path is not None:
        _ensure_guest_symlink(
            cfg,
            ip,
            symlink_path=mirror_path,
            target_path=guest_dst,
        )

    # 3. Mirror-home for the resolved host path (only when host_src is a symlink
    #    and the attachment did not use an explicit custom guest_dst).
    if lexical is not None and is_default_dst:
        resolved_src = host_src.resolve()
        resolved_mirror = _compute_mirror_home_symlink(
            cfg, resolved_src, guest_dst, is_default_dst=True
        )
        if resolved_mirror is not None and resolved_mirror != mirror_path:
            _ensure_guest_symlink(
                cfg,
                ip,
                symlink_path=resolved_mirror,
                target_path=guest_dst,
            )


def _upsert_ssh_config_entry(
    cfg: AgentVMConfig, *, dry_run: bool = False, yes: bool = False
) -> tuple[Path, bool]:
    cfg = cfg.expanded_paths()
    ssh_dir = Path.home() / '.ssh'
    ssh_cfg = ssh_dir / 'config'
    block_name = cfg.vm.name
    new_block = (
        f'# >>> aivm:{block_name} >>>\n'
        f'{mk_ssh_config(cfg).rstrip()}\n'
        f'# <<< aivm:{block_name} <<<\n'
    )
    if dry_run:
        log.info(
            'DRYRUN: update SSH config block for host {} in {}',
            block_name,
            ssh_cfg,
        )
        return ssh_cfg, False
    ensure_dir(ssh_dir)
    existing = ssh_cfg.read_text(encoding='utf-8') if ssh_cfg.exists() else ''
    pattern = re.compile(
        rf'(?ms)^# >>> aivm:{re.escape(block_name)} >>>\n.*?^# <<< aivm:{re.escape(block_name)} <<<\n?'
    )
    if pattern.search(existing):
        updated = pattern.sub(new_block, existing)
    else:
        sep = '' if not existing or existing.endswith('\n') else '\n'
        updated = f'{existing}{sep}{new_block}'
    if updated == existing:
        log.debug(
            "SSH config entry for host '{}' already up to date in {}",
            block_name,
            ssh_cfg,
        )
        return ssh_cfg, False
    CommandManager.current().confirm_file_update(
        yes=bool(yes),
        path=ssh_cfg,
        purpose=f"Update SSH config entry for host '{block_name}'.",
    )
    log.info('Writing SSH config entry to {}', ssh_cfg)
    ssh_cfg.write_text(updated, encoding='utf-8')
    return ssh_cfg, True


def _ensure_attachment_available_in_guest(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    ip: str,
    *,
    yes: bool,
    dry_run: bool,
    ensure_shared_root_host_side: bool,
    allow_disruptive_shared_root_rebind: bool = True,
    mirror_home: bool = False,
) -> None:
    """Make an attachment available at its guest destination for a running VM.

    This dispatcher hides the mode-specific reconciliation details from higher
    level workflows such as ``aivm attach``, ``aivm ssh``, and ``aivm code``.
    Depending on the attachment mode it will:

    * ensure a standard virtiofs share is mounted in the guest,
    * reconcile shared-root host and guest bind mounts, or
    * synchronize a git-mode clone into the guest.

    The shared-root flags allow callers to distinguish between explicit attach
    flows, where disruptive host-side repair is acceptable, and automatic
    restore flows, where we prefer non-disruptive verification before touching
    an existing bind mount.

    If ``mirror_home`` is True and behavior conditions are met, a companion
    symlink under the guest home mirroring the host-home-relative path is created.
    """
    mgr = CommandManager.current()
    if attachment.mode == ATTACHMENT_MODE_SHARED:
        ensure_share_mounted(
            cfg,
            ip,
            guest_dst=attachment.guest_dst,
            tag=attachment.tag,
            read_only=(attachment.access == ATTACHMENT_ACCESS_RO),
            dry_run=dry_run,
        )
    elif attachment.mode == ATTACHMENT_MODE_SHARED_ROOT:
        with mgr.intent(
            'Attach and reconcile shared-root mapping',
            why='Ensure the requested host folder is exposed to the VM and bound to the requested guest destination.',
            role='modify',
        ):
            if ensure_shared_root_host_side:
                _ensure_shared_root_host_bind(
                    cfg,
                    attachment,
                    yes=bool(yes),
                    dry_run=dry_run,
                    allow_disruptive_rebind=allow_disruptive_shared_root_rebind,
                )
                _ensure_shared_root_vm_mapping(
                    cfg,
                    yes=bool(yes),
                    dry_run=dry_run,
                    vm_running=True,
                )
            _ensure_shared_root_guest_bind(
                cfg,
                ip,
                attachment,
                dry_run=dry_run,
            )
    else:
        _ensure_git_clone_attachment(
            cfg,
            host_src,
            attachment,
            ip,
            yes=bool(yes),
            dry_run=dry_run,
        )

    if dry_run:
        return

    # After primary attachment is ready, create companion and mirror-home symlinks.
    _apply_guest_derived_symlinks(
        cfg,
        ip,
        host_src,
        attachment,
        mirror_home=mirror_home,
    )


def _git_repo_context(host_src: Path) -> tuple[Path, Path]:
    probe = CommandManager.current().run(
        ['git', '-C', str(host_src), 'rev-parse', '--show-toplevel'],
        sudo=False,
        check=False,
        capture=True,
    )
    if probe.code != 0:
        raise RuntimeError(
            f'Git attachment mode requires a Git worktree: {host_src}'
        )
    repo_root = Path((probe.stdout or '').strip()).resolve()
    rel = host_src.resolve().relative_to(repo_root)
    return repo_root, rel


def _guest_repo_root_for_attachment(
    attachment: ResolvedAttachment, repo_rel: Path
) -> str:
    guest_target = PurePosixPath(attachment.guest_dst)
    guest_root = guest_target
    for _ in repo_rel.parts:
        guest_root = guest_root.parent
    return str(guest_root)


def _git_attachment_remote_name(cfg: AgentVMConfig, repo_root: Path) -> str:
    stem = re.sub(r'[^a-z0-9]+', '-', cfg.vm.name.lower()).strip('-') or 'vm'
    digest = hashlib.sha1(str(repo_root).encode('utf-8')).hexdigest()[:8]
    return f'aivm-{stem}-{digest}'


def _upsert_host_git_remote(
    repo_root: Path,
    *,
    remote_name: str,
    remote_url: str,
    yes: bool,
) -> tuple[Path, bool]:
    """Ensure a host Git remote exists with the requested URL.

    "Upsert" means insert+update: update if the remote already exists,
    otherwise register it.  Returns ``(git_config_path, changed)`` where
    ``changed`` is ``True`` only when this function adds or updates the remote
    entry.
    """
    mgr = CommandManager.current()
    git_dir_probe = mgr.run(
        [
            'git',
            '-C',
            str(repo_root),
            'rev-parse',
            '--path-format=absolute',
            '--git-common-dir',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if git_dir_probe.code != 0:
        msg = (git_dir_probe.stderr or git_dir_probe.stdout).strip()
        raise RuntimeError(
            'Could not locate Git config for host repository.\n'
            f'Repo: {repo_root}\n'
            f'Git said: {msg}'
        )
    git_cfg = Path((git_dir_probe.stdout or '').strip()) / 'config'
    probe = mgr.run(
        ['git', '-C', str(repo_root), 'remote', 'get-url', remote_name],
        sudo=False,
        check=False,
        capture=True,
    )
    existing_url = (probe.stdout or '').strip() if probe.code == 0 else ''
    if existing_url == remote_url:
        return git_cfg, False
    if existing_url:
        purpose = (
            f"Update Git remote '{remote_name}' URL from '{existing_url}' to "
            f"'{remote_url}'."
        )
        cmd = [
            'git',
            '-C',
            str(repo_root),
            'remote',
            'set-url',
            remote_name,
            remote_url,
        ]
    else:
        purpose = (
            f"Register Git remote '{remote_name}' with URL '{remote_url}'."
        )
        cmd = [
            'git',
            '-C',
            str(repo_root),
            'remote',
            'add',
            remote_name,
            remote_url,
        ]
    mgr.confirm_file_update(
        yes=bool(yes),
        path=git_cfg,
        purpose=purpose,
    )
    mgr.run(cmd, sudo=False, check=True, capture=True)
    return git_cfg, True


def _warn_if_git_repo_dirty(repo_root: Path) -> None:
    dirty = CommandManager.current().run(
        ['git', '-C', str(repo_root), 'status', '--porcelain'],
        sudo=False,
        check=False,
        capture=True,
    )
    if (dirty.stdout or '').strip():
        print(
            f'Warning: host repo {repo_root} has uncommitted changes; Git attachment sync only transfers committed branch state.'
        )


def _git_current_branch(repo_root: Path) -> str:
    branch = CommandManager.current().run(
        ['git', '-C', str(repo_root), 'rev-parse', '--abbrev-ref', 'HEAD'],
        sudo=False,
        check=False,
        capture=True,
    )
    if branch.code != 0:
        msg = (branch.stderr or branch.stdout).strip()
        raise RuntimeError(
            'Could not determine current Git branch for attachment sync.\n'
            f'Repo: {repo_root}\n'
            f'Git said: {msg}'
        )
    name = (branch.stdout or '').strip()
    if not name or name == 'HEAD':
        raise RuntimeError(
            f'Git attachment mode requires a named branch in {repo_root}; detached HEAD is not supported.'
        )
    return name


def _ensure_guest_git_repo(
    cfg: AgentVMConfig,
    guest_repo_root: str,
    branch: str,
) -> None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    root_q = shlex.quote(guest_repo_root)
    user_q = shlex.quote(cfg.vm.user)
    # Use sudo to create the full repo root path in case the parent dirs are
    # outside the guest home and not user-writable (e.g. /home/joncrall/code/repo).
    # Only chown the repo root leaf itself — never recursively chown parent trees.
    script = (
        f'if ! mkdir -p {root_q} 2>/dev/null; then '
        f'sudo -n mkdir -p {root_q} && sudo -n chown {user_q}:{user_q} {root_q}; fi && '
        f'if [ ! -d {shlex.quote(guest_repo_root + "/.git")} ]; then '
        f'if [ -n "$(find {root_q} -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then '
        f'echo "guest target directory is not empty and is not a git repo: {guest_repo_root}" >&2; '
        f'exit 2; '
        f'fi; '
        f'git init {root_q} >/dev/null; '
        f'fi && '
        f'git -C {root_q} config receive.denyCurrentBranch updateInstead && '
        f'git -C {root_q} symbolic-ref HEAD refs/heads/{shlex.quote(branch)}'
    )
    res = CommandManager.current().run(
        [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            cfg.vm.name,
            script,
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if res.code != 0:
        raise RuntimeError(
            'Failed to prepare guest Git repo for attachment sync.\n'
            f'Guest repo: {guest_repo_root}\n'
            f'Error: {(res.stderr or res.stdout).strip()}'
        )


def _push_host_repo_to_guest(
    repo_root: Path,
    *,
    remote_name: str,
    branch: str,
) -> None:
    push = CommandManager.current().run(
        [
            'git',
            '-C',
            str(repo_root),
            'push',
            remote_name,
            f'HEAD:refs/heads/{branch}',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if push.code != 0:
        msg = (push.stderr or push.stdout).strip()
        if 'working tree has unstaged or staged changes' in msg.lower():
            raise RuntimeError(
                'Guest Git repo rejected host push because its working tree is not clean.\n'
                f'Remote: {remote_name}\n'
                f'Branch: {branch}\n'
                f'Git said: {msg}'
            )
        raise RuntimeError(
            'Failed to push host branch into guest Git repo.\n'
            f'Remote: {remote_name}\n'
            f'Branch: {branch}\n'
            f'Git said: {msg}'
        )


def _ensure_git_clone_attachment(
    cfg: AgentVMConfig,
    host_src: Path,
    attachment: ResolvedAttachment,
    ip: str,
    *,
    yes: bool,
    dry_run: bool,
) -> tuple[Path, str, str]:
    del ip
    repo_root, repo_rel = _git_repo_context(host_src)
    branch = _git_current_branch(repo_root)
    guest_repo_root = _guest_repo_root_for_attachment(attachment, repo_rel)
    remote_name = _git_attachment_remote_name(cfg, repo_root)
    remote_url = f'{cfg.vm.name}:{guest_repo_root}'
    ssh_cfg, _ = _upsert_ssh_config_entry(cfg, dry_run=dry_run, yes=yes)
    git_cfg, _ = _upsert_host_git_remote(
        repo_root,
        remote_name=remote_name,
        remote_url=remote_url,
        yes=yes,
    )
    if dry_run:
        return repo_root, ssh_cfg.as_posix(), git_cfg.as_posix()

    _warn_if_git_repo_dirty(repo_root)
    _ensure_guest_git_repo(cfg, guest_repo_root, branch)
    _push_host_repo_to_guest(
        repo_root,
        remote_name=remote_name,
        branch=branch,
    )
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    final_probe = CommandManager.current().run(
        [
            'ssh',
            *ssh_base_args(ident, strict_host_key_checking='accept-new'),
            cfg.vm.name,
            f'test -e {shlex.quote(attachment.guest_dst)}',
        ],
        sudo=False,
        check=False,
        capture=True,
    )
    if final_probe.code != 0:
        raise RuntimeError(
            'Guest Git sync completed, but the requested path is missing inside the guest repo.\n'
            f'Host source: {host_src}\n'
            f'Guest path: {attachment.guest_dst}\n'
            'If this path only exists in uncommitted host changes, commit them before using git attachment mode.'
        )
    return repo_root, str(ssh_cfg), str(git_cfg)
