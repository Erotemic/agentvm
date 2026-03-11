Design Contract
===============

Purpose
-------

This document defines the long-lived engineering contract for ``aivm``.
It is the reference for product intent, safety boundaries, reliability
expectations, and coding conventions.

The journal captures implementation history. This document captures stable
intent.


Product Intent
--------------

``aivm`` is a local libvirt/KVM VM manager optimized for agent workflows.

Primary user outcomes:

* run common workflows with minimal commands (for example ``aivm code .`` and
  ``aivm ssh .``)
* keep usage low-friction for non-VM experts
* make day-to-day development feel close to host-native workflows by default
  (attach needed folders, then run ``code`` / ``ssh`` from the working folder)
* keep a clear host/guest boundary with explicit trust expansion points
* support both interactive use and automation-friendly non-interactive flows


System Model
------------

Single source of truth
~~~~~~~~~~~~~~~~~~~~~~

* Use the config store as the canonical declared state for managed VMs,
  networks, and attachments.
* Treat runtime state (libvirt, network, firewall, guest reachability) as
  observed state that may drift from declared state.

Reconciliation model
~~~~~~~~~~~~~~~~~~~~

* Commands should reconcile toward declared intent when safe.
* Re-running the same operation should converge (idempotent behavior), not
  compound side effects.
* Runtime-sensitive operations should prefer live inspection over stale
  assumptions.


Safety and Trust Boundaries
---------------------------

1. Explicit consent for privileged host changes

   Privileged operations must be visible and confirmable unless the user has
   explicitly opted into automatic approval (``--yes`` / ``--yes-sudo``).

2. No silent trust broadening

   Actions that broaden VM access to host resources (filesystem sharing,
   firewall relaxations, network exposure, external file edits) must be
   explicit and diagnosable.

3. Verified image sources

   Base image usage must remain integrity-checked. Local caches and mirrors are
   acceptable only when they preserve verification guarantees.

4. Fail with actionable diagnostics

   Errors should explain what failed, why, and what the operator can do next.
   Favor clear failure over ambiguous partial success in safety-critical paths.

5. Shared-folder trust is a mode, not an assumption

   Read/write host-folder sharing is the practical default today, but
   isolation-oriented modes should remain first-class. Git-backed attachment is
   now one supported alternative, and future modes (for example read-only
   attachment) should continue to support secret-sensitive host repos and
   cleaner guest environments.


Reliability Principles
----------------------

Idempotency
~~~~~~~~~~~

* Lifecycle operations (network create/destroy, VM create/start/destroy,
  attachment reconcile) should tolerate retries and partially completed prior
  runs.

Atomic operations
~~~~~~~~~~~~~~~~~

* Multi-step file operations (downloads, generated artifacts, state writes)
  should use atomic patterns whenever practical:

  * write to temporary target
  * validate integrity/shape
  * replace into final location atomically when possible
  * clean up invalid intermediates

Integrity-verified and content-addressable data access
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* All network/downloaded artifacts must be hash-verified before they are
  trusted.
* Favor content-addressable lookup paths (by digest) as a fallback for data
  access, so mutable names/URLs are not the only resolution path.
* Cached artifacts should remain re-verifiable, not implicitly trusted forever
  by pathname alone.

Preflight and readiness checks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Validate hard prerequisites as early as practical.
* Ensure readiness checks match the actual execution context (including
  privilege context).

Observability
~~~~~~~~~~~~~

* Long-running and mutating operations should expose progress and command intent
  clearly.
* When writing a file to the host system, emit a note describing the write.
  If reconciliation determines there is nothing to write, skip both the write
  and the normal note; an optional debug-level message may explain the no-op.
* Normal output should be concise; deeper diagnostics should be available with
  verbosity/detail flags.


CLI and Code Architecture
-------------------------

CLI framework conventions
~~~~~~~~~~~~~~~~~~~~~~~~~

* Use ``scriptconfig`` for command structure and argument definitions.
* Keep command modules thin: parse/dispatch/orchestrate in CLI modules, place
  operational logic in domain modules.
* Prefer explicit command return codes and stable CLI behavior for scripting.

Operational command execution
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Route external command execution through shared helpers for consistent sudo
  policy, logging, and error semantics.
* Keep privilege handling explicit and auditable.
* Preserve ``--dry_run`` as a true non-destructive preview path.

State management
~~~~~~~~~~~~~~~~

* Keep state transitions explicit.
* Avoid hidden coupling between config mutation and runtime mutation.
* When behavior depends on inferred state, provide diagnostics that make the
  inference visible.

Provisioning scope
~~~~~~~~~~~~~~~~~~

* Provide basic provisioning primitives and sensible defaults.
* Keep provisioning policy user-directed rather than overly prescriptive.
* Prefer extensibility over hardcoding opinionated full-environment setup.


Testing Contract
----------------

* Unit tests should cover success paths and failure messaging for new behavior.
* Integration/E2E flows should include explicit prerequisite checks and clear
  failure reasons.
* Cleanup paths should run in ``finally`` blocks for lifecycle tests.
* Tests should protect idempotency and atomicity assumptions when those are part
  of command guarantees.


Design Change Process
---------------------

When a change modifies behavior at a design level (safety boundary, trust model,
state model, CLI contract, compatibility policy), update this document in the
same change.

Keep updates concise and structured using this template:

.. code-block:: text

   Decision Title:
   Context:
   Decision:
   Consequences:
   Follow-ups:

Guidelines:

* This document should remain evergreen and principle-focused.
* Journal entries may reference time-specific incidents; this document should
  not rely on those references.
* If implementation and this contract diverge, either align implementation or
  update this contract explicitly.

Implementation TODO Notes
-------------------------

To fully realize the integrity/content-addressable principle, current code
should be evolved in these areas:

* ``aivm/vm/lifecycle.py``:
  verify pre-existing cached base images before reuse (not only newly
  downloaded images), and add digest-keyed cache lookup fallback before URL
  fetch.
* ``aivm/config.py``:
  move image cache identity toward digest-first semantics (``cache_name`` is
  currently name-oriented).
* ``aivm/status.py``:
  status reporting should eventually reflect both named-path cache and any
  content-addressable fallback resolution.
* E2E/shared cache helpers in ``tests/test_e2e_nested.py``:
  keep local cache path/version conventions aligned with digest-addressable
  behavior once implemented in runtime code.
* Provisioning defaults:
  add ``uv`` to baseline provisioning so Python package/workflow setup is
  consistent out of the box.


Non-goals
---------

* cloud orchestration
* broad hypervisor abstraction beyond the current local libvirt focus
* opaque "magic" recovery that hides security-sensitive state changes
* forcing one fixed provisioning stack for all users/workflows
