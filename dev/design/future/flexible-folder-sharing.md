# Flexible Folder Sharing: Future Design Notes

Status: exploratory / not implemented.

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
- `git` mode:
  - avoids virtiofs device pressure.
  - syncs committed Git state, not full live filesystem semantics.

## Future goal

Add one or more attachment backends that scale to more folders without
consuming per-folder virtiofs device slots, while keeping explicit trust and
safety boundaries.

## Candidate backend directions

1. `sshfs` (or SFTP mount) from guest to host
- Pros: no per-folder VM device hotplug; familiar mount model.
- Cons: performance may be lower; requires robust host auth surface hardening.

2. `rsync`/`unison` style sync backend
- Pros: scalable attachment count; explicit sync boundaries.
- Cons: not live bidirectional POSIX semantics; conflict handling UX needed.

3. Network file server per VM (NFS/9p-like over isolated VM network)
- Pros: single shared transport can serve many folders.
- Cons: more host service complexity and firewall/trust policy requirements.

4. Multiplexed single-share workspace model
- Pros: one virtiofs mapping can expose many subfolders under a managed root.
- Cons: expands trust to larger host subtree unless carefully sandboxed.

## Design requirements (must-have)

- Preserve explicit consent before trust expansion.
- Keep non-interactive behavior deterministic (`--yes`, `--dry_run`).
- Provide clear diagnostics for backend mismatch/capacity failures.
- Avoid silent fallback between attachment backends.
- Keep restore behavior predictable across reboot and VM recreate.

## Open questions

- Should backend be selected globally, per VM, or per attachment?
- How to migrate existing `shared` attachments when capacity is exhausted?
- What minimum performance bar is acceptable for code+editor workflows?
- How to represent backend-specific health in `aivm status`?

