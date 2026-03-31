## 2026-03-31 17:43:06 +0000

Session focused on attachment path policy unification and the new mirror-symlink feature (spec was provided as a detailed prompt).

### What was done

Four files changed: `aivm/config.py`, `aivm/store.py`, `aivm/vm/share.py`, `aivm/cli/vm.py`, plus test updates in `tests/test_cli_vm_attach.py`.

**Tag generation** (`aivm/vm/share.py`): `_auto_share_tag_for_path` now always includes a stable 8-hex-char hash of the resolved path in every generated tag (`hostcode-<name>-<hash>`). Previously the hash was only added on collision. This is a quiet but important correctness fix — two directories with the same basename used to silently get the same tag until one was detected as a conflict at attach time.

**Unified default guest destination** (`aivm/cli/vm.py`): Removed the git-mode special case that defaulted the guest destination to `/home/<user>/...` relative. All modes now default to the lexical absolute host path (`expanduser().absolute()`). The old auto-migration logic (which tried to retroactively rewrite saved `guest_dst` values that matched the host path to the guest-home-relative form) was also removed. Existing saved records are preserved as-is. This is a breaking behavioral change for any new git attachments, but the right call: users who attach `/home/joncrall/code/repo` should find it at `/home/joncrall/code/repo` in the guest, not at a rewritten path under `/home/agent/`.

**Host symlink handling**: Added `_default_primary_guest_dst` and `_host_symlink_lexical_path` helpers. If the host source is itself a symlink, the primary guest destination becomes the resolved real path, and a companion symlink is created on the guest at the lexical path. The safety rules for companion symlinks (`_ensure_guest_symlink`) cover: no-op if already correct, replace empty dir, warn-and-skip for non-empty dir / regular file / wrong-target symlink.

**Mirror-home** (`behavior.mirror_shared_home_folders`): New `BehaviorConfig` flag (default false). When enabled and the host path is under the host home and the guest home differs, `_ensure_attachment_available_in_guest` creates a symlink under the guest home mirroring the relative path. The flag is threaded from the store into `VMAttachCLI.main` and `_prepare_attached_session`.

**Git exact-path support** (`_ensure_guest_git_repo`): Updated the shell script to use `sudo -n mkdir -p <parent>` with a fallback `sudo -n chown` on the leaf when `mkdir -p <root>` fails unprivileged. This allows git-mode to work when the destination is outside the guest home (e.g. `/home/joncrall/code/repo` on the guest).

### Tradeoffs and risks

The biggest behavioral change is the git-mode default destination. Any new git attachment that previously would have gone to `/home/agent/code/repo` will now go to `/home/joncrall/code/repo` (matching the host). This is intentional but could surprise users who relied on the old behavior for a path that required a writable guest home. The spec explicitly called for this change and the old auto-migration was already fragile.

The `VMAttachCLI.main` change from `Path(...).resolve()` to `Path(...).expanduser().absolute()` for `host_src` is necessary for symlink detection to work, but means any downstream code that assumed `host_src` was always fully resolved may see a non-resolved path. Audited all uses in that function — they all pass through `_resolve_attachment` which calls `host_src.resolve()` internally for `source_dir`, so this is safe.

The `_ensure_guest_symlink` helper uses `sudo -n mkdir -p` for the symlink parent on the guest. This is fine for typical aivm setups where the guest user has passwordless sudo, but could silently fail or warn if sudo isn't configured. The function logs a warning on unexpected exit codes but doesn't raise.

### Confidence

High confidence on the core path-unification and tag changes — they're simple and well-tested. Medium confidence on the symlink companion and mirror-home paths since they involve SSH guest-side shell scripts that are harder to integration-test without a live VM. The unit tests cover the logic paths but not actual SSH execution.

### Tests

Two tests were updated to reflect the new behavior (git default path, no migration). 21 new tests were added covering: default guest dst helpers, tag hash properties, guest symlink safety rules, mirror-home path computation, and the mirror integration through `_ensure_attachment_available_in_guest`. Full suite: 242 passed, 3 skipped.

