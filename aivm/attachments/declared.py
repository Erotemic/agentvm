"""Declarative shared-root attachment helpers.

This mode keeps the host-side shared-root bind staging stable while persisting
the desired guest-visible bind mounts as declarations that can be replayed at
boot or during lightweight reconcile.
"""

from __future__ import annotations

import json
import shlex
import textwrap
from dataclasses import asdict, dataclass
from pathlib import Path

from ..commands import CommandManager
from ..config import AgentVMConfig
from ..runtime import require_ssh_identity, ssh_base_args
from ..store import find_attachments_for_vm, load_store
from ..vm.share import ResolvedAttachment
from .resolve import (
    ATTACHMENT_ACCESS_RO,
    ATTACHMENT_MODE_DECLARED,
)
from .shared_root import (
    SHARED_ROOT_GUEST_MOUNT_ROOT,
    _shared_root_host_dir,
)

DECLARED_ATTACHMENT_HOST_META_DIR = '.aivm'
DECLARED_ATTACHMENT_HOST_MANIFEST_NAME = 'declared-attachments.json'
DECLARED_ATTACHMENT_GUEST_STATE_DIR = '/var/lib/aivm'
DECLARED_ATTACHMENT_GUEST_STATE_PATH = (
    f'{DECLARED_ATTACHMENT_GUEST_STATE_DIR}/attachments.json'
)
DECLARED_ATTACHMENT_REPLAY_BIN = '/usr/local/libexec/aivm-attachment-replay'
DECLARED_ATTACHMENT_REPLAY_SERVICE = 'aivm-attachment-replay.service'


@dataclass(frozen=True)
class DeclaredAttachmentRecord:
    attachment_id: str
    mode: str
    source_dir: str
    host_lexical_path: str
    shared_root_token: str
    guest_dst: str
    access: str
    enabled: bool = True


def _declared_host_meta_dir(cfg: AgentVMConfig) -> Path:
    return _shared_root_host_dir(cfg) / DECLARED_ATTACHMENT_HOST_META_DIR


def _declared_host_manifest_path(cfg: AgentVMConfig) -> Path:
    return _declared_host_meta_dir(cfg) / DECLARED_ATTACHMENT_HOST_MANIFEST_NAME


