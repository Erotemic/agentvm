# Flexible Folder Sharing: Future Design Notes

Status: **Partially implemented (v0.4.0)** — bind-mount single-export strategy is the default `shared-root` attachment mode.

## Why this exists

Current `shared` attachments use virtiofs device mappings. Each mapping is a VM
device attach operation, which is constrained by guest/device topology limits
(for example PCI/PCIe slot/function availability). In practice, this means a VM
can fail to attach additional shared folders with errors like:

- `internal error: No more available PCI slots`

This is a major scaling limitation for workflows that need many host folders.

## Current behavior summary

- `shared` mode:
  - low-latency host/guest view of the same files.
  - consumes virtiofs device capacity per attached folder.
- `shared-root` mode (v0.4.0+):
  - single virtiofs export with host-side bind mounts for each attachment.
  - scales to many folders without per-folder virtiofs device slots.
- `git` mode:
  - avoids virtiofs device pressure.
  - syncs committed Git state, not full live filesystem semantics.

## Future goal

Add one or more attachment backends that scale to more folders without
consuming per-folder virtiofs device slots, while keeping explicit trust and
safety boundaries.

## Candidate backend directions

### 1. `sshfs` (or SFTP mount) from guest to host

- **Status**: Not implemented
- **Pros**: no per-folder VM device hotplug; familiar mount model.
- **Cons**: performance may be lower; requires robust host auth surface hardening.

### 2. `rsync`/`unison` style sync backend

- **Status**: Not implemented
- **Pros**: scalable attachment count; explicit sync boundaries.
- **Cons**: not live bidirectional POSIX semantics; conflict handling UX needed.

### 3. Network file server per VM (NFS/9p-like over isolated VM network)

- **Status**: Not implemented
- **Pros**: single shared transport can serve many folders.
- **Cons**: more host service complexity and firewall/trust policy requirements.

### 4. Multiplexed single-share workspace model

- **Status**: Implemented as `shared-root` attachment mode (v0.4.0+)
- **Pros**: one virtiofs mapping can expose many subfolders under a managed root.
- **Cons**: expands trust to larger host subtree unless carefully sandboxed.

## Bind-mount based single-export strategy (implemented in v0.4.0)

This is a practical way to keep one virtiofs device while still exposing
arbitrary host folders.

### High-level idea

- Create one per-VM host export root:
  - Path: `/var/lib/libvirt/aivm/<vm>/shared-root`
  - Implementation: [`_shared_root_host_dir()`](aivm/cli/vm.py:1718)
- Keep one persistent virtiofs mapping:
  - Host: `/var/lib/libvirt/aivm/<vm>/shared-root`
  - Guest mount: `/mnt/aivm-shared`
  - Virtiofs tag: `aivm-shared-root`
  - Constants: [`SHARED_ROOT_VIRTIOFS_TAG`](aivm/cli/vm.py:95), [`SHARED_ROOT_GUEST_MOUNT_ROOT`](aivm/cli/vm.py:96)
- For each attached folder, create a host-side bind mount under that root:
  - Host source: `/home/user/projectA`
  - Bind target: `/var/lib/libvirt/aivm/<vm>/shared-root/<token>`
  - Implementation: [`_ensure_shared_root_host_bind()`](aivm/cli/vm.py:1822)
- In guest, map user-facing path to the shared token path:
  - Guest destination: `/workspace/projectA` -> `/mnt/aivm-shared/<token>`
  - Implementation: [`_ensure_shared_root_guest_bind()`](aivm/cli/vm.py:1979)

### Why bind mounts (instead of host symlinks)

- Host symlinks to paths outside the exported root are not generally resolvable
  by guest through the single virtiofs mount.
- Bind mounts materialize each source directory *inside* the exported tree, so
  guest can access it through the one virtiofs mapping.

### Attach flow (rough)

1. Ensure shared-root exists and persistent virtiofs mapping is present.
   - Implementation: [`_ensure_shared_root_vm_mapping()`](aivm/cli/vm.py:1935)
2. Allocate stable token for attachment in config/store.
3. Host: `mount --bind <source> <shared-root>/<token>` (sudo).
   - Implementation: [`_ensure_shared_root_host_bind()`](aivm/cli/vm.py:1822)
4. Guest: ensure destination path points to `/mnt/aivm-shared/<token>`.
   - Implementation: [`_ensure_shared_root_guest_bind()`](aivm/cli/vm.py:1979)
5. Persist attachment metadata (source, token, guest destination, backend).

### Detach flow (rough)

1. Remove guest-side destination mapping.
   - Implementation: [`_detach_shared_root_guest_bind()`](aivm/cli/vm.py:2188)
2. Host: `umount <shared-root>/<token>` (sudo).
   - Implementation: [`_detach_shared_root_host_bind()`](aivm/cli/vm.py:2152)
3. Remove empty mountpoint dir.
4. Remove/store-update attachment record.

### Reboot recovery

- Mount lifecycle recovery after reboot is handled by:
  - [`_restore_saved_vm_attachments()`](aivm/cli/vm.py:2632) — restores state after VM restart
  - [`_ensure_attachment_available_in_guest()`](aivm/cli/vm.py:2214) — ensures attachments are available for running VMs

