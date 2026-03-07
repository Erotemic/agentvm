"""Opt-in end-to-end smoke test for nested VM workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from hashlib import sha256
from pathlib import Path
from urllib.parse import urlparse

import pytest

from aivm.config import (
    DEFAULT_UBUNTU_NOBLE_IMG_URL,
    SUPPORTED_IMAGE_SHA256,
    AgentVMConfig,
)
from aivm.store import Store, save_store, upsert_vm


def _host_context_enabled() -> bool:
    raw = os.getenv(
        'AIVM_E2E_HOST_CONTEXT',
        '1' if os.getenv('AIVM_E2E') == '1' else '0',
    )
    return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'}


def _make_temp_ssh_material(tmp_path: Path) -> tuple[Path, Path, Path]:
    ssh_keygen = shutil.which('ssh-keygen')
    if not ssh_keygen:
        pytest.skip('ssh-keygen is required for e2e key generation.')

    home = tmp_path / 'home'
    ssh_dir = home / '.ssh'
    ssh_dir.mkdir(parents=True, exist_ok=True)
    priv = ssh_dir / 'id_ed25519'
    pub = ssh_dir / 'id_ed25519.pub'
    cfg = ssh_dir / 'config'
    proc = subprocess.run(
        [ssh_keygen, '-q', '-t', 'ed25519', '-N', '', '-f', str(priv)],
        check=False,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f'Failed to generate temp ssh keypair.\n'
            f'--- stdout ---\n{proc.stdout}\n'
            f'--- stderr ---\n{proc.stderr}\n'
        )
    cfg.write_text(
        (
            'Host *\n'
            f'  IdentityFile {priv}\n'
            '  IdentitiesOnly yes\n'
            '  StrictHostKeyChecking accept-new\n'
        ),
        encoding='utf-8',
    )
    return home, priv, pub


def _run_cli(
    argv: list[str],
    *,
    cwd: Path,
    timeout_s: int,
    env: dict[str, str],
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    # Centralize CLI invocation behavior for e2e so failures always include a
    # command line, return code, and recent output tail.
    cmd = [sys.executable, '-m', 'aivm', *argv]
    proc = subprocess.Popen(
        cmd,
        cwd=str(cwd),
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out_lines: list[str] = []
    try:
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            out_lines.append(line)
            # Preserve real-time output when pytest is run with -s.
            sys.stdout.write(line)
            sys.stdout.flush()
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        tail = ''.join(out_lines[-200:])
        raise AssertionError(
            f'CLI timed out after {timeout_s}s: {" ".join(cmd)}\n'
            f'--- output tail ---\n{tail}\n'
        )
    finally:
        if proc.stdout is not None:
            proc.stdout.close()
    stdout = ''.join(out_lines)
    completed = subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr='')
    if check and completed.returncode != 0:
        raise AssertionError(
            f'CLI failed: {" ".join(cmd)}\n'
            f'rc={completed.returncode}\n'
            f'--- output ---\n{completed.stdout}\n'
        )
    return completed


def _require_e2e_host_dependencies(
    *, cwd: Path, timeout_s: int, env: dict[str, str]
) -> None:
    # Fail fast with actionable guidance before spending minutes on VM setup.
    doctor = _run_cli(
        ['host', 'doctor', '--sudo'],
        cwd=cwd,
        timeout_s=timeout_s,
        env=env,
        check=False,
    )
    if doctor.returncode != 0:
        raise AssertionError(
            'E2E host dependencies are not ready. '
            '`aivm host doctor --sudo` failed.\n'
            'Install missing dependencies (e.g. `aivm host install_deps --yes`) '
            'and rerun.\n'
            f'--- output ---\n{doctor.stdout}\n'
        )


def _sha256_file(path: Path) -> str:
    hasher = sha256()
    with path.open('rb') as file:
        while True:
            block = file.read(1024 * 1024)
            if not block:
                break
            hasher.update(block)
    return hasher.hexdigest()


def _default_shared_image_path(user_home: Path) -> Path:
    # Keep the filename version-aware so new pinned cloud image releases can
    # coexist in cache without clobbering old files.
    parsed = urlparse(DEFAULT_UBUNTU_NOBLE_IMG_URL)
    basename = Path(parsed.path).name
    parts = [p for p in parsed.path.split('/') if p]
    version = parts[-2] if len(parts) >= 2 else 'unknown'
    name = f'{Path(basename).stem}-{version}{Path(basename).suffix}'
    return user_home / '.cache' / 'aivm' / 'e2e' / name


def _ensure_user_cached_image(shared_img: Path) -> None:
    # Maintain a single verified local copy per test host to avoid repeated
    # network downloads across e2e runs.
    expected = SUPPORTED_IMAGE_SHA256[DEFAULT_UBUNTU_NOBLE_IMG_URL]
    if shared_img.exists():
        actual = _sha256_file(shared_img)
        if actual == expected:
            print(f'Using cached base image: {shared_img}')
            return
        print(
            'Cached base image checksum mismatch; removing stale cache '
            f'and redownloading: {shared_img}'
        )
        shared_img.unlink()
    shared_img.parent.mkdir(parents=True, exist_ok=True)
    tmp_img = Path(str(shared_img) + '.part')
    if tmp_img.exists():
        tmp_img.unlink()
    print(f'Caching base image once for E2E: {shared_img}')
    proc = subprocess.run(
        [
            'curl',
            '-L',
            '--fail',
            '--progress-bar',
            '-o',
            str(tmp_img),
            DEFAULT_UBUNTU_NOBLE_IMG_URL,
        ],
        check=False,
        capture_output=False,
        text=True,
    )
    if proc.returncode != 0:
        tmp_img.unlink(missing_ok=True)
        raise AssertionError(
            f'Failed to populate shared e2e image cache at: {shared_img}'
        )
    tmp_img.replace(shared_img)
    actual = _sha256_file(shared_img)
    if actual != expected:
        shared_img.unlink(missing_ok=True)
        raise AssertionError(
            'E2E shared image cache has unexpected checksum after download.\n'
            f'Path: {shared_img}\n'
            f'Expected: {expected}\n'
            f'Actual:   {actual}'
        )


def test_e2e_nested_smoke(tmp_path: Path) -> None:
    # Host-context e2e asserts the "normal developer machine" flow where tests
    # run directly on the host and manage one guest VM lifecycle.
    if not _host_context_enabled():
        pytest.skip(
            'Set AIVM_E2E_HOST_CONTEXT=1 (and AIVM_E2E=1) to run host-context e2e tests.'
        )

    home, priv, pub = _make_temp_ssh_material(tmp_path)
    env = os.environ.copy()
    env['HOME'] = str(home)

    # Require non-interactive sudo for unattended e2e.
    sudo_probe = subprocess.run(
        ['sudo', '-n', 'true'], check=False, capture_output=True, text=True
    )
    if sudo_probe.returncode != 0:
        pytest.skip('E2E requires passwordless sudo (sudo -n true).')

    repo_root = Path(__file__).resolve().parent.parent
    timeout_s = int(os.getenv('AIVM_E2E_TIMEOUT', '2400'))
    _require_e2e_host_dependencies(cwd=repo_root, timeout_s=timeout_s, env=env)

    cfg_path = tmp_path / 'e2e-config.toml'

    suffix = uuid.uuid4().hex[:6]
    subnet_octet = 100 + (int(suffix[:2], 16) % 100)

    cfg = AgentVMConfig()
    # Use unique names/subnets per run so retries and concurrent runs don't
    # collide with stale libvirt resources.
    cfg.vm.name = f'aivm-e2e-{suffix}'
    cfg.vm.cpus = 1
    cfg.vm.ram_mb = 2048
    cfg.vm.disk_gb = 16
    cfg.network.name = f'aivm-e2e-net-{suffix}'
    cfg.network.bridge = f'vbr{suffix}'
    cfg.network.subnet_cidr = f'10.250.{subnet_octet}.0/24'
    cfg.network.gateway_ip = f'10.250.{subnet_octet}.1'
    cfg.network.dhcp_start = f'10.250.{subnet_octet}.100'
    cfg.network.dhcp_end = f'10.250.{subnet_octet}.200'
    cfg.firewall.enabled = False
    cfg.provision.enabled = False
    cfg.paths.base_dir = '/var/lib/libvirt/aivm-e2e'
    cfg.paths.state_dir = str(tmp_path / 'state')
    cfg.paths.ssh_identity_file = str(priv)
    cfg.paths.ssh_pubkey_path = str(pub)

    if os.getenv('AIVM_E2E_INDEPENDENT_IMAGE') != '1':
        user_home = Path(os.environ.get('HOME', '~')).expanduser()
        default_shared = _default_shared_image_path(user_home)
        shared_img = Path(
            os.getenv('AIVM_E2E_SHARED_IMAGE', str(default_shared))
        ).expanduser()
        _ensure_user_cached_image(shared_img)
        # Avoid repeated network downloads during e2e by sourcing from user cache.
        cfg.image.ubuntu_img_url = f'file://{shared_img}'

    store = Store()
    upsert_vm(store, cfg)
    save_store(store, cfg_path)

    try:
        _run_cli(
            ['host', 'net', 'create', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        _run_cli(
            ['vm', 'up', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        wait_res = _run_cli(
            ['vm', 'wait_ip', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        status_res = _run_cli(
            ['status', '--sudo', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        combined = (wait_res.stdout + '\n' + status_res.stdout).lower()
        assert 'vm ip' in combined or 'cached vm ip' in combined
        assert 'vm state' in combined
    finally:
        _run_cli(
            ['vm', 'destroy', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
            check=False,
        )
        _run_cli(
            ['host', 'net', 'destroy', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
            check=False,
        )
