"""VM lifecycle implementation: image, cloud-init, create/start, wait, and provision."""

from __future__ import annotations

import time
from pathlib import Path

from loguru import logger

from ..config import DEFAULT_UBUNTU_NOBLE_IMG_URL, AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args
from ..util import CmdError, ensure_dir, run_cmd

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


def _sudo_path_exists(path: Path) -> bool:
    return (
        run_cmd(
            ['test', '-e', str(path)], sudo=True, check=False, capture=True
        ).code
        == 0
    )


def _sudo_file_exists(path: Path) -> bool:
    return (
        run_cmd(
            ['test', '-f', str(path)], sudo=True, check=False, capture=True
        ).code
        == 0
    )


def _vm_defined(name: str) -> bool:
    return (
        run_cmd(
            ['virsh', 'dominfo', name], sudo=True, check=False, capture=True
        ).code
        == 0
    )


def _destroy_and_undefine_vm(name: str) -> None:
    run_cmd(['virsh', 'destroy', name], sudo=True, check=False, capture=True)
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
        res = run_cmd(cmd, sudo=True, check=False, capture=True)
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


def fetch_image(cfg: AgentVMConfig, *, dry_run: bool = False) -> Path:
    log.trace('fetch_image vm={} dry_run={}', cfg.vm.name, dry_run)
    log.debug('Fetching Ubuntu cloud image')
    p = _paths(cfg, dry_run=dry_run)
    base_img = p['img_dir'] / cfg.image.cache_name
    tmp_img = Path(str(base_img) + '.part')
    url = cfg.image.ubuntu_img_url or DEFAULT_UBUNTU_NOBLE_IMG_URL
    if _sudo_file_exists(base_img) and not cfg.image.redownload:
        log.info('Base image cached: {}', base_img)
        return base_img
    if dry_run:
        log.info(
            'DRYRUN: curl -L --fail -o {} {}; mv {} {}',
            tmp_img,
            url,
            tmp_img,
            base_img,
        )
        return base_img
    _ensure_qemu_access(cfg, dry_run=False)
    run_cmd(
        ['mkdir', '-p', str(p['img_dir'])], sudo=True, check=True, capture=True
    )
    run_cmd(['rm', '-f', str(tmp_img)], sudo=True, check=False, capture=True)
    log.info('Downloading base image to {} (showing progress)', base_img)
    try:
        run_cmd(
            ['curl', '-L', '--fail', '--progress-bar', '-o', str(tmp_img), url],
            sudo=True,
            check=True,
            capture=False,
        )
        run_cmd(
            ['mv', '-f', str(tmp_img), str(base_img)],
            sudo=True,
            check=True,
            capture=True,
        )
    except CmdError:
        run_cmd(
            ['rm', '-f', str(tmp_img)], sudo=True, check=False, capture=True
        )
        raise
    log.info('Downloaded base image: {}', base_img)
    return base_img


