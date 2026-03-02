"""Opt-in end-to-end smoke test for nested VM workflows."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from aivm.config import DEFAULT_UBUNTU_NOBLE_IMG_URL, AgentVMConfig
from aivm.store import Store, save_store, upsert_vm


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
        tail = ''.join(out_lines[-200:])
        raise AssertionError(
            f'CLI timed out after {timeout_s}s: {" ".join(cmd)}\n'
            f'--- output tail ---\n{tail}\n'
        )
    stdout = ''.join(out_lines)
    completed = subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr='')
    if check and completed.returncode != 0:
        raise AssertionError(
            f'CLI failed: {" ".join(cmd)}\n'
            f'rc={completed.returncode}\n'
            f'--- output ---\n{completed.stdout}\n'
        )
    return completed


def _ensure_user_cached_image(shared_img: Path) -> None:
    if shared_img.exists():
        print(f'Using cached base image: {shared_img}')
        return
    shared_img.parent.mkdir(parents=True, exist_ok=True)
    print(f'Caching base image once for E2E: {shared_img}')
    proc = subprocess.run(
        [
            'curl',
            '-L',
            '--fail',
            '--progress-bar',
            '-o',
            str(shared_img),
            DEFAULT_UBUNTU_NOBLE_IMG_URL,
        ],
        check=False,
        capture_output=False,
        text=True,
    )
    if proc.returncode != 0:
        raise AssertionError(
            f'Failed to populate shared e2e image cache at: {shared_img}'
        )


def test_e2e_nested_smoke(tmp_path: Path) -> None:
    if os.getenv('AIVM_E2E') != '1':
        pytest.skip('Set AIVM_E2E=1 to run nested e2e smoke test.')

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
    cfg_path = tmp_path / 'e2e-config.toml'

    suffix = uuid.uuid4().hex[:6]
    subnet_octet = 100 + (int(suffix[:2], 16) % 100)

    cfg = AgentVMConfig()
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
        default_shared = (
            user_home / '.cache' / 'aivm' / 'e2e' / 'noble-base.img'
        )
        shared_img = Path(
            os.getenv('AIVM_E2E_SHARED_IMAGE', str(default_shared))
        ).expanduser()
        _ensure_user_cached_image(shared_img)
        # Avoid repeated network downloads during e2e by sourcing from user cache.
        cfg.image.ubuntu_img_url = f'file://{shared_img}'

    store = Store()
    upsert_vm(store, cfg)
    save_store(store, cfg_path)

    timeout_s = int(os.getenv('AIVM_E2E_TIMEOUT', '2400'))

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
