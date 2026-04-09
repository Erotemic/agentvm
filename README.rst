The aivm Module
===============


.. warning::

   This project was written with GPT-5.3 Codex and is still being evaluated
   for correctness and safety. It is for experimental use only.
   See the `Security Model <docs/source/security.rst>`_ for the threat model
   and security posture.


|Pypi| |PypiDownloads| |ReadTheDocs| |GithubActions| |Codecov|



+---------------+-----------------------------------------+
| Read the Docs | https://aivm.readthedocs.io/en/latest/  |
+---------------+-----------------------------------------+
| Pypi          | https://pypi.org/project/aivm           |
+---------------+-----------------------------------------+

A small Python CLI to **create and manage a local libvirt/KVM Ubuntu 24.04 VM**
designed for running coding agents with a stronger boundary than containers.

What it provides
----------------

* Dedicated libvirt NAT network per ``aivm`` configuration
* Optional host firewall isolation via nftables
* Ubuntu cloud-image VM provisioning via cloud-init
* SSH + VS Code Remote-SSH workflows
* Optional virtiofs folder sharing (explicit trust extension)
* Optional settings sync into the guest user profile
* A single config store for defaults, VMs, networks, and attachments

.. note::

   Two opt-in end-to-end test modules live in ``tests/``: ``test_e2e_nested.py``
   (light smoke path) and ``test_e2e_full.py`` (comprehensive cycle).  They are
   skipped by default; to run them locally set ``AIVM_E2E=1`` and invoke pytest
   manually.  These tests require a host with libvirt/KVM, passwordless ``sudo``
   and (optionally) an Ubuntu cloud image cached under
   ``~/.cache/aivm/e2e``.

   An additional opt-in bootstrap-context e2e test is available in
   ``test_e2e_bootstrap_context.py``. It creates a fresh outer VM and runs the
   host-context e2e suite inside that VM. Enable it with
   ``AIVM_E2E_BOOTSTRAP=1`` when running ``./run_e2e_tests.sh``.

Install
-------

.. code-block:: bash

   uv pip install .

Fast Start
----------

Recommended for new repos:

Currently ``aivm config init``  is required, but we will make that implicit in a future version.

.. code-block:: bash

   aivm code .
   aivm status
   aivm status --sudo   # optional deeper privileged checks

``aivm code .`` auto-selects/bootstraps VM context from the global config store
(``~/.config/aivm/config.toml``), attaches the current folder if needed, and
opens VS Code.

During setup and reconcile flows, subprocess logging is now organized around
user-meaningful steps instead of isolated commands. ``aivm`` shows the current
step, why it exists, a semantic summary for each planned command, and the exact
command line that will run before it executes the step. Full raw commands still
appear at higher verbosity.

If you prefer an explicit flow, ``aivm config init`` is required before
``aivm vm create``.

See also:

* `Design Contract <docs/source/design.rst>`_
* `Quickstart <docs/source/quickstart.rst>`_
* `Workflows <docs/source/workflows.rst>`_

Status and sudo behavior
------------------------

By default, ``aivm status`` avoids privileged probes. Use ``--sudo`` for
network/firewall/libvirt/image checks.

Command manager defaults:

* subprocess execution is centralized through a command manager
* logs are grouped into step/plan previews with nested context
* read-only sudo probes (inspect/query/status) are auto-approved by default
* state-changing sudo steps still prompt unless ``--yes``/``--yes-sudo`` is set
* approval usually happens once per grouped step, not once per command

Grouped approval does **not** widen privilege beyond the commands shown in the
step preview. The preview is the approval boundary.

Use:

* ``--yes`` to auto-approve all prompts
* ``--yes-sudo`` to auto-approve only sudo prompts

When running interactively, expect step previews such as:

* current context / breadcrumb
* current step title
* why the step exists
* semantic summaries plus exact commands for the current step
* a single approval prompt for the whole step when required

Interactive approval semantics:

