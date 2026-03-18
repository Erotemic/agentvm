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
* Setup/reconcile logs are grouped into step previews so you can see what the
  current step is doing before the commands run.

Use this path when you want minimal setup friction.

Path B: Explicit config-store setup
-----------------------------------

.. code-block:: bash

   aivm config init
   aivm vm create

This path is explicit and reproducible. Here, ``aivm config init`` is required
before ``aivm vm create``.

After either path
-----------------

.. code-block:: bash

   aivm status
   aivm status --sudo
   aivm vm update

Notes
-----

* ``status --sudo`` enables privileged checks (libvirt/network/firewall/image).
* Privileged operations prompt unless ``--yes`` or ``--yes-sudo`` is used.
* Approvals normally happen once per grouped step, not once per command.
* Raw commands are still visible at higher verbosity levels.
