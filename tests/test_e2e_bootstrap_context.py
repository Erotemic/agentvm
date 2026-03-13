"""Opt-in bootstrap-context e2e test.

This test validates a two-level flow:

1) current host context creates a fresh outer VM
2) inside that outer VM, a small set of documented user workflows are run

It is intentionally opt-in because runtime is long and environment requirements
are strict.
"""

from __future__ import annotations

import os
import subprocess
import textwrap
import uuid
from pathlib import Path

import pytest

from test_e2e_nested import (
    _make_temp_ssh_material,
    _run_cli,
)

from aivm.config import AgentVMConfig
from aivm.store import Store, save_store, upsert_vm


def _bootstrap_context_enabled() -> bool:
    raw = os.getenv('AIVM_E2E_BOOTSTRAP', '0')
    return str(raw).strip().lower() in {'1', 'true', 'yes', 'on'}


def _run_remote_script(
    *,
    user: str,
    ip: str,
    identity_file: Path,
    env: dict[str, str],
    timeout_s: int,
    script: str,
) -> subprocess.CompletedProcess[str]:
    # Execute the inner bootstrap payload over SSH while streaming logs back to
    # pytest output so long-running nested failures are diagnosable.
    cmd = [
        'ssh',
        '-i',
        str(identity_file),
        '-o',
        'UserKnownHostsFile=/dev/null',
        '-o',
        'StrictHostKeyChecking=accept-new',
        '-o',
        'BatchMode=yes',
        '-o',
        'ConnectTimeout=20',
        '-o',
        'ConnectionAttempts=3',
        '-o',
        'ServerAliveInterval=15',
        '-o',
        'ServerAliveCountMax=8',
        f'{user}@{ip}',
        'bash',
        '-euxo',
        'pipefail',
        '-s',
    ]
    proc = subprocess.Popen(
        cmd,
        env=env,
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    out_lines: list[str] = []
    try:
        assert proc.stdin is not None
        proc.stdin.write(script)
        if not script.endswith('\n'):
            proc.stdin.write('\n')
        proc.stdin.close()
        assert proc.stdout is not None
        for line in iter(proc.stdout.readline, ''):
            if not line:
                break
            out_lines.append(line)
            print(line, end='')
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)
        tail = ''.join(out_lines[-200:])
        raise AssertionError(
            f'Remote bootstrap script timed out after {timeout_s}s.\n'
            f'--- output tail ---\n{tail}\n'
        )
    finally:
        if proc.stdin is not None and not proc.stdin.closed:
            proc.stdin.close()
        if proc.stdout is not None:
            proc.stdout.close()
    stdout = ''.join(out_lines)
    completed = subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr='')
    if completed.returncode != 0:
        raise AssertionError(
            'Remote bootstrap script failed.\n'
            f'rc={completed.returncode}\n'
            f'--- output ---\n{completed.stdout}\n'
        )
    return completed