def _declared_attachment_records_for_vm(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> list[DeclaredAttachmentRecord]:
    reg = load_store(cfg_path)
    records: list[DeclaredAttachmentRecord] = []
    for att in find_attachments_for_vm(reg, cfg.vm.name):
        if str(att.mode or '').strip() != ATTACHMENT_MODE_DECLARED:
            continue
        records.append(
            DeclaredAttachmentRecord(
                attachment_id=str(att.tag or att.host_path),
                mode=str(att.mode or ATTACHMENT_MODE_DECLARED),
                source_dir=str(att.host_path),
                host_lexical_path=str(att.host_lexical_path or ''),
                shared_root_token=str(att.tag or ''),
                guest_dst=str(att.guest_dst or ''),
                access=str(att.access or 'rw'),
                enabled=True,
            )
        )
    return sorted(records, key=lambda rec: (rec.guest_dst, rec.shared_root_token))


def _declared_attachment_manifest_text(
    cfg: AgentVMConfig,
    cfg_path: Path,
) -> str:
    records = _declared_attachment_records_for_vm(cfg, cfg_path)
    payload = {
        'schema_version': 1,
        'vm_name': cfg.vm.name,
        'shared_root_mount': SHARED_ROOT_GUEST_MOUNT_ROOT,
        'records': [asdict(rec) for rec in records],
    }
    return json.dumps(payload, indent=2, sort_keys=True) + '\n'


def _declared_replay_python() -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import shutil
        import subprocess
        import sys
        import tempfile
        from pathlib import Path, PurePosixPath

        SHARED_ROOT_TAG = "aivm-shared-root"
        SHARED_ROOT_MOUNT = "{SHARED_ROOT_GUEST_MOUNT_ROOT}"
        HOST_MANIFEST = str(PurePosixPath(SHARED_ROOT_MOUNT) / "{DECLARED_ATTACHMENT_HOST_META_DIR}" / "{DECLARED_ATTACHMENT_HOST_MANIFEST_NAME}")
        STATE_DIR = "{DECLARED_ATTACHMENT_GUEST_STATE_DIR}"
        STATE_PATH = "{DECLARED_ATTACHMENT_GUEST_STATE_PATH}"

        def run(cmd, check=True, capture=False):
            return subprocess.run(
                cmd,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )

        def mount_shared_root():
            os.makedirs(SHARED_ROOT_MOUNT, exist_ok=True)
            probe = subprocess.run(["mountpoint", "-q", SHARED_ROOT_MOUNT])
            if probe.returncode == 0:
                return
            run(["mount", "-t", "virtiofs", SHARED_ROOT_TAG, SHARED_ROOT_MOUNT])

        def load_json(path):
            try:
                with open(path, "r", encoding="utf-8") as file:
                    return json.load(file)
            except FileNotFoundError:
                return {{}}

        def atomic_write_json(path, payload):
            path = Path(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                "w",
                encoding="utf-8",
                dir=str(path.parent),
                delete=False,
            ) as file:
                json.dump(payload, file, indent=2, sort_keys=True)
                file.write("\\n")
                tmp_name = file.name
            os.chmod(tmp_name, 0o644)
            os.replace(tmp_name, path)

        def sync_manifest():
            previous = load_json(STATE_PATH)
            desired = previous
            if os.path.exists(HOST_MANIFEST):
                desired = load_json(HOST_MANIFEST)
                atomic_write_json(STATE_PATH, desired)
            return previous, desired

        def mount_source_for(record):
            token = str(record.get("shared_root_token") or "").strip()
            if not token:
                raise RuntimeError("declared attachment record missing shared_root_token")
            return str(PurePosixPath(SHARED_ROOT_MOUNT) / token)

        def desired_option(record):
            return "ro" if str(record.get("access") or "").strip() == "ro" else "rw"

        def unmount_guest_dst(guest_dst):
            probe = subprocess.run(["mountpoint", "-q", guest_dst])
            if probe.returncode != 0:
                return
            result = subprocess.run(
                ["umount", guest_dst],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            if result.returncode == 0:
                return
            message = ((result.stderr or "") + "\\n" + (result.stdout or "")).lower()
            if "not mounted" in message:
                return
            raise RuntimeError(
                f"could not unmount {{guest_dst}}: {{(result.stderr or result.stdout).strip()}}"
            )

        def ensure_record(record):
            guest_dst = str(record.get("guest_dst") or "").strip()
            if not guest_dst:
                raise RuntimeError("declared attachment record missing guest_dst")
            source = mount_source_for(record)
            if not os.path.isdir(source):
                raise RuntimeError(f"declared attachment source missing in shared root: {{source}}")
            current = subprocess.run(
                ["findmnt", "-n", "-o", "SOURCE", "--target", guest_dst],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if current.returncode == 0 and (current.stdout or "").strip() not in {{source, "none"}}:
                unmount_guest_dst(guest_dst)
            os.makedirs(guest_dst, exist_ok=True)
            if subprocess.run(["mountpoint", "-q", guest_dst]).returncode != 0:
                run(["mount", "--bind", source, guest_dst])
            run(["mount", "-o", f"remount,bind,{{desired_option(record)}}", guest_dst])

        def prune_removed(previous, desired):
            desired_ids = {{
                str(rec.get("attachment_id") or rec.get("shared_root_token") or "")
                for rec in desired.get("records", [])
                if rec.get("enabled", True)
            }}
            for rec in previous.get("records", []):
                rec_id = str(rec.get("attachment_id") or rec.get("shared_root_token") or "")
                if rec_id and rec_id in desired_ids and rec.get("enabled", True):
                    continue
                guest_dst = str(rec.get("guest_dst") or "").strip()
                if guest_dst:
                    unmount_guest_dst(guest_dst)

        def main():
            mount_shared_root()
            previous, desired = sync_manifest()
            prune_removed(previous, desired)
            failures = []
            for record in desired.get("records", []):
                if not record.get("enabled", True):
                    guest_dst = str(record.get("guest_dst") or "").strip()
                    if guest_dst:
                        unmount_guest_dst(guest_dst)
                    continue
                try:
                    ensure_record(record)
                except Exception as ex:  # pragma: no cover - guest runtime path
                    failures.append(str(ex))
            if failures:
                for item in failures:
                    print(item, file=sys.stderr)
                raise SystemExit(1)

        if __name__ == "__main__":
            main()
        """
    )


def _declared_replay_service_unit() -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=aivm declared attachment replay
        After=local-fs.target

        [Service]
        Type=oneshot
        ExecStart={DECLARED_ATTACHMENT_REPLAY_BIN}

        [Install]
        WantedBy=multi-user.target
        """
    )


def _run_guest_root_script(
    cfg: AgentVMConfig,
    ip: str,
    *,
    script: str,
    summary: str,
    detail: str,
    dry_run: bool,
) -> None:
    ident = require_ssh_identity(cfg.paths.ssh_identity_file)
    cmd = [
        'ssh',
        *ssh_base_args(
            ident,
            strict_host_key_checking='accept-new',
            connect_timeout=5,
            batch_mode=True,
        ),
        f'{cfg.vm.user}@{ip}',
        script,
    ]
    if dry_run:
        print(f'DRYRUN: would run guest reconcile command: {" ".join(shlex.quote(c) for c in cmd)}')
        return
    CommandManager.current().run(
        cmd,
        sudo=False,
        check=True,
        capture=True,
        summary=summary,
        detail=detail,
    )


def _sync_declared_attachment_manifest_on_host(
    cfg: AgentVMConfig,
    cfg_path: Path,
    *,
    dry_run: bool,
) -> Path:
    manifest_path = _declared_host_manifest_path(cfg)
    manifest_text = _declared_attachment_manifest_text(cfg, cfg_path)
    if dry_run:
        print(f'DRYRUN: would write declared attachment manifest to {manifest_path}')
        return manifest_path
    mgr = CommandManager.current()
    meta_dir = _declared_host_meta_dir(cfg)
    manifest_q = shlex.quote(str(manifest_path))
    payload = shlex.quote(manifest_text)
    with mgr.step(
        'Sync declared attachment manifest',
        why='Update the host-side declared attachment manifest that the guest boot-time replay helper consumes.',
        approval_scope=f'declared-manifest:{cfg.vm.name}',
    ):
        mgr.submit(
            ['mkdir', '-p', str(meta_dir)],
            sudo=True,
            role='modify',
            summary='Create declared attachment metadata directory',
            detail=f'target={meta_dir}',
        )
        mgr.submit(
            ['bash', '-c', f'printf %s {payload} > {manifest_q}'],
            sudo=True,
            role='modify',
            summary='Write declared attachment manifest',
            detail=f'target={manifest_path}',
        )
    return manifest_path


def _install_declared_attachment_replay(
    cfg: AgentVMConfig,
    ip: str,
    *,
    dry_run: bool,
) -> None:
    replay_py = _declared_replay_python()
    service_text = _declared_replay_service_unit()
    script = textwrap.dedent(
        f"""\
        set -euo pipefail
        sudo -n mkdir -p {shlex.quote(str(Path(DECLARED_ATTACHMENT_REPLAY_BIN).parent))}
        sudo -n mkdir -p /etc/systemd/system
        tmp_py="$(mktemp)"
        tmp_service="$(mktemp)"
        cat > "$tmp_py" <<'PYEOF'
        {replay_py}
        PYEOF
        cat > "$tmp_service" <<'SVCEOF'
        {service_text}
        SVCEOF
        sudo -n install -m 0755 "$tmp_py" {shlex.quote(DECLARED_ATTACHMENT_REPLAY_BIN)}
        sudo -n install -m 0644 "$tmp_service" /etc/systemd/system/{DECLARED_ATTACHMENT_REPLAY_SERVICE}
        rm -f "$tmp_py" "$tmp_service"
        sudo -n systemctl daemon-reload
        sudo -n systemctl enable {DECLARED_ATTACHMENT_REPLAY_SERVICE}
        """
    )
    _run_guest_root_script(
        cfg,
        ip,
        script=script,
        summary='Install declared attachment replay helper',
        detail='Install or refresh the guest systemd replay helper used for boot-time declared attachment restore.',
        dry_run=dry_run,
    )


def _reconcile_declared_attachments_in_guest(
    cfg: AgentVMConfig,
    cfg_path: Path,
    ip: str,
    *,
    dry_run: bool,
) -> None:
    _sync_declared_attachment_manifest_on_host(cfg, cfg_path, dry_run=dry_run)
    _install_declared_attachment_replay(cfg, ip, dry_run=dry_run)
    _run_guest_root_script(
        cfg,
        ip,
        script=f'sudo -n {shlex.quote(DECLARED_ATTACHMENT_REPLAY_BIN)}',
        summary='Replay declared attachment mounts inside guest',
        detail='Verify and repair guest-visible declared attachment bind mounts from the persisted manifest.',
        dry_run=dry_run,
    )