def _ensure_qemu_access(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    cfg = cfg.expanded_paths()
    base_root = Path(cfg.paths.base_dir) / cfg.vm.name
    grp = 'libvirt-qemu'
    if (
        run_cmd(
            ['getent', 'group', 'libvirt-qemu'], check=False, capture=True
        ).code
        != 0
    ):
        grp = 'kvm'
    if dry_run:
        log.info(
            'DRYRUN: chown/chmod {} for qemu access (group={})', base_root, grp
        )
        return
    run_cmd(
        ['mkdir', '-p', str(base_root)], sudo=True, check=True, capture=True
    )
    run_cmd(
        ['chown', '-R', f'root:{grp}', str(base_root)],
        sudo=True,
        check=True,
        capture=True,
    )
    run_cmd(
        ['chmod', '0751', str(base_root)], sudo=True, check=True, capture=True
    )
    for sub in ('images', 'cloud-init'):
        d = base_root / sub
        run_cmd(['mkdir', '-p', str(d)], sudo=True, check=True, capture=True)
        run_cmd(
            ['chown', '-R', f'root:{grp}', str(d)],
            sudo=True,
            check=True,
            capture=True,
        )
        run_cmd(['chmod', '0750', str(d)], sudo=True, check=True, capture=True)


def _write_cloud_init(
    cfg: AgentVMConfig, *, dry_run: bool = False
) -> dict[str, Path]:
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

    # TODO: use textwrap to make these multi-line strings look
    # nice with the code indentation.
    if cfg.vm.allow_password_login:
        if ':' in cfg.vm.password or '\n' in cfg.vm.password:
            raise RuntimeError(
                "VM password must not contain ':' or newlines (cloud-init chpasswd format)."
            )
        passwd_block = f"""
chpasswd:
  expire: false
  users:
    - name: {cfg.vm.user}
      password: {cfg.vm.password}
"""

    cloud = f"""#cloud-config
datasource_list: [ NoCloud, None ]
datasource:
  NoCloud: {{}}

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
bootcmd:
  - [bash, -lc, "systemctl mask systemd-networkd-wait-online.service NetworkManager-wait-online.service || true"]

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

runcmd:
  - systemctl mask --now systemd-networkd-wait-online.service NetworkManager-wait-online.service || true
  - systemctl enable --now ssh
  - systemctl enable --now unattended-upgrades || true
"""

    meta = f"""instance-id: {cfg.vm.name}
local-hostname: {cfg.vm.name}
"""
    netcfg = """version: 2
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

    run_cmd(['mkdir', '-p', str(ci_dir)], sudo=True, check=True, capture=True)
    _ensure_qemu_access(cfg, dry_run=False)
    run_cmd(
        ['bash', '-lc', f"cat > {user_data} <<'EOF'\n{cloud}\nEOF"],
        sudo=True,
        check=True,
        capture=True,
    )
    run_cmd(
        ['bash', '-lc', f"cat > {meta_data} <<'EOF'\n{meta}\nEOF"],
        sudo=True,
        check=True,
        capture=True,
    )
    run_cmd(
        ['bash', '-lc', f"cat > {network_config} <<'EOF'\n{netcfg}\nEOF"],
        sudo=True,
        check=True,
        capture=True,
    )
    run_cmd(
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
        check=True,
        capture=True,
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
    if _sudo_path_exists(vm_disk) and recreate:
        if dry_run:
            log.info('DRYRUN: rm -f {}', vm_disk)
        else:
            run_cmd(
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
    run_cmd(
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

    if vm_exists(cfg, dry_run=dry_run):
        if not recreate:
            st = (
                run_cmd(
                    ['virsh', 'domstate', cfg.vm.name],
                    sudo=True,
                    check=False,
                    capture=True,
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
            run_cmd(
                ['virsh', 'start', cfg.vm.name],
                sudo=True,
                check=True,
                capture=True,
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
    except Exception:
        # TODO: better handling of errors when the user does not have the
        # prerequisites installed. Tell them to run `aivm host install_deps`
        raise

    vm_disk = _ensure_disk(cfg, base_img, dry_run=dry_run, recreate=recreate)
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
        '--boot',
        'uefi',
        *extra,
    ]
    if dry_run:
        log.info('DRYRUN: {}', ' '.join(cmd))
        return
    try:
        first = run_cmd(cmd, sudo=True, check=False, capture=True)
    except CmdError as ex:
        # Some call sites/tests may still raise even when check=False.
        first = ex.result
    if first.code != 0:
        ex = CmdError(cmd, first)
        if source_dir and _is_missing_virtiofsd_error(ex):
            raise RuntimeError(_virtiofsd_failure_message(source_dir)) from ex
        if _is_guest_memory_allocation_error(ex):
            raise RuntimeError(_memory_allocation_failure_message(cfg)) from ex
        if _is_missing_uefi_firmware_error(ex):
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
                run_cmd(cmd_no_uefi, sudo=True, check=True, capture=True)
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
            raise ex
    log.info('VM created: {}', cfg.vm.name)


def _mac_for_vm(cfg: AgentVMConfig) -> str:
    res = run_cmd(
        ['virsh', 'domiflist', cfg.vm.name],
        sudo=True,
        check=False,
        capture=True,
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
    while time.time() < deadline:
        ip = ''
        lease_text = ''
        domif_text = ''
        if mac:
            lease_text = run_cmd(
                ['virsh', 'net-dhcp-leases', cfg.network.name],
                sudo=True,
                check=False,
                capture=True,
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
            domif_text = run_cmd(
                ['virsh', 'domifaddr', cfg.vm.name],
                sudo=True,
                check=False,
                capture=True,
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
            ip_file.write_text(ip + '\n', encoding='utf-8')
            log.info('VM IP: {} (saved to {})', ip, ip_file)
            return ip
        if cached_ip:
            ssh_probe = run_cmd(
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
                ip_file.write_text(cached_ip + '\n', encoding='utf-8')
                log.info(
                    'VM reachable via cached IP fallback: {} (saved to {})',
                    cached_ip,
                    ip_file,
                )
                return cached_ip
        now = time.time()
        if now >= next_status_at:
            st = run_cmd(
                ['virsh', 'domstate', cfg.vm.name],
                sudo=True,
                check=False,
                capture=True,
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


def destroy_vm(cfg: AgentVMConfig, *, dry_run: bool = False) -> None:
    name = cfg.vm.name
    if dry_run:
        log.info('DRYRUN: virsh destroy/undefine {}', name)
        return
    _destroy_and_undefine_vm(name)
    log.info('VM removed: {}', name)


def vm_status(cfg: AgentVMConfig) -> str:
    name = cfg.vm.name
    dom = run_cmd(
        ['virsh', 'dominfo', name], sudo=True, check=False, capture=True
    )
    if dom.code != 0:
        return f'VM not found: {name}\n'
    state = run_cmd(
        ['virsh', 'domstate', name], sudo=True, check=False, capture=True
    ).stdout.strip()
    ip = get_ip_cached(cfg) or ''
    return (
        dom.stdout + f'\nstate={state}\n' + (f'cached_ip={ip}\n' if ip else '')
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
        res = run_cmd(cmd, sudo=False, check=False, capture=True)
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
    remote = (
        'set -euo pipefail; '
        'sudo apt-get update -y; '
        'sudo apt-get install -y software-properties-common >/dev/null 2>&1 || true; '
        'sudo add-apt-repository -y universe >/dev/null 2>&1 || true; '
        'sudo apt-get update -y; '
        f'sudo DEBIAN_FRONTEND=noninteractive apt-get install -y {" ".join(docker_pkgs + pkgs)}'
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
    run_cmd(cmd, sudo=False, check=True, capture=False)
    log.info('Provisioning complete.')