### Operational concerns

All operational concerns from the design are addressed:

- **Mount lifecycle recovery after reboot**: Handled by [`_restore_saved_vm_attachments()`](aivm/cli/vm.py:2632)
- **Conflict handling for source/destination**: Implemented in [`_ensure_shared_root_host_bind()`](aivm/cli/vm.py:1822) with `allow_disruptive_rebind` control
- **Cleanup robustness**: Stale mountpoint detection and repair in host bind logic
- **Security boundaries maintained**: Explicit trust boundary remains "all bind-mounted sources"
- **Sudo policy and diagnostics**: [`_confirm_sudo_block()`](aivm/cli/vm.py) required for mutating operations

## Design requirements (must-have)

All design requirements are satisfied in v0.4.0:

- ✅ **Explicit consent before trust expansion**: [`PlanScope`](aivm/cli/vm.py) and [`IntentScope`](aivm/cli/vm.py) provide approval scopes for shared-root operations
- ✅ **Non-interactive behavior (`--yes`, `--dry_run`)**: All functions accept `dry_run` and `yes` parameters
- ✅ **Clear diagnostics for backend mismatch/capacity failures**: Error messages include source, destination, and mount state details
- ✅ **Avoid silent fallback between attachment backends**: Each mode (`shared`, `shared-root`, `git`) is explicit; no automatic switching
- ✅ **Predictable restore behavior across reboot and VM recreate**: [`_restore_saved_vm_attachments()`](aivm/cli/vm.py:2632) handles recovery with safe defaults

## Not Yet Implemented

The following backends and features remain unimplemented as of v0.4.0:

| Feature | Backend | Status | Notes |
|------|------|------|-------|
| `sshfs` mount | SSH/SFTP | Not implemented | Requires guest SSH server; lower latency than rsync but needs auth hardening |
| `rsync` sync | rsync/unison | Not implemented | One-way sync; no live bidirectional POSIX semantics |
| `unison` sync | unison | Not implemented | Bidirectional sync with conflict resolution; not live filesystem |
| NFS server | NFSv4 | Not implemented | Requires host NFS service; firewall/trust policy complexity |
| 9p server | 9p/virtio-9p | Not implemented | Alternative to virtiofs; requires guest kernel support |
| Multiplexed single-share workspace | — | Partially implemented | Implemented as `shared-root`; other backends would provide alternatives |

### Implementation gaps

1. **No fallback mechanism**: If virtiofs device capacity is exhausted, there is no automatic fallback to an alternative backend.
2. **No backend selection UI**: Users cannot choose between backends; `shared-root` is the only multiplexed option.
3. **No performance metrics**: No way to query or compare performance characteristics of different backends.
4. **No migration path**: No tooling to migrate existing `shared` attachments to `shared-root` or other backends.

## Open questions

- Should backend be selected globally, per VM, or per attachment?
- How to migrate existing `shared` attachments when capacity is exhausted?
- What minimum performance bar is acceptable for code+editor workflows?
- How to represent backend-specific health in `aivm status`?
- Should there be a backend selection CLI flag (e.g., `--backend=sshfs`)?

## Implementation references

### Core functions

| Function | Purpose | Line |
|------|----|------|
| [`_shared_root_host_dir()`](aivm/cli/vm.py:1718) | Compute host export root path | 1718 |
| [`_shared_root_host_target()`](aivm/cli/vm.py:1722) | Compute bind target for attachment | 1722 |
| [`_ensure_shared_root_parent_dir()`](aivm/cli/vm.py:1775) | Create shared-root parent directory | 1775 |
| [`_ensure_shared_root_host_bind()`](aivm/cli/vm.py:1822) | Create/repair host-side bind mount | 1822 |
| [`_ensure_shared_root_vm_mapping()`](aivm/cli/vm.py:1935) | Ensure virtiofs device mapping | 1935 |
| [`_ensure_shared_root_guest_bind()`](aivm/cli/vm.py:1979) | Mount and bind inside guest | 1979 |
| [`_detach_shared_root_host_bind()`](aivm/cli/vm.py:2152) | Unmount host bind target | 2152 |
| [`_detach_shared_root_guest_bind()`](aivm/cli/vm.py:2188) | Unmount guest destination | 2188 |
| [`_ensure_attachment_available_in_guest()`](aivm/cli/vm.py:2214) | Orchestrate attachment for running VM | 2214 |
| [`_restore_saved_vm_attachments()`](aivm/cli/vm.py:2632) | Restore attachments after reboot | 2632 |

### Constants

| Constant | Value | Line |
|------|------|------|
| `SHARED_ROOT_VIRTIOFS_TAG` | `aivm-shared-root` | 95 |
| `SHARED_ROOT_GUEST_MOUNT_ROOT` | `/mnt/aivm-shared` | 96 |

### Supporting modules

- [`aivm/vm/share.py`](aivm/vm/share.py) — virtiofs attach/detach operations
- [`aivm/vm/drift.py`](aivm/vm/drift.py) — drift detection for shared-root attachments
- [`aivm/vm/lifecycle.py`](aivm/vm/lifecycle.py) — VM lifecycle with shared-root support
