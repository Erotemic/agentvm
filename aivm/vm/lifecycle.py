"""VM lifecycle primitives used by CLI flows.

This module owns host-side VM state transitions: image acquisition, cloud-init
artifact generation, VM definition/start, readiness waits, and provisioning.
Most functions assume libvirt ``qemu:///system`` usage with host sudo.
"""

from __future__ import annotations

import textwrap
import time
from pathlib import Path
from urllib.parse import unquote, urlparse

from loguru import logger

from ..commands import CommandManager
from ..declared_replay import (
    DECLARED_ATTACHMENT_REPLAY_BIN,
    DECLARED_ATTACHMENT_REPLAY_SERVICE,
    declared_replay_python,
    declared_replay_service_unit,
)
from ..config import (
    DEFAULT_UBUNTU_NOBLE_IMG_URL,
    SUPPORTED_IMAGE_SHA256,
    AgentVMConfig,
)
from ..runtime import require_ssh_identity, ssh_base_args
from ..util import CmdError, ensure_dir

log = logger


def _is_missing_uefi_firmware_error(ex: Exception) -> bool:
    text = str(ex).lower()
    return "did not find any uefi binary path for arch 'x86_64'" in text


def _is_missing_virtiofsd_error(ex: Exception) -> bool:
    return 'unable to find a satisfying virtiofsd' in str(ex).lower()


def _is_guest_memory_allocation_error(ex: Exception) -> bool:
    text = str(ex).lower()
    return "cannot set up guest memory 'pc.ram': cannot allocate memory" in text


def _virtiofsd_failure_message(source_dir: str) -> str:
    return (
        'VM creation failed because virtiofsd is not available on this host, '
        f'but a shared folder was requested (source={source_dir}).\n'
        'Install virtiofs support on the host (e.g. package providing '
        '`virtiofsd`, often `qemu-system-common` or `virtiofsd`), or disable '
        'folder sharing for this run.'
    )


def _memory_allocation_failure_message(cfg: AgentVMConfig) -> str:
    return (
        'VM creation failed because QEMU could not allocate guest RAM on the host.\n'
        f'Requested resources: ram_mb={cfg.vm.ram_mb}, cpus={cfg.vm.cpus}.\n'
        'This is common on nested/low-memory hosts. Try lowering VM resources '
        '(for example ram_mb=2048 and cpus=2) and retry.'
    )


def _failed_command_name(ex: Exception) -> str | None:
    if isinstance(ex, FileNotFoundError):
        name = ex.filename
        return str(name) if name else None
    if not isinstance(ex, CmdError):
        return None
    cmd = ex.cmd
    if isinstance(cmd, str):
        parts = cmd.split()
    else:
        parts = [str(p) for p in cmd]
    if not parts:
        return None
    if parts[0] == 'sudo' and len(parts) > 1:
        return parts[1]
    return parts[0]


def _is_missing_command_error(ex: Exception) -> bool:
    if isinstance(ex, FileNotFoundError):
        return True
    if not isinstance(ex, CmdError):
        return False
    if ex.result.code == 127:
        return True
    text = f'{ex.result.stderr}\n{ex.result.stdout}'.lower()
    return 'command not found' in text


def _sudo_path_exists(path: Path) -> bool:
    mgr = CommandManager.current()
    return (
        mgr.run(
            ['test', '-e', str(path)],
            sudo=True,
            role='read',
            check=False,
            capture=True,
        ).code
        == 0
    )


def _sudo_file_exists(path: Path) -> bool:
    mgr = CommandManager.current()
    return (
        mgr.run(
            ['test', '-f', str(path)],
            sudo=True,
            role='read',
            check=False,
            capture=True,
        ).code
        == 0
    )


def _vm_defined(name: str) -> bool:
    mgr = CommandManager.current()
    if mgr.current_plan() is None:
        with mgr.step(
            'Inspect VM definition',
            why=(
                'Check whether the libvirt domain already exists before '
                'deciding whether create, recreate, or cleanup work is needed.'
            ),
            approval_scope=f'vm-defined:{name}',
        ):
            res = mgr.submit(
                ['virsh', 'dominfo', name],
                sudo=True,
                role='read',
                check=False,
                capture=True,
                eager=True,
                summary=f'Inspect VM definition {name}',
            ).result()
    else:
        res = mgr.run(
            ['virsh', 'dominfo', name],
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Inspect VM definition {name}',
        )
    return res.code == 0


def _submit_qemu_dir_prepare(
    mgr: CommandManager,
    path: Path,
    *,
    group: str,
    mode: str,
    summary_prefix: str,
    recursive: bool,
) -> None:
    mgr.submit(
        ['mkdir', '-p', str(path)],
        sudo=True,
        role='modify',
        check=True,
        capture=True,
        summary=f'Create {summary_prefix}',
    )
    mgr.submit(
        ['chown', *(['-R'] if recursive else []), f'root:{group}', str(path)],
        sudo=True,
        role='modify',
        check=True,
        capture=True,
        summary=f'Set libvirt ownership for {summary_prefix}',
    )
    mgr.submit(
        ['chmod', mode, str(path)],
        sudo=True,
        role='modify',
        check=True,
        capture=True,
        summary=f'Set permissions for {summary_prefix}',
    )


def _destroy_and_undefine_vm(name: str) -> None:
    mgr = CommandManager.current()
    mgr.run(
        ['virsh', 'destroy', name],
        sudo=True,
        role='modify',
        check=False,
        capture=True,
    )
    # Different libvirt states require different undefine flags.
    attempts = [
        [
            'virsh',
            'undefine',
            name,
            '--managed-save',
            '--snapshots-metadata',
            '--nvram',
            '--remove-all-storage',
        ],
        [
            'virsh',
            'undefine',
            name,
            '--managed-save',
            '--snapshots-metadata',
            '--nvram',
        ],
        ['virsh', 'undefine', name, '--nvram', '--remove-all-storage'],
        ['virsh', 'undefine', name, '--nvram'],
        ['virsh', 'undefine', name, '--remove-all-storage'],
        ['virsh', 'undefine', name],
    ]
    errs: list[str] = []
    for cmd in attempts:
        res = mgr.run(
            cmd,
            sudo=True,
            role='modify',
            check=False,
            capture=True,
        )
        if res.code != 0:
            msg = (res.stderr or res.stdout or '').strip()
            if msg:
                errs.append(f'{cmd}: {msg}')
        if not _vm_defined(name):
            return
    detail = '\n'.join(errs[-4:]) if errs else '(no details)'
    raise RuntimeError(
        f'Failed to undefine VM {name}; domain is still present after retries.\n{detail}'
    )


