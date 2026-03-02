# Changelog
We [keep a changelog](https://keepachangelog.com/en/1.0.0/).
We aim to adhere to [semantic versioning](https://semver.org/spec/v2.0.0.html).

## [Version 0.2.0] - Unreleased

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