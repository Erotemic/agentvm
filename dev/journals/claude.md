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