* ``y`` approves the current step only
* ``a`` approves the current step and all later steps
* ``s`` shows the full exact commands for the current step, then reprompts

For example, the default ``shared-root`` path used by ``aivm ssh .`` /
``aivm code .`` now groups attachment reconciliation into named steps such as
inspecting host bind state, preparing host bind targets, ensuring the VM
virtiofs mapping, and mounting/verifying the bind inside the guest.

Readable previews may abbreviate long shell payloads, but the full exact
commands are still available on demand in the approval prompt and are always
logged when they actually run.

Config defaults:

.. code-block:: toml

   [behavior]
   yes_sudo = false
   auto_approve_readonly_sudo = true  # set false for strict "prompt every sudo" mode

Common Workflows
----------------

VS Code and SSH

.. code-block:: bash

   aivm vm ssh_config

.. code-block:: bash

   aivm code . --sync_settings
   aivm vm code --host_src . --sync_settings
   aivm vm code . --sync_settings
   aivm vm ssh .

Folder attachment

.. code-block:: bash

   aivm attach .
   aivm detach .
   aivm vm attach --vm aivm-2404 --host_src .
   aivm attach . --mode git

Attachment modes:

* ``shared-root`` (default for new attachments): legacy behavior. One VM-level virtiofs mapping
  exports ``/var/lib/libvirt/aivm/<vm>/shared-root``; each attached folder is
  bind-mounted under that root on host and then bind-mounted to ``guest_dst`` in
  guest.
* ``declared``: preferred new shared-root path. It still uses the single
  VM-level shared-root virtiofs export and staged host binds, but it writes a
  persisted attachment manifest, installs a guest systemd replay helper, and
  lets boot / ``aivm code .`` / ``aivm ssh .`` repair guest-visible bind mounts
  from that manifest instead of rebuilding every attachment from scratch.
* ``shared``: direct per-folder virtiofs mapping from host source to guest. This
  is simpler but consumes one VM virtiofs device slot per folder.
* ``git``: guest-local Git clone, synced via host/guest remotes.

In ``shared``, ``shared-root``, ``declared``, and ``git`` modes, attached folders
mount to the same absolute path inside the guest by default unless
``--guest_dst`` overrides it. Running VMs are
live-attached when possible.
``aivm code`` and ``aivm ssh`` remount the selected folder and best-effort
restore other folders already saved for that VM after guest startup.

For ``declared`` attachments, explicit unshare updates the stored declaration
and refreshes the replay manifest instead of depending on interactive teardown
of the stable host-side staged bind mount.

Major limitation: shared-mode folder count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each ``shared`` folder uses a dedicated virtiofs device mapping in the VM
definition. Attaching many folders can hit VM device-slot limits (for example
PCI/PCIe capacity), which surfaces from libvirt as errors like
``No more available PCI slots`` during attach/restore.

``shared-root`` and ``declared`` reduce this pressure by using one persistent
virtiofs mapping per VM and per-attachment host/guest bind mounts.
Its host-side preparation is also designed to avoid mutating the ownership or
permissions of the user's source tree; ``aivm`` prepares only its own internal
directories and does not recursively rewrite a bind-mounted project path.

Workarounds today:

* detach unused shared folders
* prefer ``--mode git`` for folders that do not need live writable host sharing
* split large folder sets across multiple VMs

Use ``--mode git`` to keep a normal Git repo on guest disk instead of exposing
a writable virtiofs share. In that mode, ``aivm`` configures the guest repo to
accept host pushes via ``receive.denyCurrentBranch=updateInstead`` and
registers a host-side remote pointing at the guest repo over the VM SSH alias.
The host can push committed branch state into the VM and fetch guest commits
back later. Uncommitted host changes stay on the host until you commit them.

``aivm code --mode git .`` behavior:

* New folder (no saved attachment): creates/uses a git-mode attachment and
  defaults the guest destination to the exact host path.