def test_e2e_bootstrap_context(tmp_path: Path) -> None:
    # Bootstrap-context e2e validates "fresh machine" onboarding by creating a
    # first-layer VM, installing aivm there, and exercising the documented
    # non-interactive workflows from that clean environment.
    if not _bootstrap_context_enabled():
        pytest.skip('Set AIVM_E2E_BOOTSTRAP=1 to run bootstrap-context e2e.')
    if os.getenv('AIVM_E2E') != '1':
        pytest.skip('Set AIVM_E2E=1 to run bootstrap-context e2e.')

    home, priv, pub = _make_temp_ssh_material(tmp_path)
    env = os.environ.copy()
    env['HOME'] = str(home)
    cli_verbosity = int(os.getenv('AIVM_E2E_CLI_VERBOSITY', '2'))
    cli_verbosity_args = (
        [f'--verbose={cli_verbosity}'] if cli_verbosity > 0 else []
    )

    sudo_probe = subprocess.run(
        ['sudo', '-n', 'true'], check=False, capture_output=True, text=True
    )
    if sudo_probe.returncode != 0:
        detail = (sudo_probe.stderr or sudo_probe.stdout or '').strip()
        raise AssertionError(
            'Bootstrap e2e requires passwordless sudo (`sudo -n true` failed).'
            + (f'\n--- sudo output ---\n{detail}\n' if detail else '')
        )

    repo_root = Path(__file__).resolve().parent.parent
    timeout_s = int(os.getenv('AIVM_E2E_TIMEOUT', '3600'))
    doctor = _run_cli(
        ['host', 'doctor', *cli_verbosity_args, '--sudo'],
        cwd=repo_root,
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

    cfg_path = tmp_path / 'e2e-bootstrap.toml'
    suffix = uuid.uuid4().hex[:6]
    subnet_octet = 100 + (int(suffix[:2], 16) % 100)
    cfg = AgentVMConfig()
    # Keep outer bootstrap VM/network isolated from host-context e2e fixtures.
    cfg.vm.name = f'aivm-e2e-bootstrap-{suffix}'
    cfg.vm.cpus = 2
    cfg.vm.ram_mb = 4096
    cfg.vm.disk_gb = 24
    cfg.network.name = f'aivm-e2e-boot-net-{suffix}'
    cfg.network.bridge = f'vbrb{suffix[:4]}'
    cfg.network.subnet_cidr = f'10.251.{subnet_octet}.0/24'
    cfg.network.gateway_ip = f'10.251.{subnet_octet}.1'
    cfg.network.dhcp_start = f'10.251.{subnet_octet}.100'
    cfg.network.dhcp_end = f'10.251.{subnet_octet}.200'
    cfg.firewall.enabled = False
    cfg.provision.enabled = False
    cfg.paths.base_dir = '/var/lib/libvirt/aivm-e2e-bootstrap'
    cfg.paths.state_dir = str(tmp_path / 'state')
    cfg.paths.ssh_identity_file = str(priv)
    cfg.paths.ssh_pubkey_path = str(pub)

    store = Store()
    upsert_vm(store, cfg)
    save_store(store, cfg_path)

    guest_repo_path = '/workspace/aivm'
    inner_timeout_s = int(os.getenv('AIVM_E2E_BOOTSTRAP_TIMEOUT', '7200'))
    inner_wait_ip_timeout_s = int(
        os.getenv('AIVM_E2E_BOOTSTRAP_WAIT_IP_TIMEOUT', '1200')
    )
    remote_script = textwrap.dedent(
        f"""\
        # Minimal first-layer bootstrap: install this repo as a tool in the
        # fresh guest, prepare host deps there, then exercise the documented
        # user workflows directly instead of nesting the full e2e suite again.
        DEBIAN_FRONTEND=noninteractive
        export DEBIAN_FRONTEND
        if [ ! -e /dev/kvm ]; then
          echo "ERROR: /dev/kvm missing in bootstrap guest. Nested virtualization unavailable."
          exit 1
        fi
        # Cloud-init may hold apt/dpkg locks briefly on first boot.
        sudo cloud-init status --wait || true
        retry() {{
          local n=0
          local max=12
          until "$@"; do
            n=$((n + 1))
            if [ "$n" -ge "$max" ]; then
              echo "ERROR: command failed after retries: $*"
              return 1
            fi
            sleep 5
          done
        }}
        retry sudo apt-get update -y
        sudo apt-get install -y software-properties-common >/dev/null 2>&1 || true
        sudo add-apt-repository -y universe >/dev/null 2>&1 || true
        retry sudo apt-get update -y
        retry sudo apt-get install -y \\
          ca-certificates curl \\
          python3 python3-venv python3-pip
        export PATH="$HOME/.local/bin:$PATH"
        if ! command -v uv >/dev/null 2>&1; then
          curl -LsSf https://astral.sh/uv/install.sh | sh
        fi
        export PATH="$HOME/.local/bin:$PATH"
        mkdir -p "$HOME/.ssh" "$HOME/.venvs"
        if [ ! -f "$HOME/.ssh/id_ed25519" ]; then
          ssh-keygen -q -t ed25519 -N '' -f "$HOME/.ssh/id_ed25519"
        fi
        cd {guest_repo_path}
        uv venv --clear "$HOME/.venvs/aivm-e2e"
        . "$HOME/.venvs/aivm-e2e/bin/activate"
        uv pip install -e .
        cleanup() {{
          python -m aivm vm destroy {' '.join(cli_verbosity_args)} --yes || true
          python -m aivm host net destroy {' '.join(cli_verbosity_args)} --yes || true
        }}
        trap cleanup EXIT
        python -m aivm host install_deps {' '.join(cli_verbosity_args)} --yes
        python -m aivm host doctor {' '.join(cli_verbosity_args)} --sudo
        mkdir -p "$HOME/workspace/project"
        cd "$HOME/workspace/project"
        printf 'bootstrap test\\n' > README.txt
        python -m aivm help tree
        python -m aivm config init {' '.join(cli_verbosity_args)} --defaults --yes --force
        python -m aivm config path
        python -m aivm vm create {' '.join(cli_verbosity_args)} --yes --force
        # Nested guests can take materially longer to reach DHCP than the
        # direct-host e2e path, so give the bootstrap workflow its own budget.
        python -m aivm vm wait_ip {' '.join(cli_verbosity_args)} --timeout {inner_wait_ip_timeout_s} --yes
        python -m aivm status {' '.join(cli_verbosity_args)}
        python -m aivm status {' '.join(cli_verbosity_args)} --sudo --yes
        python -m aivm attach {' '.join(cli_verbosity_args)} . --yes
        if python -m aivm attach {' '.join(cli_verbosity_args)} . --mode git --yes; then
          echo "ERROR: expected mode mismatch when changing existing attachment mode without detach"
          exit 1
        fi
        python -m aivm detach {' '.join(cli_verbosity_args)} . --yes
        python -m aivm attach {' '.join(cli_verbosity_args)} . --mode shared-root --yes
        python -m aivm list {' '.join(cli_verbosity_args)}
        python -m aivm vm ssh_config {' '.join(cli_verbosity_args)}
        python -m aivm vm update {' '.join(cli_verbosity_args)} --yes
        """
    )

    try:
        # Bring up the first-layer guest, mount the repo, then exercise the
        # documented nested bootstrap workflow inside it.
        _run_cli(
            [
                'host',
                'net',
                'create',
                *cli_verbosity_args,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        _run_cli(
            [
                'vm',
                'up',
                *cli_verbosity_args,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        wait_res = _run_cli(
            [
                'vm',
                'wait_ip',
                *cli_verbosity_args,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        ip = wait_res.stdout.strip().splitlines()[-1]
        _run_cli(
            [
                'vm',
                'attach',
                *cli_verbosity_args,
                str(repo_root),
                '--guest_dst',
                guest_repo_path,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        _run_remote_script(
            user=cfg.vm.user,
            ip=ip,
            identity_file=priv,
            env=env,
            timeout_s=inner_timeout_s,
            script=remote_script,
        )
    finally:
        _run_cli(
            [
                'vm',
                'destroy',
                *cli_verbosity_args,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
            check=False,
        )
        _run_cli(
            [
                'host',
                'net',
                'destroy',
                *cli_verbosity_args,
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
            check=False,
        )