def _paths(cfg: AgentVMConfig, *, dry_run: bool = False) -> dict[str, Path]:
    cfg = cfg.expanded_paths()
    base_dir = Path(cfg.paths.base_dir) / cfg.vm.name
    img_dir = base_dir / 'images'
    ci_dir = base_dir / 'cloud-init'
    state_dir = Path(cfg.paths.state_dir) / cfg.vm.name
    return {
        'base_dir': base_dir,
        'img_dir': img_dir,
        'ci_dir': ci_dir,
        'state_dir': state_dir,
        'ip_file': state_dir / f'{cfg.vm.name}.ip',
        'known_hosts': state_dir / 'known_hosts',
    }


def _resolve_expected_image_sha256(*, image_url: str) -> tuple[str | None, str]:
    digest = SUPPORTED_IMAGE_SHA256.get(image_url, '').strip().lower()
    if digest:
        return digest, 'built-in supported-image hash registry'
    parsed = urlparse(image_url)
    if parsed.scheme == 'file':
        file_path = Path(unquote(parsed.path))
        if not file_path.exists():
            raise RuntimeError(
                'Local file image URL does not exist.\n'
                f'Requested URL: {image_url}\n'
                f'Path: {file_path}'
            )
        out = (
            CommandManager.current()
            .run(
                ['sha256sum', str(file_path)],
                check=True,
                capture=True,
            )
            .stdout
        )
        local_digest = out.strip().split()[0].lower() if out.strip() else ''
        for supported_url, supported_digest in SUPPORTED_IMAGE_SHA256.items():
            if local_digest == supported_digest.strip().lower():
                return (
                    local_digest,
                    'local file URL matched supported pinned image digest '
                    f'({supported_url})',
                )
        supported_hashes = '\n'.join(
            f'  - {u} :: {d}' for u, d in sorted(SUPPORTED_IMAGE_SHA256.items())
        )
        raise RuntimeError(
            'Image file URL digest is not in the built-in verified image registry.\n'
            f'Requested URL: {image_url}\n'
            f'Path: {file_path}\n'
            f'Actual SHA256: {local_digest}\n'
            'Supported URL/hash entries:\n'
            f'{supported_hashes}\n'
            'This often means a partial/corrupt local cache file. Delete it and retry.'
        )
    supported = '\n'.join(f'  - {u}' for u in sorted(SUPPORTED_IMAGE_SHA256))
    raise RuntimeError(
        'Image URL is not in the built-in verified image registry.\n'
        f'Requested URL: {image_url}\n'
        'Supported URLs:\n'
        f'{supported}\n'
        'Use a supported image URL, or add this URL + SHA256 to SUPPORTED_IMAGE_SHA256.'
    )


def _verify_image_sha256(
    *, image_path: Path, expected_sha256: str | None, source: str
) -> None:
    if not expected_sha256:
        return
    mgr = CommandManager.current()
    log.info('Verifying base image checksum (source: {})', source)
    out = mgr.submit(
        ['sha256sum', str(image_path)],
        sudo=True,
        role='read',
        check=True,
        capture=True,
        summary='Compute base image checksum',
        detail=f'path={image_path} source={source}',
    ).stdout
    actual = out.strip().split()[0].lower() if out.strip() else ''
    if actual != expected_sha256:
        mgr.submit(
            ['rm', '-f', str(image_path)],
            sudo=True,
            role='modify',
            check=False,
            capture=True,
            summary='Remove invalid base image after checksum mismatch',
            detail=f'path={image_path}',
        ).result()
        raise RuntimeError(
            'Downloaded base image checksum mismatch; removed invalid image.\n'
            f'Path: {image_path}\n'
            f'Expected: {expected_sha256}\n'
            f'Actual:   {actual}\n'
            'Re-run to retry download, or use a supported pinned image URL.'
        )
    log.info('Base image checksum verified: {}', image_path)


def fetch_image(cfg: AgentVMConfig, *, dry_run: bool = False) -> Path:
    log.trace('fetch_image vm={} dry_run={}', cfg.vm.name, dry_run)
    log.debug('Fetching Ubuntu cloud image')
    p = _paths(cfg, dry_run=dry_run)
    base_img = p['img_dir'] / cfg.image.cache_name
    tmp_img = Path(str(base_img) + '.part')
    url = cfg.image.ubuntu_img_url or DEFAULT_UBUNTU_NOBLE_IMG_URL
    expected_sha256, checksum_source = _resolve_expected_image_sha256(
        image_url=url
    )
    parsed = urlparse(url)
    local_file_src = (
        Path(unquote(parsed.path)).expanduser()
        if parsed.scheme == 'file'
        else None
    )
    mgr = CommandManager.current()
    if _sudo_file_exists(base_img) and not cfg.image.redownload:
        if dry_run:
            log.info('Base image cached: {}', base_img)
            return base_img
        # Re-verify named cache hits so interrupted downloads or stale local
        # files do not silently poison later VM creation runs.
        try:
            with mgr.intent(
                'Fetch base image',
                why=(
                    'VM creation needs a verified Ubuntu cloud image cached on '
                    'the host before the VM disk can be prepared.'
                ),
                role='modify',
            ):
                with mgr.step(
                    'Verify cached base image',
                    why=(
                        'Revalidate the cached image so interrupted downloads or '
                        'stale local files do not silently poison later VM runs.'
                    ),
                    approval_scope=f'image-verify-cache:{cfg.vm.name}',
                ):
                    _verify_image_sha256(
                        image_path=base_img,
                        expected_sha256=expected_sha256,
                        source=f'cached base image ({checksum_source})',
                    )
            log.info('Base image cached: {}', base_img)
            return base_img
        except RuntimeError as ex:
            log.warning(
                'Cached base image failed checksum verification; redownloading. {}',
                ex,
            )
    if dry_run:
        if local_file_src is not None:
            log.info(
                'DRYRUN: cp {} {}; mv {} {}',
                local_file_src,
                tmp_img,
                tmp_img,
                base_img,
            )
        else:
            log.info(
                'DRYRUN: curl -L --fail -o {} {}; mv {} {}',
                tmp_img,
                url,
                tmp_img,
                base_img,
            )
        return base_img
    _ensure_qemu_access(cfg, dry_run=False)
    if local_file_src is not None:
        log.info(
            'Copying local base image to {} via atomic temp file', base_img
        )
    else:
        log.info('Downloading base image to {} (showing progress)', base_img)
    with mgr.intent(
        'Fetch base image',
        why=(
            'VM creation needs a verified Ubuntu cloud image cached on the '
            'host before the VM disk can be prepared.'
        ),
        role='modify',
    ):
        with mgr.step(
            'Fetch and verify base image',
            why=(
                'Prepare the image directory, refresh any stale partial file, '
                'transfer the image atomically, and verify its checksum before '
                'it is reused.'
            ),
            approval_scope=f'image-fetch:{cfg.vm.name}',
        ):
            mkdir_handle = mgr.submit(
                ['mkdir', '-p', str(p['img_dir'])],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Create VM image directory',
                detail=f'target={p["img_dir"]}',
            )
            cleanup_tmp_handle = mgr.submit(
                ['rm', '-f', str(tmp_img)],
                sudo=True,
                role='modify',
                check=False,
                capture=True,
                summary='Remove stale partial image file',
                detail=f'target={tmp_img}',
            )
            transfer_cmd = (
                ['cp', '--reflink=auto', str(local_file_src), str(tmp_img)]
                if local_file_src is not None
                else [
                    'curl',
                    '-L',
                    '--fail',
                    '--progress-bar',
                    '-o',
                    str(tmp_img),
                    url,
                ]
            )
            transfer_handle = mgr.submit(
                transfer_cmd,
                sudo=True,
                role='modify',
                check=False,
                capture=(local_file_src is not None),
                summary=(
                    'Copy local base image into staging file'
                    if local_file_src is not None
                    else 'Download base image into staging file'
                ),
                detail=(
                    f'source={local_file_src} destination={tmp_img}'
                    if local_file_src is not None
                    else f'url={url} destination={tmp_img}'
                ),
            )
            move_handle = mgr.submit(
                ['mv', '-f', str(tmp_img), str(base_img)],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Move staged base image into cache',
                detail=f'source={tmp_img} destination={base_img}',
            )
            checksum_handle = mgr.submit(
                ['sha256sum', str(base_img)],
                sudo=True,
                role='read',
                check=True,
                capture=True,
                summary='Compute base image checksum',
                detail=f'path={base_img} source={checksum_source}',
            )
            mkdir_handle.result()
            cleanup_tmp_handle.result()
            transfer_res = transfer_handle.result()
            if transfer_res.code != 0:
                mgr.submit(
                    ['rm', '-f', str(tmp_img)],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=True,
                    summary='Remove incomplete staging image after transfer failure',
                    detail=f'target={tmp_img}',
                ).result()
                raise CmdError(transfer_cmd, transfer_res)
            move_handle.result()
            checksum_out = checksum_handle.stdout
            actual = (
                checksum_out.strip().split()[0].lower()
                if checksum_out.strip()
                else ''
            )
            if expected_sha256 and actual != expected_sha256:
                mgr.submit(
                    ['rm', '-f', str(base_img)],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=True,
                    summary='Remove invalid base image after checksum mismatch',
                    detail=f'path={base_img}',
                ).result()
                raise RuntimeError(
                    'Downloaded base image checksum mismatch; removed invalid image.\n'
                    f'Path: {base_img}\n'
                    f'Expected: {expected_sha256}\n'
                    f'Actual:   {actual}\n'
                    'Re-run to retry download, or use a supported pinned image URL.'
                )
            log.info('Base image checksum verified: {}', base_img)
    log.info('Downloaded base image: {}', base_img)
    return base_img


