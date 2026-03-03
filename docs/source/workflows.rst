Workflows
=========

Common daily workflows.

Open project in VM
------------------

.. code-block:: bash

   aivm code .
   aivm vm code . --sync_settings

SSH into mapped directory
-------------------------

.. code-block:: bash

   aivm vm ssh .
   aivm vm ssh_config

Attach folders
--------------

.. code-block:: bash

   aivm attach .
   aivm vm attach --vm aivm-2404 --host_src . --guest_dst /workspace/project

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

Get command tree
----------------

.. code-block:: bash

   aivm help tree
   aivm help plan