---

## 2026-03-28 00:15:00 +0000

Session focused on three areas: changelog maintenance, removing dead backward-compatibility code, and improving log attribution in `CommandManager`.

### Changelog

Added a v0.4.0 entry to `CHANGELOG.md` covering the major changes since v0.3.0: the `CommandManager` module, drift detection (`aivm/vm/drift.py`), status enhancements, the formal attachment model (`AttachmentMode`/`AttachmentAccess`/`ResolvedAttachment`), and the removal of legacy `run_cmd`/`CmdResult`/sudo-intent from `aivm/util.py`. User subsequently released v0.3.0 and v0.4.0, and created a v0.4.1 unreleased section.

### Backward-compatibility alias cleanup

Removed the only backward-compat alias in the codebase: `saved_attachment_drift_report = saved_vm_drift_report` in `aivm/vm/drift.py`. It had zero callers. Since the project has no public Python API (CLI only), these aliases serve no purpose.

### CommandManager log attribution refactor

The original problem: log lines from `CommandManager` methods like `_render_plan_preview` showed `aivm.commands:_render_plan_preview:1028` as the source frame, which is unhelpful to operators. The real question is "which caller triggered this plan?"

**First attempt (reverted):** Added `_caller_log(submitted_by)` which parsed the `capture_submitter()` provenance string (`module:function:lineno`) and used `log.patch()` to override loguru's frame fields. User rejected this as fragile — it relied on string parsing and on the plan/spec capturing a submitter at submit-time for replay at log-time. The `capture_submitter()` method itself was doing frame-walking via `inspect.currentframe()` to find the first non-internal caller.

**Final approach:** Replaced the entire `capture_submitter`/`submitted_by` mechanism with `_stacklevel` parameter threading. Each internal method accepts `_stacklevel` and increments it by 1 when calling deeper methods, so `log.opt(depth=_stacklevel)` naturally points at the real caller frame. Entry points seed the initial value: `PlanScope.__exit__` passes `_stacklevel=2`, `run()` passes `_stacklevel=2` into `submit()`, etc. Removed `capture_submitter()`, `_caller_log()`, and the `submitted_by` field from both `CommandSpec` and `CommandPlan`.

This is mechanically straightforward and the depth count is always exact — no string parsing, no frame-walking at submit time, and the provenance is derived from the actual call stack at log time rather than reconstructed.

**Removed fields/methods:**
- `CommandSpec.submitted_by`
- `CommandPlan.submitted_by`
- `CommandManager.capture_submitter()`
- `CommandManager._caller_log()`
- `import inspect` (no longer needed)

**Methods that gained `_stacklevel`:** `submit`, `run`, `flush`, `flush_through`, `finish_plan`, `_approve_plan_if_needed`, `_render_plan_preview`, `_render_plan_full_commands`, `_flush_plan`, `_flush_loose_commands`, `_execute_one`, `_confirm_loose_sudo_command`, and `CommandHandle.result`.

### Docs update

Updated `docs/source/design.rst` and `docs/source/workflows.rst` to remove stale references to the `run_cmd` compatibility shim and "migration continues" language. The migration to `CommandManager` is complete — no shim exists.

Tradeoffs and what might break: the `_stacklevel` approach is precise but requires discipline — if someone adds an intermediate call in the chain without threading `_stacklevel + 1`, the frame attribution will be off by one. This is a reasonable tradeoff: the failure mode is merely a wrong log frame (cosmetic), not incorrect behavior, and it's the standard pattern used by Python's own `warnings.warn(stacklevel=)`.

Uncertainties: the `_stacklevel` defaults assume the public methods (`submit`, `run`, `flush`, `flush_through`) are called directly from user code. If they're called from other internal helpers in the future, those helpers would need to thread `_stacklevel` too. The default of 1 means "direct caller is user code" which is correct for all current call sites.
