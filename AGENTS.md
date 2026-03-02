# AGENTS.md

## Purpose
Guidance for contributors (human or AI agents) working in this repository.

## Project Context
- Package / CLI name: `aivm`
- Primary goal: manage local libvirt/KVM VMs for agent workflows.
- Config model:
  - User/global registry + per-VM config under app config dir via `ub.Path.appdir('aivm', type='config')`
  - Optional per-directory metadata: `.aivm-dir.toml`

## Core CLI UX Principles
- Prefer simple defaults:
  - `aivm code .`
  - `aivm ssh .`
- Ask before privileged operations unless `--yes` is provided.
- Status should be safe and informative by default; privileged checks are opt-in via `--sudo`.
- Discovery/import should be explicit and user-confirmed for unmanaged VMs.

## Safety Expectations
- Do not silently broaden VM host-path exposure.
- Avoid attaching the same host folder to multiple VMs unless user forces it.
- Surface clear diagnostics for share/network/firewall mismatches.
- Prefer live libvirt metadata when config may be stale (bridge/network details).

## Development Notes
- Keep changes focused and incremental.
- Preserve non-interactive usability (`--yes`, `--dry_run`).
- Keep docs updated when CLI behavior or command tree changes.

## Useful Local Commands
- Lint/type/test (as available in environment):
  - `python -m py_compile aivm/*.py`
  - `pytest -q`
  - `ty check aivm`
- CLI help and structure:
  - `aivm --help`
  - `aivm help tree`

## Packaging
- Source of truth is `pyproject.toml`.
- Runtime dependencies must be declared in `[project].dependencies`.

## Developer journal
Keep a running journal at `dev/journals/<agent_name>.md` (e.g.
`dev/journals/codex.md`) to capture the story of the work (decisions, progress,
challenges). This is not a changelog.  Write at a high level for future
maintainers: enough context for someone to pick up where you left off.

- Format: Each entry starts with `## YYYY-MM-DD HH:MM:SS -ZZZZ` (local time).
- Must include: what you were working on, a substantive entry about your state of mind / reflections, uncertainties/risks, tradeoffs, what might break, what you're confident about.
- May include: what happened, rationale, testing notes, next steps, open questions.
- Rules: Prefer append-only. You may edit only the most recent entry *during the same session* (use timestamp + context to judge); never modify the timestamp line; once a new session starts, create a new entry. Never modify older entries. Avoid large diffs; reference files/modules/issues instead.
