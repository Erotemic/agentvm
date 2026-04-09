"""Shared persistent-attachment replay constants and templates.

This module is intentionally dependency-light so VM bootstrap code can import
it without pulling in the higher-level attachments package.
"""

from __future__ import annotations

import textwrap
from pathlib import PurePosixPath

PERSISTENT_ATTACHMENT_HOST_META_DIR = '.aivm'
PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME = 'persistent-attachments.json'
PERSISTENT_ATTACHMENT_GUEST_STATE_DIR = '/var/lib/aivm'
PERSISTENT_ATTACHMENT_GUEST_STATE_PATH = (
    f'{PERSISTENT_ATTACHMENT_GUEST_STATE_DIR}/attachments.json'
)
PERSISTENT_ATTACHMENT_REPLAY_BIN = '/usr/local/libexec/aivm-persistent-attachment-replay'
PERSISTENT_ATTACHMENT_REPLAY_SERVICE = 'aivm-persistent-attachment-replay.service'
PERSISTENT_ROOT_VIRTIOFS_TAG = 'aivm-persistent-root'
PERSISTENT_ROOT_GUEST_MOUNT_ROOT = '/mnt/aivm-persistent'

def persistent_replay_python() -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import subprocess
        import sys
        import tempfile
        from pathlib import Path, PurePosixPath

        PERSISTENT_ROOT_TAG = "{PERSISTENT_ROOT_VIRTIOFS_TAG}"
        PERSISTENT_ROOT_MOUNT = "{PERSISTENT_ROOT_GUEST_MOUNT_ROOT}"
        HOST_MANIFEST = str(PurePosixPath(PERSISTENT_ROOT_MOUNT) / "{PERSISTENT_ATTACHMENT_HOST_META_DIR}" / "{PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME}")
        STATE_DIR = "{PERSISTENT_ATTACHMENT_GUEST_STATE_DIR}"
        STATE_PATH = "{PERSISTENT_ATTACHMENT_GUEST_STATE_PATH}"

        def run(cmd, check=True, capture=False):
            return subprocess.run(
                cmd,
                check=check,
                text=True,
                stdout=subprocess.PIPE if capture else None,
                stderr=subprocess.PIPE if capture else None,
            )

        def mount_persistent_root():
            os.makedirs(PERSISTENT_ROOT_MOUNT, exist_ok=True)
            probe = subprocess.run(["mountpoint", "-q", PERSISTENT_ROOT_MOUNT])
            if probe.returncode == 0:
                return
            run(["mount", "-t", "virtiofs", PERSISTENT_ROOT_TAG, PERSISTENT_ROOT_MOUNT])

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
            if not os.path.exists(HOST_MANIFEST):
                raise RuntimeError(
                    f"persistent attachment manifest missing from mounted host export: {{HOST_MANIFEST}}"
                )
            desired = load_json(HOST_MANIFEST)
            atomic_write_json(STATE_PATH, desired)
            return previous, desired

        def mount_source_for(record):
            token = str(record.get("shared_root_token") or "").strip()
            if not token:
                raise RuntimeError("persistent attachment record missing shared_root_token")
            return str(PurePosixPath(PERSISTENT_ROOT_MOUNT) / token)

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
                raise RuntimeError("persistent attachment record missing guest_dst")
            source = mount_source_for(record)
            if not os.path.isdir(source):
                raise RuntimeError(f"persistent attachment source missing in shared root: {{source}}")
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
            mount_persistent_root()
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


def persistent_replay_service_unit() -> str:
    return textwrap.dedent(
        f"""\
        [Unit]
        Description=aivm persistent attachment replay
        After=local-fs.target

        [Service]
        Type=oneshot
        ExecStart={PERSISTENT_ATTACHMENT_REPLAY_BIN}

        [Install]
        WantedBy=multi-user.target
        """
    )
