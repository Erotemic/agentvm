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

Privileged host actions prompt before sudo operations. Use:

* ``--yes`` to auto-approve all prompts
* ``--yes-sudo`` to auto-approve only sudo prompts

Config default:

.. code-block:: toml

   [behavior]
   yes_sudo = true

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
   aivm vm attach --vm aivm-2404 --host_src .

By default, attached folders mount to the same absolute path inside the guest.
Use ``--guest_dst`` to override. Running VMs are live-attached when possible.
``aivm code`` and ``aivm ssh`` remount the selected folder and best-effort
restore other folders already saved for that VM after guest startup.

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