def _ensure_qemu_access(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    cfg = cfg.expanded_paths()
    base_root = Path(cfg.paths.base_dir) / cfg.vm.name
    grp = 'libvirt-qemu'
    if (
        CommandManager.current()
        .run(['getent', 'group', 'libvirt-qemu'], check=False, capture=True)
        .code
        != 0
    ):
        grp = 'kvm'
    if dry_run:
        log.info(
            'DRYRUN: chown/chmod {} for qemu access (group={})', base_root, grp
        )
        return
    mgr = CommandManager.current()
    with mgr.intent(
        'Prepare VM storage',
        why=(
            'libvirt/qemu need host directories with predictable ownership and '
            'permissions before images and cloud-init artifacts are written.'
        ),
        role='modify',
    ):
        with mgr.step(
            'Prepare qemu-accessible VM directories',
            why=(
                'Create the VM root plus image and cloud-init directories with '
                'libvirt-readable ownership and permissions.'
            ),
            approval_scope=f'vm-storage:{base_root}',
        ):
            _submit_qemu_dir_prepare(
                mgr,
                base_root,
                group=grp,
                mode='0751',
                summary_prefix='VM root directory',
                recursive=False,
            )
            _submit_qemu_dir_prepare(
                mgr,
                base_root / 'images',
                group=grp,
                mode='0750',
                summary_prefix='VM image directory',
                recursive=True,
            )
            _submit_qemu_dir_prepare(
                mgr,
                base_root / 'cloud-init',
                group=grp,
                mode='0750',
                summary_prefix='cloud-init directory',
                recursive=True,
            )


def _write_cloud_init(
    cfg: AgentVMConfig, *, dry_run: bool = False
) -> dict[str, Path]:
    """Render and materialize cloud-init artifacts for a VM definition.

    Returns the host paths of generated artifacts, including the seed ISO used
    by ``virt-install``. The generated config intentionally enables guest sudo
    for agent workflows.
    """
    cfg = cfg.expanded_paths()
    p = _paths(cfg, dry_run=dry_run)
    ci_dir = p['ci_dir']
    user_data = ci_dir / 'user-data'
    meta_data = ci_dir / 'meta-data'
    network_config = ci_dir / 'network-config'
    seed_iso = ci_dir / f'{cfg.vm.name}-seed.iso'

    pubkey_path = (
        Path(cfg.paths.ssh_pubkey_path) if cfg.paths.ssh_pubkey_path else None
    )
    if not pubkey_path or not pubkey_path.exists():
        raise RuntimeError(
            f'Missing SSH public key. Set paths.ssh_pubkey_path in config (got: {cfg.paths.ssh_pubkey_path})'
        )
    pubkey = pubkey_path.read_text(encoding='utf-8').strip()

    ssh_pwauth = 'true' if cfg.vm.allow_password_login else 'false'
    lock_passwd = 'false' if cfg.vm.allow_password_login else 'true'
    passwd_block = ''
    sshd_pw = 'yes' if cfg.vm.allow_password_login else 'no'
    sshd_kbd = 'yes' if cfg.vm.allow_password_login else 'no'

    if cfg.vm.allow_password_login:
        if ':' in cfg.vm.password or '\n' in cfg.vm.password:
            raise RuntimeError(
                "VM password must not contain ':' or newlines (cloud-init chpasswd format)."
            )
        passwd_block = textwrap.dedent(
            f"""\
            chpasswd:
              expire: false
              users:
                - name: {cfg.vm.user}
                  password: {cfg.vm.password}
            """
        )
        passwd_block = textwrap.indent(passwd_block.rstrip('\n'), '        ')

    cloud = textwrap.dedent(
        f"""\
        #cloud-config
        users:
          - name: {cfg.vm.user}
            groups: [sudo]
            shell: /bin/bash
            sudo: ["ALL=(ALL) NOPASSWD:ALL"]
            lock_passwd: {lock_passwd}
            ssh_authorized_keys:
              - {pubkey}

        ssh_pwauth: {ssh_pwauth}
        disable_root: true

{passwd_block}
        # cloud-localds already seeds NoCloud; repeating datasource keys in the
        # user-data blob only triggers cloud-init schema warnings.
        bootcmd:
          - [bash, -c, "systemctl mask systemd-networkd-wait-online.service NetworkManager-wait-online.service || true"]

        package_update: true
        packages:
          - openssh-server
          - ca-certificates
          - curl
          - git
          - python3
          - python3-venv
          - python3-pip
          - unattended-upgrades

        write_files:
          - path: /etc/ssh/sshd_config.d/99-aivm-hardening.conf
            permissions: "0644"
            content: |
              PasswordAuthentication {sshd_pw}
              PermitRootLogin no
              KbdInteractiveAuthentication {sshd_kbd}
              X11Forwarding no
              AllowTcpForwarding yes
              GatewayPorts no
          - path: {DECLARED_ATTACHMENT_REPLAY_BIN}
            permissions: "0755"
            content: |
{textwrap.indent(declared_replay_python().rstrip(), '              ')}
          - path: /etc/systemd/system/{DECLARED_ATTACHMENT_REPLAY_SERVICE}
            permissions: "0644"
            content: |
{textwrap.indent(declared_replay_service_unit().rstrip(), '              ')}

        runcmd:
          - systemctl mask --now systemd-networkd-wait-online.service NetworkManager-wait-online.service || true
          - systemctl daemon-reload
          - systemctl enable {DECLARED_ATTACHMENT_REPLAY_SERVICE}
          - systemctl enable --now ssh
          - systemctl enable --now unattended-upgrades || true
        """
    )

    meta = textwrap.dedent(
        f"""\
        instance-id: {cfg.vm.name}
        local-hostname: {cfg.vm.name}
        """
    )
    netcfg = textwrap.dedent(
        """\
        version: 2
        ethernets:
          all-en:
            match:
              name: "en*"
            dhcp4: true
            optional: true
          all-eth:
            match:
              name: "eth*"
            dhcp4: true
            optional: true
        """
    )

    if dry_run:
        log.info(
            'DRYRUN: write cloud-init (+network-config) + cloud-localds {}',
            seed_iso,
        )
        return {
            'user_data': user_data,
            'meta_data': meta_data,
            'network_config': network_config,
            'seed_iso': seed_iso,
        }

    _ensure_qemu_access(cfg, dry_run=False)
    mgr = CommandManager.current()
    with mgr.intent(
        'Generate cloud-init artifacts',
        why=(
            'VM creation needs cloud-init user-data, metadata, network config, '
            'and a seed ISO before virt-install can define the guest.'
        ),
        role='modify',
    ):
        with mgr.step(
            'Write cloud-init files and build seed ISO',
            why=(
                'Materialize the rendered cloud-init files on the host and pack '
                'them into a NoCloud seed image for the VM.'
            ),
            approval_scope=f'cloud-init:{cfg.vm.name}',
        ):
            mgr.submit(
                ['mkdir', '-p', str(ci_dir)],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Create cloud-init artifact directory',
            )
            mgr.submit(
                ['bash', '-c', f"cat > {user_data} <<'EOF'\n{cloud}\nEOF"],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Write cloud-init user-data',
            )
            mgr.submit(
                ['bash', '-c', f"cat > {meta_data} <<'EOF'\n{meta}\nEOF"],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Write cloud-init meta-data',
            )
            mgr.submit(
                [
                    'bash',
                    '-c',
                    f"cat > {network_config} <<'EOF'\n{netcfg}\nEOF",
                ],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Write cloud-init network config',
            )
            mgr.submit(
                [
                    'cloud-localds',
                    '-v',
                    '-N',
                    str(network_config),
                    str(seed_iso),
                    str(user_data),
                    str(meta_data),
                ],
                sudo=True,
                role='modify',
                check=True,
                capture=True,
                summary='Build NoCloud seed ISO from cloud-init files',
            )
    return {
        'user_data': user_data,
        'meta_data': meta_data,
        'network_config': network_config,
        'seed_iso': seed_iso,
    }


def _ensure_disk(
    cfg: AgentVMConfig,
    base_img: Path,
    *,
    dry_run: bool = False,
    recreate: bool = False,
) -> Path:
    p = _paths(cfg, dry_run=dry_run)
    vm_disk = p['img_dir'] / f'{cfg.vm.name}.qcow2'
    mgr = CommandManager.current()
    if _sudo_path_exists(vm_disk) and recreate:
        if dry_run:
            log.info('DRYRUN: rm -f {}', vm_disk)
        else:
            mgr.run(
                ['rm', '-f', str(vm_disk)], sudo=True, check=True, capture=True
            )
    if _sudo_path_exists(vm_disk):
        log.info('VM disk exists: {}', vm_disk)
        return vm_disk
    if dry_run:
        log.info(
            'DRYRUN: qemu-img create -f qcow2 -F qcow2 -b {} {} {}G',
            base_img,
            vm_disk,
            cfg.vm.disk_gb,
        )
        return vm_disk
    mgr.run(
        [
            'qemu-img',
            'create',
            '-f',
            'qcow2',
            '-F',
            'qcow2',
            '-b',
            str(base_img),
            str(vm_disk),
            f'{cfg.vm.disk_gb}G',
        ],
        sudo=True,
        check=True,
        capture=True,
    )
    return vm_disk


def vm_exists(cfg: AgentVMConfig, *, dry_run: bool = False) -> bool:
    if dry_run:
        return False
    return _vm_defined(cfg.vm.name)


def create_or_start_vm(
    cfg: AgentVMConfig,
    *,
    dry_run: bool = False,
    recreate: bool = False,
    share_source_dir: str = '',
    share_tag: str = '',
) -> None:
    """Ensure a VM exists and is running, creating/redefining when needed.

    Behavior summary:
    * existing running VM: no-op
    * existing stopped VM: start
    * recreate requested: destroy/undefine then define again
    * missing VM: create from base image + cloud-init artifacts
    """
    log.trace(
        'create_or_start_vm vm={} dry_run={} recreate={} share_source_dir={} share_tag={}',
        cfg.vm.name,
        dry_run,
        recreate,
        share_source_dir or '(none)',
        share_tag or '(none)',
    )
    log.debug('Creating or starting VM {}', cfg.vm.name)
    cfg = cfg.expanded_paths()
    mgr = CommandManager.current()

    with mgr.intent(
        f'Ensure VM {cfg.vm.name} exists and is running',
        why=(
            'Reuse an existing VM when possible, otherwise create or '
            'recreate the libvirt domain and boot it for the requested task.'
        ),
        role='modify',
    ):
        if vm_exists(cfg, dry_run=dry_run):
            if not recreate:
                with mgr.step(
                    'Ensure existing VM is running',
                    why=(
                        'Inspect the current domain state and start the '
                        'existing VM only if it is defined but stopped.'
                    ),
                    approval_scope=f'vm-start:{cfg.vm.name}',
                ):
                    st = (
                        mgr.submit(
                            ['virsh', 'domstate', cfg.vm.name],
                            sudo=True,
                            role='read',
                            check=False,
                            capture=True,
                            eager=True,
                            summary=f'Inspect runtime state for VM {cfg.vm.name}',
                        )
                        .stdout.strip()
                        .lower()
                    )
                    if 'running' in st:
                        log.info('VM already running: {}', cfg.vm.name)
                        return
                    if dry_run:
                        log.info('DRYRUN: virsh start {}', cfg.vm.name)
                        return
                    mgr.submit(
                        ['virsh', 'start', cfg.vm.name],
                        sudo=True,
                        role='modify',
                        check=True,
                        capture=True,
                        summary=f'Start existing VM {cfg.vm.name}',
                    )
                log.info('VM started: {}', cfg.vm.name)
                return
            if dry_run:
                log.info('DRYRUN: virsh destroy/undefine {}', cfg.vm.name)
            else:
                _destroy_and_undefine_vm(cfg.vm.name)

        base_img = fetch_image(cfg, dry_run=dry_run)
        try:
            ci = _write_cloud_init(cfg, dry_run=dry_run)
        except Exception as ex:
            if _is_missing_command_error(ex):
                missing = _failed_command_name(ex)
                hint = (
                    f'Missing required host command: `{missing}`. '
                    if missing
                    else ''
                )
                raise RuntimeError(
                    f'Failed to build cloud-init artifacts for VM `{cfg.vm.name}`. '
                    f'{hint}Run `aivm host install_deps` and retry.'
                ) from ex
            raise

        vm_disk = _ensure_disk(
            cfg, base_img, dry_run=dry_run, recreate=recreate
        )
        seed_iso = ci['seed_iso']

        # Always define VMs with shared memory backing so virtiofs can be attached
        # later without requiring a VM recreate.
        extra = ['--memorybacking', 'source.type=memfd,access.mode=shared']
        source_dir = str(share_source_dir or '').strip()
        tag = str(share_tag or '').strip()
        if source_dir:
            if not tag:
                raise RuntimeError(
                    'share_tag is required when share_source_dir is provided.'
                )
            extra += [
                '--filesystem',
                f'source={source_dir},target={tag},driver.type=virtiofs',
            ]

        # These VMs are for agent development workflows, not secure-boot or TPM
        # validation. Keep UEFI for modern Ubuntu boot, but make the firmware
        # profile explicit so nested hosts do not inherit heavier defaults that
        # have proven flaky and so serial console output is more useful.
        boot_opts = 'uefi,loader.secure=no,bios.useserial=on'
        cmd = [
            'virt-install',
            '--name',
            cfg.vm.name,
            '--memory',
            str(cfg.vm.ram_mb),
            '--vcpus',
            str(cfg.vm.cpus),
            '--cpu',
            'host-passthrough',
            '--import',
            '--os-variant',
            'ubuntu24.04',
            '--disk',
            f'path={vm_disk},format=qcow2,bus=virtio',
            '--disk',
            f'path={seed_iso},device=cdrom',
            '--network',
            f'network={cfg.network.name},model=virtio',
            '--graphics',
            'none',
            '--noautoconsole',
            '--rng',
            '/dev/urandom',
            '--tpm',
            'none',
            *extra,
        ]
        # Prefer UEFI consistently, but do not let libvirt auto-enable secure-boot
        # related firmware features for general-purpose development guests.
        cmd += ['--boot', boot_opts]
        if dry_run:
            log.info('DRYRUN: {}', ' '.join(cmd))
            return
        try:
            first = CommandManager.current().run(
                cmd,
                sudo=True,
                role='modify',
                check=False,
                capture=True,
            )
        except CmdError as ex:
            # Some call sites/tests may still raise even when check=False.
            first = ex.result
        if first.code != 0:
            err = CmdError(cmd, first)
            if source_dir and _is_missing_virtiofsd_error(err):
                raise RuntimeError(
                    _virtiofsd_failure_message(source_dir)
                ) from err
            if _is_guest_memory_allocation_error(err):
                raise RuntimeError(
                    _memory_allocation_failure_message(cfg)
                ) from err
            if _is_missing_uefi_firmware_error(err):
                log.warning(
                    'UEFI firmware not available on host. Retrying VM create with non-UEFI boot.'
                )
                cmd_no_uefi = list(cmd)
                try:
                    idx = cmd_no_uefi.index('--boot')
                    del cmd_no_uefi[idx : idx + 2]
                except ValueError:
                    pass
                try:
                    CommandManager.current().run(
                        cmd_no_uefi, sudo=True, check=True, capture=True
                    )
                except CmdError as ex2:
                    if source_dir and _is_missing_virtiofsd_error(ex2):
                        raise RuntimeError(
                            _virtiofsd_failure_message(source_dir)
                        ) from ex2
                    if _is_guest_memory_allocation_error(ex2):
                        raise RuntimeError(
                            _memory_allocation_failure_message(cfg)
                        ) from ex2
                    raise
            else:
                raise err
        log.info('VM created: {}', cfg.vm.name)


def _mac_for_vm(cfg: AgentVMConfig) -> str:
    mgr = CommandManager.current()
    if mgr.current_plan() is None:
        with mgr.step(
            'Inspect VM network interfaces',
            why=(
                'Read the VM interface list so later IP discovery can match '
                'DHCP leases against the guest MAC address.'
            ),
            approval_scope=f'vm-network-interfaces:{cfg.vm.name}',
        ):
            res = mgr.submit(
                ['virsh', 'domiflist', cfg.vm.name],
                sudo=True,
                role='read',
                check=False,
                capture=True,
                eager=True,
                summary=f'Inspect network interfaces for VM {cfg.vm.name}',
            ).result()
    else:
        res = mgr.run(
            ['virsh', 'domiflist', cfg.vm.name],
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Inspect network interfaces for VM {cfg.vm.name}',
        )
    for line in res.stdout.splitlines():
        if (
            'network' in line.lower()
            and 'interface' not in line.lower()
            and '---' not in line
        ):
            parts = line.split()
            if parts:
                return parts[-1].strip()
    return ''


def get_ip_cached(cfg: AgentVMConfig) -> str | None:
    p = _paths(cfg, dry_run=False)
    ip_file = p['ip_file']
    if ip_file.exists():
        return ip_file.read_text(encoding='utf-8').strip() or None
    return None


def wait_for_ip(
    cfg: AgentVMConfig, *, timeout_s: int = 360, dry_run: bool = False
) -> str:
    log.debug('Waiting for VM IP via DHCP lease')
    p = _paths(cfg, dry_run=dry_run)
    ip_file = p['ip_file']
    if dry_run:
        log.info('DRYRUN: wait for IP and write {}', ip_file)
        return '0.0.0.0'
    ensure_dir(p['state_dir'])
    mac = _mac_for_vm(cfg)
    cached_ip = get_ip_cached(cfg)
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    if not mac:
        log.warning(
            'Could not determine VM MAC; DHCP lease lookup may fail. Falling back to domifaddr.'
        )
    if cached_ip:
        log.info(
            'Using cached IP as fallback while waiting for lease discovery: {}',
            cached_ip,
        )
    deadline = time.time() + timeout_s
    start = time.time()
    next_status_at = start
    last_state = 'unknown'
    last_lease_count = 0
    last_domif_count = 0
    warned_about_possible_hang = False
    mgr = CommandManager.current()
    with mgr.intent(
        f'Wait for IP for {cfg.vm.name}',
        why='Poll libvirt lease/interface state until the guest IP is discoverable.',
        role='read',
    ):
        while time.time() < deadline:
            ip = ''
            lease_text = ''
            domif_text = ''
            if mac:
                lease_text = mgr.run(
                    ['virsh', 'net-dhcp-leases', cfg.network.name],
                    sudo=True,
                    role='read',
                    check=False,
                    capture=True,
                    summary=f'Inspect DHCP leases for network {cfg.network.name}',
                ).stdout
                for line in lease_text.splitlines():
                    if mac.lower() in line.lower():
                        parts = line.split()
                        for part in parts:
                            if '/' in part and '.' in part:
                                ip = part.split('/')[0]
                                break
                    if ip:
                        break
            if not ip:
                domif_text = mgr.run(
                    ['virsh', 'domifaddr', cfg.vm.name],
                    sudo=True,
                    role='read',
                    check=False,
                    capture=True,
                    summary=f'Inspect interface addresses for VM {cfg.vm.name}',
                ).stdout
                for line in domif_text.splitlines():
                    if 'ipv4' in line.lower():
                        parts = line.split()
                        for part in parts:
                            if '/' in part and '.' in part:
                                ip = part.split('/')[0]
                                break
                    if ip:
                        break
            if ip:
                log.info('Writing VM IP cache to {}', ip_file)
                ip_file.write_text(ip + '\n', encoding='utf-8')
                log.info('VM IP: {} (saved to {})', ip, ip_file)
                return ip
            if cached_ip:
                ssh_probe = mgr.run(
                    [
                        'ssh',
                        *ssh_base_args(
                            ident,
                            batch_mode=True,
                            connect_timeout=3,
                            strict_host_key_checking='accept-new',
                        ),
                        f'{cfg.vm.user}@{cached_ip}',
                        'true',
                    ],
                    sudo=False,
                    check=False,
                    capture=True,
                )
                if ssh_probe.code == 0:
                    log.info('Writing VM IP cache to {}', ip_file)
                    ip_file.write_text(cached_ip + '\n', encoding='utf-8')
                    log.info(
                        'VM reachable via cached IP fallback: {} (saved to {})',
                        cached_ip,
                        ip_file,
                    )
                    return cached_ip
            now = time.time()
            if now >= next_status_at:
                st = mgr.run(
                    ['virsh', 'domstate', cfg.vm.name],
                    sudo=True,
                    role='read',
                    check=False,
                    capture=True,
                    summary=f'Inspect runtime state for VM {cfg.vm.name}',
                ).stdout.strip()
                if st:
                    last_state = st
                lease_lines = [
                    line
                    for line in lease_text.splitlines()
                    if line.strip() and not set(line.strip()) <= {'-'}
                ]
                last_lease_count = max(0, len(lease_lines) - 1)
                domif_lines = [
                    line
                    for line in domif_text.splitlines()
                    if line.strip() and not set(line.strip()) <= {'-'}
                ]
                last_domif_count = max(0, len(domif_lines) - 1)
                elapsed = max(0, int(now - start))
                log.info(
                    'Waiting for VM network: vm={} elapsed={}s state={} leases_seen={} domifaddr_ipv4_rows={} mac={}',
                    cfg.vm.name,
                    elapsed,
                    last_state,
                    last_lease_count,
                    last_domif_count,
                    mac or 'unknown',
                )
                if elapsed >= 45 and not warned_about_possible_hang:
                    warned_about_possible_hang = True
                    log.warning(
                        'VM network still not ready after {}s. VM may still be booting, or hung. '
                        'Quick checks: `virsh console {}` and `aivm status --sudo --detail`.',
                        elapsed,
                        cfg.vm.name,
                    )
                if 'running' not in last_state.lower():
                    raise RuntimeError(
                        f'VM {cfg.vm.name} is not running while waiting for IP (state={last_state!r}).'
                    )
                next_status_at = now + 10
            time.sleep(2)
    raise TimeoutError(
        'Timed out waiting for VM IP '
        f'(vm={cfg.vm.name}, state={last_state!r}, leases_seen={last_lease_count}, domifaddr_ipv4_rows={last_domif_count}, cached_ip={cached_ip or "none"}). '
        f'Try: sudo virsh net-dhcp-leases {cfg.network.name}'
    )


def _is_vm_active(state: str) -> bool:
    """Return True if the libvirt state indicates an active domain.

    Active states include 'running', 'idle', 'paused', 'blocked', 'pmsuspended',
    and transient states like 'in shutdown' or 'shutting down'. Inactive
    states include 'shut off', 'crashed'.
    """
    state = state.lower().strip()
    # Active states: running, idle, paused, blocked, pmsuspended, in shutdown, shutting down
    active_states = ['running', 'idle', 'paused', 'blocked', 'pmsuspended', 'in shutdown', 'shutting down']
    return any(s in state for s in active_states)


def _get_vm_state(name: str) -> tuple[int, str, str]:
    """Get the current state of a VM.

    Returns a tuple of (return_code, state_string, error_string).
    The state and error strings are lowercased and stripped.
    On success, state contains the VM state and error is empty.
    On failure, state is empty and error contains the error message.
    """
    mgr = CommandManager.current()
    res = mgr.run(
        ['virsh', 'domstate', name],
        sudo=True,
        role='read',
        check=False,
        capture=True,
        summary=f'Get state of VM {name}',
    )
    state = (res.stdout or '').strip().lower()
    error = (res.stderr or '').strip().lower()
    return (res.code, state, error)


def _wait_for_vm_state(
    name: str,
    target_state: str,
    *,
    timeout_s: int = 120,
    poll_interval_s: int = 2,
) -> None:
    """Wait for a VM to reach a target state.

    Polls the VM state until it matches ``target_state`` or the timeout
    expires. Raises ``RuntimeError`` if the timeout is reached before
    the target state is observed, or if the domstate command fails.
    """
    import time

    elapsed = 0
    last_state = ''
    last_error = ''
    while elapsed < timeout_s:
        code, state, error = _get_vm_state(name)
        if code != 0:
            # Command failed - this is an error, not just a state change
            last_error = error
            raise RuntimeError(
                f'Failed to get state for VM {name} (code={code}). '
                f'Error: {last_error}'
            )
        if target_state in state:
            return
        time.sleep(poll_interval_s)
        elapsed += poll_interval_s
        last_state = state
    raise RuntimeError(
        f'Timeout waiting for VM {name} to reach state {target_state!r} '
        f'(current state: {last_state!r}) after {timeout_s}s.'
    )


def _wait_for_vm_not_state(
    name: str,
    exclude_state: str,
    *,
    timeout_s: int = 10,
    poll_interval_s: int = 1,
) -> None:
    """Wait for a VM to leave a specific state.

    Polls the VM state until it no longer matches ``exclude_state`` or the
    timeout expires. Raises ``RuntimeError`` if the timeout is reached or
    if the domstate command fails.
    This is useful for waiting for a VM to transition out of a suspended state.
    """
    import time

    elapsed = 0
    last_state = ''
    last_error = ''
    while elapsed < timeout_s:
        code, state, error = _get_vm_state(name)
        if code != 0:
            # Command failed - this is an error, not a state change
            last_error = error
            raise RuntimeError(
                f'Failed to get state for VM {name} (code={code}). '
                f'Error: {last_error}'
            )
        if exclude_state not in state:
            return
        time.sleep(poll_interval_s)
        elapsed += poll_interval_s
        last_state = state
    raise RuntimeError(
        f'Timeout waiting for VM {name} to leave state {exclude_state!r} '
        f'(still in state: {last_state!r}) after {timeout_s}s.'
    )


def shutdown_vm(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    """Gracefully shut down the VM using ACPI shutdown signal.

    This sends a graceful shutdown signal to the guest OS. If the guest
    does not shut down within a reasonable time, callers may need to use
    ``destroy_vm`` for a forced shutdown.
    """
    name = cfg.vm.name
    if dry_run:
        log.info('DRYRUN: virsh shutdown {}', name)
        return
    mgr = CommandManager.current()
    with mgr.intent(
        f'Shut down VM {name}',
        why='Gracefully stop the VM by sending an ACPI shutdown signal to the guest OS.',
        role='modify',
    ):
        # First check if VM is active
        code, state, error = _get_vm_state(name)
        if code != 0:
            msg = error or 'unknown error'
            raise RuntimeError(
                f'Failed to get state for VM {name} (code={code}). '
                f'Error: {msg}'
            )
        if not _is_vm_active(state):
            log.info('VM {} is not active (state={}); nothing to do.', name, state)
            return

        # Handle pmsuspended specially - resume first since ACPI shutdown
        # requires the guest to be running to receive the signal
        if 'pmsuspended' in state:
            log.info('VM {} is pmsuspended; resuming first', name)
            res = mgr.run(
                ['virsh', 'resume', name],
                sudo=True,
                role='modify',
                check=False,
                capture=True,
                summary='Resume pmsuspended VM',
            )
            if res.code != 0:
                msg = (res.stderr or res.stdout or '').strip()
                raise RuntimeError(
                    f'Failed to resume VM {name}.\n{msg}'
                )
            # Wait for VM to transition out of pmsuspended
            _wait_for_vm_not_state(name, 'pmsuspended', timeout_s=10, poll_interval_s=1)
            # Re-check state after resume to ensure VM is in a valid state for shutdown
            code, state, error = _get_vm_state(name)
            if code != 0:
                msg = error or 'unknown error'
                raise RuntimeError(
                    f'Failed to get state for VM {name} after resume (code={code}). '
                    f'Error: {msg}'
                )
            if not _is_vm_active(state):
                log.info('VM {} transitioned to inactive state {} after resume; nothing to do.', name, state)
                return
            log.info('VM {} resumed (state={})', name, state)

        # Send ACPI shutdown signal
        res = mgr.run(
            ['virsh', 'shutdown', name],
            sudo=True,
            role='modify',
            check=False,
            capture=True,
            summary=f'Send ACPI shutdown signal to VM {name}',
        )
        if res.code != 0:
            msg = (res.stderr or res.stdout or '').strip()
            raise RuntimeError(
                f'Failed to send shutdown signal to VM {name}.\n{msg}'
            )
        log.info('Shutdown signal sent to VM {}', name)


def restart_vm(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    """Gracefully restart the VM (shutdown then start).

    This sends a graceful shutdown signal to the guest OS, waits for it to
    stop, and then starts the VM again. If the guest does not shut down
    within a reasonable time, this may need to be followed by a forced
    restart using ``destroy_vm`` and ``create_or_start_vm``.

    This operation requires the VM to already exist; it will not create
    a new VM.
    """
    name = cfg.vm.name
    if dry_run:
        log.info('DRYRUN: restart VM {}', name)
        return

    # Verify the VM exists before attempting restart
    if not _vm_defined(name):
        raise RuntimeError(
            f'VM {name!r} does not exist. Restart requires an existing VM; '
            f'use `aivm vm up` to create and start it.'
        )

    mgr = CommandManager.current()
    with mgr.intent(
        f'Restart VM {name}',
        why='Gracefully stop and then start the VM to apply changes or recover from transient issues.',
        role='modify',
    ):
        # First check if VM is active
        code, state, error = _get_vm_state(name)
        if code != 0:
            msg = error or 'unknown error'
            raise RuntimeError(
                f'Failed to get state for VM {name} (code={code}). '
                f'Error: {msg}'
            )

        if _is_vm_active(state):
            # Handle pmsuspended specially - resume it first, then shutdown
            if 'pmsuspended' in state:
                log.info('VM {} is pmsuspended; resuming first', name)
                res = mgr.run(
                    ['virsh', 'resume', name],
                    sudo=True,
                    role='modify',
                    check=False,
                    capture=True,
                    summary='Resume pmsuspended VM',
                )
                if res.code != 0:
                    msg = (res.stderr or res.stdout or '').strip()
                    raise RuntimeError(
                        f'Failed to resume VM {name}.\n{msg}'
                    )
                # Wait for VM to transition out of pmsuspended
                _wait_for_vm_not_state(name, 'pmsuspended', timeout_s=10, poll_interval_s=1)
                # Re-check state after resume to ensure VM is in a valid state for shutdown
                code, state, error = _get_vm_state(name)
                if code != 0:
                    msg = error or 'unknown error'
                    raise RuntimeError(
                        f'Failed to get state for VM {name} after resume (code={code}). '
                        f'Error: {msg}'
                    )
                if not _is_vm_active(state):
                    log.info('VM {} transitioned to inactive state {} after resume; starting it.', name, state)
                    _start_vm(name)
                    log.info('VM {} restarted', name)
                    return
                log.info('VM {} resumed (state={})', name, state)

            log.info('Sending shutdown signal to VM {} (state={})', name, state)
            # Send ACPI shutdown signal
            res = mgr.run(
                ['virsh', 'shutdown', name],
                sudo=True,
                role='modify',
                check=False,
                capture=True,
                summary='Send ACPI shutdown signal to VM',
            )
            if res.code != 0:
                msg = (res.stderr or res.stdout or '').strip()
                raise RuntimeError(
                    f'Failed to send shutdown signal to VM {name}.\n{msg}'
                )
            # Wait for the VM to actually shut down before starting it again
            log.info('Waiting for VM {} to shut down...', name)
            _wait_for_vm_state(name, 'shut off', timeout_s=120, poll_interval_s=2)
            log.info('VM {} has shut down', name)
        else:
            log.info('VM {} is not active (state={}); starting it.', name, state)

        # Start the VM (use start_vm helper, not create_or_start_vm)
        log.info('Starting VM {}', name)
        _start_vm(name)
        log.info('VM {} restarted', name)


def _start_vm(name: str) -> None:
    """Start a defined VM by name.

    This is a low-level helper that only starts an existing domain;
    it does not create or recreate the VM.
    """
    mgr = CommandManager.current()
    mgr.run(
        ['virsh', 'start', name],
        sudo=True,
        role='modify',
        check=True,
        summary=f'Start VM {name}',
    )


def destroy_vm(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    name = cfg.vm.name
    if dry_run:
        log.info('DRYRUN: virsh destroy/undefine {}', name)
        return
    mgr = CommandManager.current()
    with mgr.intent(
        f'Destroy VM {name}',
        why='Remove the libvirt domain and its related managed definition state.',
        role='modify',
    ):
        _destroy_and_undefine_vm(name)
    log.info('VM removed: {}', name)


def vm_status(cfg: AgentVMConfig) -> str:
    name = cfg.vm.name
    mgr = CommandManager.current()
    with mgr.intent(
        f'Inspect VM {name}',
        why='Read the live libvirt domain details and cached IP for this VM.',
        role='read',
    ):
        dom = mgr.run(
            ['virsh', 'dominfo', name],
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Inspect VM definition {name}',
        )
        if dom.code != 0:
            return f'VM not found: {name}\n'
        state = mgr.run(
            ['virsh', 'domstate', name],
            sudo=True,
            role='read',
            check=False,
            capture=True,
            summary=f'Inspect VM runtime state {name}',
        ).stdout.strip()
        ip = get_ip_cached(cfg) or ''
        return (
            dom.stdout
            + f'\nstate={state}\n'
            + (f'cached_ip={ip}\n' if ip else '')
        )


def ssh_config(cfg: AgentVMConfig) -> str:
    cfg = cfg.expanded_paths()
    ip = get_ip_cached(cfg) or 'VM_IP_UNKNOWN'
    ident = cfg.paths.ssh_identity_file or '~/.ssh/id_ed25519'
    host = cfg.vm.name
    return f"""Host {host}
  HostName {ip}
  User {cfg.vm.user}
  IdentityFile {ident}
  IdentitiesOnly yes
  StrictHostKeyChecking accept-new
"""


def wait_for_ssh(
    cfg: AgentVMConfig,
    ip: str,
    *,
    timeout_s: int = 300,
    dry_run: bool = False,
) -> None:
    cfg = cfg.expanded_paths()
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    if dry_run:
        log.info('DRYRUN: wait for SSH on {}@{}', cfg.vm.user, ip)
        return
    deadline = time.time() + timeout_s
    # SSH can come up slowly on first boot, especially under nested
    # virtualization where cloud-init and key generation compete for limited
    # CPU. Keep each probe bounded, but allow enough time for a real login
    # handshake to finish before declaring the guest unreachable.
    probe_timeout_s = 30
    while time.time() < deadline:
        cmd = [
            'ssh',
            *ssh_base_args(
                ident,
                batch_mode=True,
                connect_timeout=3,
                strict_host_key_checking='accept-new',
            ),
            f'{cfg.vm.user}@{ip}',
            'true',
        ]
        res = CommandManager.current().run(
            cmd,
            sudo=False,
            check=False,
            capture=True,
            timeout=probe_timeout_s,
        )
        if res.code == 0:
            log.info('SSH is ready on {}', ip)
            return
        time.sleep(2)
    raise TimeoutError(f'Timed out waiting for SSH on {ip}:22')


def provision(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    log.debug('Provisioning VM with developer tools')
    if not cfg.provision.enabled:
        log.info('Provision disabled; skipping.')
        return
    cfg = cfg.expanded_paths()
    if dry_run:
        ip = '0.0.0.0'
    else:
        ip = get_ip_cached(cfg) or wait_for_ip(
            cfg, timeout_s=360, dry_run=False
        )
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    pkgs = list(cfg.provision.packages)
    docker_pkgs = (
        ['docker.io', 'docker-compose-v2']
        if cfg.provision.install_docker
        else []
    )
    ensure_uv = (
        'export PATH="$HOME/.local/bin:$PATH"; '
        'if ! command -v uv >/dev/null 2>&1; then '
        'curl -LsSf https://astral.sh/uv/install.sh | sh; '
        'fi; '
        'export PATH="$HOME/.local/bin:$PATH"; '
        'command -v uv >/dev/null 2>&1'
    )
    remote = (
        'set -euo pipefail; '
        'sudo apt-get update -y; '
        'sudo apt-get install -y software-properties-common >/dev/null 2>&1 || true; '
        'sudo add-apt-repository -y universe >/dev/null 2>&1 || true; '
        'sudo apt-get update -y; '
        f'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {" ".join(docker_pkgs + pkgs)}; '
        f'{ensure_uv}'
    )
    cmd = [
        'ssh',
        *ssh_base_args(ident, strict_host_key_checking='accept-new'),
        f'{cfg.vm.user}@{ip}',
        remote,
    ]
    if dry_run:
        log.info('DRYRUN: {}', ' '.join(cmd))
        return
    wait_for_ssh(cfg, ip, timeout_s=300, dry_run=False)
    log.info('Running provisioning apt installs (showing progress)')
    CommandManager.current().run(cmd, sudo=False, check=True, capture=False)
    log.info('Provisioning complete.')
