"""More comprehensive end-to-end tests for local execution.

These exercises go well beyond the minimal "smoke" path in
`test_e2e_nested.py` and are intended for developers running against
real libvirt/KVM hosts (hence guarded by ``AIVM_E2E=1``).  The goal is
not to replace the fast unit tests with mocks, but rather provide a
suite that puts the actual CLI and VM/network plumbing through its
paces when you have a capable environment available.

Because the existing nested smoke test exports a few helpers, we
import them here rather than duplicate them.  If the shape of those
helpers changes the import will fail and you'll be reminded to keep the
modules in sync.
"""

from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path

import pytest

# import helpers from the sibling smoke test; pytest adds the
# repository root to sys.path so we can import the module directly by
# name rather than treating `tests` as a package.
from test_e2e_nested import (
    _default_shared_image_path,
    _ensure_user_cached_image,
    _host_context_enabled,
    _make_temp_ssh_material,
    _require_e2e_host_dependencies,
    _run_cli,
)

from aivm.config import AgentVMConfig
from aivm.store import (
    Store,
    find_attachment_for_vm,
    load_store,
    save_store,
    upsert_vm,
)

# Re‑use the same helper logic from test_e2e_nested.


def test_e2e_full_cycle(tmp_path: Path) -> None:
    """Full end-to-end exercise.

    This test drives the CLI through:

    * network creation
    * VM creation/start
    * IP discovery
    * status probing
    * SSH connectivity (via "vm ssh")
    * provisioning
    * firewall inspection
    * attaching a host folder
    * session cleanup (vm destroy, network destroy)

    The test is fairly slow and requires a host with libvirt/KVM and
    passwordless sudo; it is skipped unless ``AIVM_E2E=1``.
    """

    if not _host_context_enabled():
        pytest.skip(
            'Set AIVM_E2E_HOST_CONTEXT=1 (and AIVM_E2E=1) to run host-context e2e tests.'
        )

    home, priv, pub = _make_temp_ssh_material(tmp_path)
    env = os.environ.copy()
    env['HOME'] = str(home)

    # Ensure sudo can be used non‑interactively.
    sudo_probe = subprocess.run(
        ['sudo', '-n', 'true'], check=False, capture_output=True, text=True
    )
    if sudo_probe.returncode != 0:
        pytest.skip('E2E requires passwordless sudo (sudo -n true).')

    repo_root = Path(__file__).resolve().parent.parent
    timeout_s = int(os.getenv('AIVM_E2E_TIMEOUT', '2400'))
    _require_e2e_host_dependencies(cwd=repo_root, timeout_s=timeout_s, env=env)

    cfg_path = tmp_path / 'e2e-full.toml'

    suffix = uuid.uuid4().hex[:6]
    subnet_octet = 100 + (int(suffix[:2], 16) % 100)

    cfg = AgentVMConfig()
    # Use per-run unique identifiers to avoid collisions with prior failed e2e
    # runs or concurrently executing workers.
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
    cfg.firewall.enabled = True
    cfg.firewall.allow_tcp_ports = [22, 2222]
    # provisioning is intentionally disabled here to avoid intermittent
    # apt lock conflicts during testing.  We still invoke the CLI with
    # --dry_run later to exercise the code path.
    cfg.provision.enabled = False
    cfg.paths.base_dir = '/var/lib/libvirt/aivm-e2e'
    cfg.paths.state_dir = str(tmp_path / 'state')
    cfg.paths.ssh_identity_file = str(priv)
    cfg.paths.ssh_pubkey_path = str(pub)

    # reuse shared image cache unless explicitly disabled
    if os.getenv('AIVM_E2E_INDEPENDENT_IMAGE') != '1':
        user_home = Path(os.environ.get('HOME', '~')).expanduser()
        default_shared = _default_shared_image_path(user_home)
        shared_img = Path(
            os.getenv('AIVM_E2E_SHARED_IMAGE', str(default_shared))
        ).expanduser()
        _ensure_user_cached_image(shared_img)
        cfg.image.ubuntu_img_url = f'file://{shared_img}'

    # host folder we will later attach to the VM
    share_dir = tmp_path / 'hostshare'
    share_dir.mkdir()
    (share_dir / 'flag.txt').write_text('e2e', encoding='utf-8')

    store = Store()
    upsert_vm(store, cfg)
    save_store(store, cfg_path)

    # wrap the main operations so we can always clean up.
    try:
        # network/VM lifecycle
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
        # CLI prints logs before the IP; take last non-empty line as the
        # address.
        ip = wait_res.stdout.strip().splitlines()[-1]
        _run_cli(
            ['host', 'fw', 'status', '--yes', '--config', str(cfg_path)],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )

        # verify ssh connectivity by running a simple remote command using
        # the generated key pair rather than going through the CLI.  The CLI
        # `vm ssh` helper always launches an interactive shell and doesn't
        # accept an arbitrary command, so it's unsuitable for automated
        # checks.
        ssh_cmd = [
            'ssh',
            '-i',
            str(priv),
            '-o',
            'UserKnownHostsFile=/dev/null',
            '-o',
            'StrictHostKeyChecking=accept-new',
            f'{cfg.vm.user}@{ip}',
            'ls',
            '/',
        ]
        proc = subprocess.run(
            ssh_cmd,
            cwd=str(repo_root),
            env=env,
            check=False,
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            raise AssertionError(
                'SSH connectivity check failed:\n'
                f'cmd: {ssh_cmd}\n'
                f'rc={proc.returncode}\n'
                f'stdout={proc.stdout}\n'
                f'stderr={proc.stderr}\n'
            )

        # exercise the provision command in dry-run mode (we disabled
        # real provisioning above to avoid flakes with apt locks).
        _run_cli(
            [
                'vm',
                'provision',
                '--yes',
                '--dry_run',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )

        # Attach with no explicit mode and verify the new default is shared-root.
        _run_cli(
            [
                'vm',
                'attach',
                str(share_dir),
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        reg_after_default_attach = load_store(cfg_path)
        default_att = find_attachment_for_vm(
            reg_after_default_attach, share_dir, cfg.vm.name
        )
        assert default_att is not None
        assert default_att.mode == 'shared-root'

        # Explicit mode disagreement on existing attachment should error.
        mismatch_res = _run_cli(
            [
                'vm',
                'attach',
                str(share_dir),
                '--mode',
                'git',
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
            check=False,
        )
        assert mismatch_res.returncode != 0
        assert 'Attachment mode mismatch' in mismatch_res.stdout
        print(
            'Verified expected failure: conflicting attachment mode request '
            'is rejected with a detach+reattach guidance message.'
        )

        # Detach then reattach explicitly in shared-root mode.
        _run_cli(
            [
                'vm',
                'detach',
                str(share_dir),
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        reg_after_detach = load_store(cfg_path)
        assert (
            find_attachment_for_vm(reg_after_detach, share_dir, cfg.vm.name)
            is None
        )
        _run_cli(
            [
                'vm',
                'attach',
                str(share_dir),
                '--mode',
                'shared-root',
                '--yes',
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
        reg_after_shared_root_attach = load_store(cfg_path)
        shared_root_att = find_attachment_for_vm(
            reg_after_shared_root_attach, share_dir, cfg.vm.name
        )
        assert shared_root_att is not None
        assert shared_root_att.mode == 'shared-root'

        # sync a tiny settings file to ensure the sync code path runs
        test_sync = tmp_path / 'testrc'
        test_sync.write_text('echo hi', encoding='utf-8')
        _run_cli(
            [
                'vm',
                'sync_settings',
                '--yes',
                '--paths',
                str(test_sync),
                '--config',
                str(cfg_path),
            ],
            cwd=repo_root,
            timeout_s=timeout_s,
            env=env,
        )
    finally:
        # teardown
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
