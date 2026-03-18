Workflows
=========

Common daily workflows.

Open project in VM
------------------

.. code-block:: bash

   aivm code .
   aivm vm code . --sync_settings

These shortcut flows now report progress as grouped steps. Expect a current
step title, a short explanation of why that step is happening, and one approval
prompt for the whole step when privileged host changes are required.

For the default ``shared-root`` attachment path, the current implementation
usually breaks reconciliation into:

* inspect shared-root host bind state
* prepare host bind targets
* inspect/ensure the VM virtiofs mapping
* mount and verify the bind inside the guest

SSH into mapped directory
-------------------------

.. code-block:: bash

   aivm vm ssh .
   aivm vm ssh_config

Attach folders
--------------

.. code-block:: bash

   aivm attach .
   aivm detach .
   aivm vm attach --vm aivm-2404 --host_src . --guest_dst /workspace/project
   aivm attach . --mode git

``aivm code`` / ``aivm ssh`` restore the requested folder and attempt to
remount the VM's other saved folder attachments after reboot.

Attachment modes:

* ``shared-root`` (default for new attachments): one persistent VM virtiofs
  mapping and per-folder host/guest bind mounts.
* ``shared``: direct per-folder virtiofs mapping.
* ``git``: guest-local Git clone synced by host/guest remotes.

``--mode git`` switches the attachment to a normal guest-local repo. That
avoids a writable host share and adds a host-side Git remote pointing at the
guest repo. ``aivm`` configures the guest side with
``receive.denyCurrentBranch=updateInstead`` so the host can push committed
branch state into the checked-out guest repo and fetch guest commits later.
Git-mode default guest paths are chosen under ``/home/<vm-user>/...`` so sync
does not depend on root-owned guest paths; use ``--guest_dst`` to override.

Major limitation: shared folder count
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Each ``shared`` folder consumes a dedicated virtiofs device mapping. Large
attachment sets can exhaust VM device-slot capacity (for example PCI/PCIe
slots), causing attach/restore failures such as
``No more available PCI slots``.

``shared-root`` reduces this pressure by using a single persistent virtiofs
mapping per VM.

If this happens, prefer ``--mode git`` for some folders, detach unused shared
folders, or split the workload across multiple VMs.

Attachment mode rules:

* New folder defaults to ``shared-root`` when ``--mode`` is omitted.
* Existing folder reuses its saved mode when ``--mode`` is omitted.
* Changing mode for an existing folder requires explicit detach + reattach.
  Passing a different ``--mode`` directly now returns an error.

``aivm code --mode git .`` specifics:

* New folder: attaches in ``git`` mode, with default guest path under
  ``/home/<vm-user>/...``.
* Previously attached ``shared``/``shared-root`` folder: errors until you detach
  and reattach in ``git`` mode.
* No explicit mode (``aivm code .``): use saved mode when present, else create a
  new ``shared-root`` attachment.

.. code-block:: bash

   aivm detach .
   aivm attach . --mode git

Inspect and list resources
--------------------------

.. code-block:: bash

   aivm status
   aivm status --detail
   aivm list
   aivm list --section vms
   aivm list --section networks
   aivm list --section folders

Manage config store
-------------------

.. code-block:: bash

   aivm config show
   aivm config edit
   aivm config discover

Sync host settings into guest
-----------------------------

.. code-block:: bash

   aivm vm sync_settings
   aivm vm sync-settings --paths "~/.gitconfig,~/.tmux.conf"

Reconcile VM drift
------------------

.. code-block:: bash

   aivm vm update

Workflow logging model
----------------------

``aivm`` command execution is organized around:

* nested intent context, which keeps the larger goal visible
* step/plan previews, which describe what the current sequence of commands is
  about to do
* raw commands, which remain visible for deeper inspection at higher verbosity

This is meant to make multi-command workflows easier to follow and safer to
approve than a stream of isolated sudo command prompts. Shared-root
attach/reconcile is the most complete example today; some older helper flows
still rely on the compatibility shim while migration continues.

Get command tree
----------------

.. code-block:: bash

   aivm help tree
   aivm help plan
   aivm help completion
