Quickstart
==========

Choose one of these startup paths.

Path A: One-command project entry (recommended)
-----------------------------------------------

.. code-block:: bash

   aivm code .

Behavior:

* Uses global config store at ``~/.config/aivm/config.toml``.
* If VM context is missing, ``aivm`` can bootstrap required config/VM steps.
* Attaches current folder and opens VS Code.
* Major setup/reconcile logs are grouped into step previews so you can see what
  the current step is doing before the commands run.
* For default ``shared-root`` attachments, those steps usually include host
  bind inspection/repair, VM mapping checks, and guest mount verification.

Use this path when you want minimal setup friction.

Path B: Explicit config-store setup
-----------------------------------

.. code-block:: bash

   aivm config init
   aivm vm create

This path is explicit and reproducible. ``aivm config init`` establishes VM
defaults and SSH identity configuration; ``aivm vm create`` provisions the VM.

After either path
-----------------

.. code-block:: bash

   aivm status
   aivm status --sudo
   aivm vm update

Optional stable GPU passthrough
-------------------------------

.. code-block:: bash

   aivm vm gpu attach --vm myvm
   sudo reboot
   aivm vm up --vm myvm

This workflow can make explicit host-level VFIO boot-binding changes managed by
``aivm``. Those changes are high risk by design:

* the host and guest cannot use the GPU at the same time
* after reboot, the host may lose display/compute access on that GPU
* undoing the change later also requires another host reboot

Undo with ``aivm``:

.. code-block:: bash

   aivm vm gpu detach 0000:65:00.0 --vm myvm
   sudo reboot

Manual undo:

* remove the AIVM-managed ``aivm-vfio-*`` files from
  ``/etc/modules-load.d/`` and ``/etc/initramfs-tools/scripts/init-top/``
* run ``sudo update-initramfs -u``
* reboot the host

Notes
-----

* ``status --sudo`` enables privileged checks (libvirt/network/firewall/image).
* ``status --detail`` includes raw diagnostics (virsh/nft/ssh probe outputs).
* Privileged operations prompt unless ``--yes`` or ``--yes-sudo`` is used.
* Approvals normally happen once per grouped step, not once per command.
* Step previews show both semantic summaries and the exact commands to be run.
* ``s`` shows the full exact commands for the current step, then reprompts.
* ``y`` approves the current step only; ``a`` approves the current and all
  later steps.
* Full executed commands are always logged; raw commands are also still visible
  at higher verbosity levels.
* Shared-root setup is designed to avoid changing ownership/perms of your host
  source tree.
