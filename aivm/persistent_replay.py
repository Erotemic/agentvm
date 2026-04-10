"""Shared persistent-attachment replay constants and templates.

This module is intentionally dependency-light so VM bootstrap code can import
it without pulling in the higher-level attachments package.
"""

from __future__ import annotations

import textwrap

PERSISTENT_ATTACHMENT_HOST_MANIFEST_NAME = 'persistent-attachments.json'
PERSISTENT_ATTACHMENT_GUEST_STATE_DIR = '/var/lib/aivm'
PERSISTENT_ATTACHMENT_GUEST_STATE_PATH = (
    f'{PERSISTENT_ATTACHMENT_GUEST_STATE_DIR}/attachments.json'
)
PERSISTENT_ATTACHMENT_REPLAY_BIN = (
    '/usr/local/libexec/aivm-persistent-attachment-replay'
)
PERSISTENT_ATTACHMENT_REPLAY_SERVICE = (
    'aivm-persistent-attachment-replay.service'
)
PERSISTENT_ROOT_VIRTIOFS_TAG = 'aivm-persistent-root'
PERSISTENT_ROOT_GUEST_MOUNT_ROOT = '/mnt/aivm-persistent'


def persistent_replay_python() -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env python3
        import json
        import os
        import posixpath
        import subprocess
        import sys
        from pathlib import PurePosixPath

        PERSISTENT_ROOT_TAG = "{PERSISTENT_ROOT_VIRTIOFS_TAG}"
        PERSISTENT_ROOT_MOUNT = "{PERSISTENT_ROOT_GUEST_MOUNT_ROOT}"
        # Guest replay is intentionally fed only from the VM-local manifest
        # that the host syncs in. The helper must never read host desired state
        # back through virtiofs.
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
                raise FileNotFoundError(
                    f"persistent attachment manifest missing from guest state dir: {{path}}"
                )

        def normalize_guest_dst(raw):
            text = str(raw or "").strip()
            if not text:
                return ""
            text = posixpath.normpath(text)
            if not text.startswith("/"):
                return ""
            return text

        def desired_option(record):
            return "ro" if str(record.get("access") or "").strip() == "ro" else "rw"

        def mount_source_for(record):
            token = str(record.get("shared_root_token") or "").strip()
            if not token:
                return ""
            return str(PurePosixPath(PERSISTENT_ROOT_MOUNT) / token)

        def parse_findmnt_pairs(stdout):
            values = {{}}
            for token in (stdout or "").split():
                if "=" not in token:
                    continue
                key, value = token.split("=", 1)
                values[key.strip().upper()] = value.strip().strip('"')
            return values

        def current_mount_info(target):
            result = subprocess.run(
                ["findmnt", "-P", "-n", "-o", "SOURCE,OPTIONS", "--target", target],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return None
            info = parse_findmnt_pairs(result.stdout)
            if not info:
                return None
            return {{
                "source": info.get("SOURCE", ""),
                "options": info.get("OPTIONS", ""),
            }}

        def unmount_guest_dst(guest_dst, *, ignore_busy=False):
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
            if ignore_busy and "busy" in message:
                print(
                    f"WARNING: skipping busy stale persistent attachment mount {{guest_dst}}: {{(result.stderr or result.stdout).strip()}}",
                    file=sys.stderr,
                )
                return
            raise RuntimeError(
                f"could not unmount {{guest_dst}}: {{(result.stderr or result.stdout).strip()}}"
            )

        def is_descendant(child, parent):
            child_path = PurePosixPath(child)
            parent_path = PurePosixPath(parent)
            return child_path != parent_path and child_path.is_relative_to(parent_path)

        def validate_records(records):
            # Normalize the desired record set before replay.
            #
            # Enabled parents are the only entries that may suppress nested
            # enabled children. Disabled entries are still tracked so we can
            # unmount them explicitly, but they never act as blockers for
            # descendant mounts.
            normalized = []
            for index, record in enumerate(records):
                if not isinstance(record, dict):
                    print(
                        f"WARNING: skipping malformed persistent attachment record at index {{index}}",
                        file=sys.stderr,
                    )
                    continue
                guest_dst = normalize_guest_dst(record.get("guest_dst"))
                if not guest_dst:
                    print(
                        f"WARNING: skipping persistent attachment record with missing guest_dst at index {{index}}",
                        file=sys.stderr,
                    )
                    continue
                token = str(record.get("shared_root_token") or "").strip()
                if not token:
                    print(
                        f"WARNING: skipping persistent attachment record with missing shared_root_token at index {{index}}",
                        file=sys.stderr,
                    )
                    continue
                enabled = bool(record.get("enabled", True))
                access = str(record.get("access") or "").strip() or "rw"
                normalized.append((guest_dst, index, enabled, access, record))

            normalized.sort(
                key=lambda item: (
                    len(PurePosixPath(item[0]).parts),
                    item[0],
                    item[1],
                )
            )
            accepted = []
            blockers = []
            seen_targets = {{}}
            for guest_dst, index, enabled, access, record in normalized:
                if guest_dst in seen_targets:
                    first_index = seen_targets[guest_dst]
                    print(
                        f"ERROR: duplicate persistent attachment guest_dst {{guest_dst}} at index {{index}} duplicates index {{first_index}}; skipping",
                        file=sys.stderr,
                    )
                    continue
                seen_targets[guest_dst] = index
                if enabled:
                    parent_hit = None
                    for accepted_guest_dst, accepted_access in blockers:
                        if is_descendant(guest_dst, accepted_guest_dst):
                            parent_hit = (accepted_guest_dst, accepted_access)
                    if parent_hit is not None:
                        parent_guest_dst, parent_access = parent_hit
                        if access != parent_access:
                            print(
                                f"ERROR: ignoring nested persistent attachment child {{guest_dst}} under {{parent_guest_dst}} because access differs (child={{access}} parent={{parent_access}})",
                                file=sys.stderr,
                            )
                        else:
                            print(
                                f"WARNING: ignoring nested persistent attachment child {{guest_dst}} under {{parent_guest_dst}}",
                                file=sys.stderr,
                            )
                        continue
                    blockers.append((guest_dst, access))
                accepted.append((guest_dst, enabled, record))
            return accepted

        def prune_stale_mounts(desired_targets):
            result = subprocess.run(
                ["findmnt", "-P", "-n", "-o", "TARGET,SOURCE"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            if result.returncode != 0:
                return
            root_prefix = PERSISTENT_ROOT_MOUNT.rstrip("/") + "/"
            for line in (result.stdout or "").splitlines():
                info = parse_findmnt_pairs(line)
                target = normalize_guest_dst(info.get("TARGET"))
                source = str(info.get("SOURCE") or "").strip()
                if not target or target == PERSISTENT_ROOT_MOUNT:
                    continue
                if not source:
                    continue
                if not (source == PERSISTENT_ROOT_MOUNT or source.startswith(root_prefix)):
                    continue
                if target in desired_targets:
                    continue
                unmount_guest_dst(target, ignore_busy=True)

        def ensure_record(record):
            guest_dst = normalize_guest_dst(record.get("guest_dst"))
            if not guest_dst:
                raise RuntimeError("persistent attachment record missing guest_dst")
            source = mount_source_for(record)
            if not source:
                print(
                    f"WARNING: skipping persistent attachment record with missing shared_root_token for guest_dst {{guest_dst}}",
                    file=sys.stderr,
                )
                return
            if not os.path.isdir(source):
                print(
                    f"WARNING: skipping persistent attachment record with missing source in shared root: {{source}}",
                    file=sys.stderr,
                )
                return
            current = current_mount_info(guest_dst)
            desired = desired_option(record)
            if current is not None:
                current_source = str(current.get("source") or "").strip()
                current_options = str(current.get("options") or "").strip()
                if current_source and current_source != source:
                    unmount_guest_dst(guest_dst, ignore_busy=True)
                    current = current_mount_info(guest_dst)
                    if current is not None:
                        current_source = str(current.get("source") or "").strip()
                        if current_source and current_source != source:
                            print(
                                f"WARNING: skipping persistent attachment replacement for busy mount {{guest_dst}} (current={{current_source}} desired={{source}})",
                                file=sys.stderr,
                            )
                            return
                        current_options = str(current.get("options") or "").strip()
                        if desired in current_options.split(","):
                            return
                elif desired in current_options.split(","):
                    return
            if current is None:
                os.makedirs(guest_dst, exist_ok=True)
                if subprocess.run(["mountpoint", "-q", guest_dst]).returncode != 0:
                    run(["mount", "--bind", source, guest_dst])
                current = current_mount_info(guest_dst)
            if current is None:
                raise RuntimeError(f"could not verify persistent attachment mount {{guest_dst}}")
            current_options = str(current.get("options") or "").strip()
            if desired not in current_options.split(","):
                run(["mount", "-o", f"remount,bind,{{desired}}", guest_dst])

        def sync_state():
            desired = load_json(STATE_PATH)
            records = validate_records(desired.get("records", []))
            desired_targets = {{
                guest_dst for guest_dst, _enabled, _record in records
            }}
            prune_stale_mounts(desired_targets)
            failures = []
            for guest_dst, enabled, record in records:
                if not enabled:
                    try:
                        unmount_guest_dst(guest_dst, ignore_busy=True)
                    except Exception as ex:  # pragma: no cover - guest runtime path
                        failures.append(str(ex))
                    continue
                try:
                    ensure_record(record)
                except Exception as ex:  # pragma: no cover - guest runtime path
                    failures.append(str(ex))
            return failures

        def main():
            mount_persistent_root()
            try:
                failures = sync_state()
            except FileNotFoundError as ex:
                print(str(ex), file=sys.stderr)
                raise SystemExit(1)
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