* Folder previously attached as ``shared`` or ``shared-root``: returns an error
  (mode mismatch). Detach + reattach is required to switch modes.
* ``aivm code .`` without ``--mode``: reuses saved mode if present; otherwise
  creates a new ``shared-root`` attachment.

Migration note:

* ``declared`` is the rollout path for eventually replacing legacy
  ``shared-root`` attachment churn. Existing ``shared-root`` attachments keep
  working unchanged. Reattach a folder with ``aivm detach .`` then
  ``aivm attach . --mode declared`` when you want the new persisted replay
  behavior.

Mode selection behavior:

* New folder (no saved attachment record): defaults to ``shared-root`` unless
  ``--mode`` is explicitly set.
* Existing folder attachment: omitting ``--mode`` reuses the saved mode for that
  ``(host folder, VM)`` pair.
* Existing folder attachment + explicit different ``--mode``: this now errors.
  You must explicitly detach then reattach to change mode:

.. code-block:: bash

   aivm detach .
   aivm attach . --mode git

Inventory and visibility

.. code-block:: bash

   aivm list
   aivm vm list
   aivm list --section vms
   aivm list --section networks
   aivm list --section folders
   aivm status --detail

Config-store lifecycle (explicit flow)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   aivm config init
   aivm vm create
   aivm vm sync_settings
   aivm vm update
   aivm config discover
   aivm config show
   aivm config edit
   aivm help plan
   aivm help tree
   aivm help completion
   aivm host doctor

Settings sync configuration

.. code-block:: toml

   [sync]
   enabled = true
   overwrite = true
   paths = [
     "~/.gitconfig",
     "~/.gitignore",
     "~/.config/Code/User/settings.json",
     "~/.config/Code/User/keybindings.json",
     "~/.tmux.conf",
     "~/.bashrc",
   ]

Ad hoc override:

.. code-block:: bash

   aivm vm sync-settings \
     --paths "~/.gitconfig,~/.config/Code/User/settings.json,~/.tmux.conf"

When ``[sync].enabled=true``, ``aivm vm code ...`` syncs before launching VS Code.

Command Groups
--------------

.. code-block:: bash

   aivm config --help
   aivm host --help
   aivm host image_fetch --help
   aivm help --help
   aivm host net --help
   aivm host fw --help
   aivm vm --help

Safety Notes
------------

* This tool assumes **Linux + libvirt**. It focuses on Debian/Ubuntu hosts for dependency installation.
* Security model and threat model details: the `Security Model <docs/source/security.rst>`_.
* NAT alone does not prevent VM -> LAN. Enable firewall isolation if you want "internet-only" access.
* To allow specific VM->host or VM->blocked-LAN service ports while firewall isolation is enabled, set ``[firewall].allow_tcp_ports`` / ``allow_udp_ports`` in config (for example ``allow_tcp_ports = [22, 5432]``).
* virtiofs sharing is optional; it's powerful, but it intentionally exposes that host directory to the VM.
* ``aivm vm code`` requires VS Code's ``code`` CLI and the Remote - SSH extension.


.. |Pypi| image:: https://img.shields.io/pypi/v/aivm.svg
    :target: https://pypi.python.org/pypi/aivm

.. |PypiDownloads| image:: https://img.shields.io/pypi/dm/aivm.svg
    :target: https://pypistats.org/packages/aivm

.. |ReadTheDocs| image:: https://readthedocs.org/projects/aivm/badge/?version=latest
    :target: https://aivm.readthedocs.io/en/latest/

.. |GithubActions| image:: https://github.com/Erotemic/aivm/actions/workflows/tests.yml/badge.svg
    :target: https://github.com/Erotemic/aivm/actions?query=branch%3Amain

.. |Codecov| image:: https://codecov.io/github/Erotemic/aivm/badge.svg?branch=main&service=github
    :target: https://codecov.io/github/Erotemic/aivm?branch=main
