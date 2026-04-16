# Changelog
We [keep a changelog](https://keepachangelog.com/en/1.0.0/).
We aim to adhere to [semantic versioning](https://semver.org/spec/v2.0.0.html).

## Version 0.4.1 - Unreleased

### Changed
* Attaching directories now has mirrors that resolve to exact path matches on the guest and paths relative to root.
* Added an opt-in `persistent` attachment mode that uses its own `persistent-root` virtiofs export, persists desired guest-visible bind mounts as declarations, and replays them from a guest systemd helper instead of reconstructing every attachment on each `aivm code .` / `aivm ssh .` run.

### Fixed
* Attaching directories now uses consistent guest locations between different attach modes 
* Read-only access is now documented and wired through the new persistent attachment replay path.


## [Version 0.4.0] - Released 2026-03-27

### Added
- New `CommandManager` module (`aivm/commands.py`) centralizing all subprocess execution with intent-based approval workflows, command plans, and role annotations (read vs modify).
- VM configuration drift detection (`aivm/vm/drift.py`) with `DriftReport` and `DriftItem` dataclasses covering hardware (CPU/RAM) and share-mapping mismatches.
- Status command now reports whether the current working directory is shared with a VM and flags any detected VM configuration drift.
- Formal attachment model with `AttachmentMode` (shared, shared-root, git), `AttachmentAccess` (rw, ro), and `ResolvedAttachment` dataclasses in `aivm/vm/share.py`.
- Read-only share access mode for folder attachments.
- Directory share status display in `aivm status` output.
- Grouped command approval: related commands are batched into plans with unified previews before execution.
- SSH bootstrap prompting improvements.

### Changed
- All subprocess calls across lifecycle, firewall, host, and network modules now route through `CommandManager` instead of the previous ad-hoc `run_cmd` utility.
- Operations declare explicit intent contexts describing *why* they are happening, improving logs and approval prompts.
- Status and drift probes return tri-state outcomes (True/False/None) to gracefully handle permission or query errors.
- Firewall and network setup operations now show clear intent in command previews.
- Better error classification for apt lock conflicts, missing UEFI firmware, and memory allocation failures.
- Python target version updated from 3.8 to 3.11 in ruff configuration.
- Added mypy configuration section in `pyproject.toml`.
- Bumped project version metadata to `0.4.0`.

### Removed
- Removed legacy `run_cmd`, `CmdResult`, and sudo intent arming from `aivm/util.py`; all command execution now lives in `CommandManager`.

### Fixed
- Fixed mounting issues and improved virtiofs tag alignment across lifecycle operations.
- Fixed auto-approval logic for read-only sudo commands.
- Various type annotation improvements and test fixes.

## [Version 0.3.0] - Released 2026-03-27

### Added
- Added a Git-backed attachment mode that keeps a guest-local repo instead of creating a writable virtiofs share.
- Host repos can now register a Git remote that targets the guest working repo over the managed VM SSH alias.

### Changed
- Folder-oriented `attach` / `code` / `ssh` flows now persist and honor per-attachment mode (`shared` or `git`).
- Bumped project version metadata to `0.3.0`.

### Notes
- Git-backed attachments currently seed committed repository state only; uncommitted host worktree changes are not copied into the guest clone.
- Git-backed attachments currently sync committed branch state; uncommitted host worktree changes are not pushed into the guest repo.

## [Version 0.2.0] - Released 2026-03-27

### Added
- New CLI package and commands: `aivm.cli` with subcommands for `config`, `firewall`, `help`, `host`, `main`, `net`, and `vm`.
- New runtime and state modules: `aivm/runtime.py`, `aivm/status.py`, `aivm/results.py`, and `aivm/store.py`.
- New `aivm/errors.py` and `aivm/resource_checks.py` helpers.
- New `aivm/vm` package with `lifecycle`, `share` and `sync` modules.
- Added many tests covering CLI, config, detect, firewall, host, net, resource checks, status/runtime, store, util and VM helpers.
- Added `AGENTS.md` developer guidance and several developer journal entries.

### Changed
- Major refactor of CLI into a package; removed single-file `aivm/cli.py` in favor of modular commands.
- Refactored submodules and internal structure to improve testability and separation of concerns.
- Consolidated configuration: moved from per-VM config files to a single user-level configuration (reducing per-VM files written under `configs`).
- Improved global configuration handling and color/logging configuration (`loguru` defaults and color config).
- Documentation updates: updated `README.rst` and docs configuration.
- Bumped project version and updated `pyproject.toml` for the 0.2.0 release candidate.

### Fixed
- Fixes to VM creation/management flows and related tests.
- SSH permission and prompt fixes.
- Fixed issues with outdated MAC address handling.
- Various test and lint fixes (formatting, import fixes) to stabilize the test suite.

### Removed
- Removed legacy `aivm/registry.py` and the old `aivm/vm.py` single-file VM implementation.
- Removed `README.md` in favor of the reworked `README.rst`.

### Notes
- Added ability to open firewall ports via CLI.
- UX and provisioning improvements (avoid sudo where possible, better prompts).


## [Version 0.1.0] - 2026-02-25

### Added
- Project scaffold and initial CLI: single-file `aivm/cli.py` providing basic VM lifecycle and host utilities.
- Core modules included: `aivm/config.py`, `aivm/detect.py`, `aivm/firewall.py`, `aivm/host.py`, `aivm/net.py`, `aivm/vm.py`, `aivm/util.py`, and `aivm/registry.py`.
- Packaging and docs: `pyproject.toml`, `requirements.txt`, `README.md` and `README.rst`.
- Initial tests and CI helpers: minimal tests (dry-run/import) under `tests/` and basic CI/workflow files.
- Minimal, experimental release focused on local libvirt/KVM Ubuntu 24.04 VM management; intended as a starting scaffold for subsequent refactors.
