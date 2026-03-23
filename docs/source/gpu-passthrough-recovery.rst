GPU Passthrough Recovery
========================

This page documents recovery and debugging steps for a class of GPU passthrough
failures seen while developing AIVM GPU support.

The goal of this document is to help users recover their host GPU, understand
what state the system is in, and distinguish between:

* a configuration / validation problem,
* a "restart required" problem,
* a guest-driver problem,
* a host VFIO / power-management / reset problem, and
* a motherboard topology / slot-sharing limitation.

This page is intentionally detailed because these failures can strand a GPU in
a state where:

* the guest does not see the GPU,
* the host no longer sees the GPU as a normal NVIDIA device,
* and a naive rollback may still leave the device bound to ``vfio-pci`` on boot.


Scope
-----

This page is based on a real debugging session involving:

* libvirt / QEMU / VFIO GPU passthrough
* Ubuntu host and Ubuntu guest
* two NVIDIA RTX 3090 GPUs
* a motherboard where the lower physical slot was bandwidth-limited and shared
  resources with SATA ports

The exact commands and symptoms may differ on other systems, but the recovery
patterns are broadly useful.


Quick summary
-------------

The most important lessons are:

1. If passthrough is working at the PCI layer, the guest should show an NVIDIA
   device in ``lspci`` **before** guest NVIDIA drivers are installed.

2. If the guest does **not** show an NVIDIA device in ``lspci``, do **not**
   start by installing guest drivers. First determine whether the device is
   present at the PCI layer.

3. A GPU can be:

   * correctly detached from the host,
   * correctly present in libvirt XML,
   * correctly visible to QEMU as a hostdev,

   and still be unusable because the device is stuck in a bad reset / power
   state and returns invalid PCI config space.

4. ``Unknown header type 7f`` / ``HEADER_TYPE=ff`` is a strong sign that the
   device is present-but-dead from the perspective of PCI config-space access.

5. AIVM may install an early-boot initramfs helper that force-binds the GPU to
   ``vfio-pci``. If that helper is not removed during rollback, the GPU will be
   stolen from the host on every boot.

6. A narrow link width (for example ``x2`` on a lower chipset-attached slot)
   can explain poor host-side performance, but **does not by itself** explain
   ``header type 7f`` passthrough failures. Those are separate issues.


Symptom patterns
----------------

Guest does not enumerate an NVIDIA device
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inside the guest:

.. code-block:: bash

   lspci
   sudo lspci -nn | grep -i nvidia

If nothing NVIDIA-related appears, the guest does not currently have a usable
passed-through GPU at the PCI layer.

This means:

* do **not** treat it as a guest NVIDIA driver problem yet
* do **not** expect ``nvidia-smi`` to work in the guest yet

Guest PCI addresses do not need to match host PCI addresses, so the test is
simply whether any NVIDIA PCI device is visible inside the guest.

Host sees the device on ``vfio-pci``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo lspci -nnk -s 03:00.0 -s 03:00.1

A plausible "prepared for passthrough" state looks like:

.. code-block:: text

   03:00.0 ... Kernel driver in use: vfio-pci
   03:00.1 ... Kernel driver in use: vfio-pci

This means the host has handed the device to VFIO, but it does **not** prove
the guest will successfully enumerate it.

Libvirt XML includes hostdevs, but guest still sees nothing
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo virsh dumpxml aivm-2404 | grep -nA12 -B2 '<hostdev'
   sudo virsh dumpxml aivm-2404 --inactive | grep -nA12 -B2 '<hostdev'

Interpretation:

* ``virsh dumpxml <vm>`` shows the **current** running domain configuration
* ``virsh dumpxml <vm> --inactive`` shows the **persistent** config used on the
  next start

If only the inactive config contains the GPU hostdevs, the VM likely needs a
full stop/start.

If both current and inactive config contain the hostdevs, but the guest still
sees no GPU, the problem is deeper than "restart required".

QEMU sees the hostdev, but guest rejects it
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo virsh qemu-monitor-command aivm-2404 --hmp 'info pci'

If QEMU shows something like:

.. code-block:: text

   Bus 15, device 0, function 0:
     VGA controller: PCI device 10de:2204
     id "hostdev0"
   Bus 16, device 0, function 0:
     Audio controller: PCI device 10de:1aef
     id "hostdev1"

then QEMU has attached the devices into the guest PCI topology.

If the guest still logs:

.. code-block:: text

   pci 0000:0f:00.0: [10de:2204] type 7f class 0xffffff conventional PCI
   pci 0000:0f:00.0: unknown header type 7f, ignoring device

then the guest is receiving a broken PCI device image. This is **not** a guest
driver problem; it is a host-side VFIO / power / reset problem.

Host-side "stuck in D3" / "header type 7f" failure
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Common host-side indicators:

.. code-block:: bash

   sudo lspci -nnvv -s 03:00.0
   sudo lspci -nnvv -s 03:00.1
   sudo setpci -s 03:00.0 HEADER_TYPE
   sudo setpci -s 03:00.1 HEADER_TYPE
   sudo dmesg | grep -Ei 'vfio|D3cold|header type|reset|03:00'

Examples of bad signs:

.. code-block:: text

   !!! Unknown header type 7f
   ff
   vfio: Unable to power on device, stuck in D3
   Unknown PCI header type '127'
   Cannot read device rom

When this happens, the device is usually in a bad host-side state. The guest
cannot use it even if the XML and QEMU topology are correct.


Companion function false positives
----------------------------------

NVIDIA GPUs commonly expose at least two PCI functions on the same slot:

* ``BB:DD.0`` -- GPU / VGA function
* ``BB:DD.1`` -- HDMI / DisplayPort audio function

These are often in the same IOMMU group and should usually be handled together.

A real example looked like this:

.. code-block:: text

   IOMMU group 17:
   0000:03:00.0
   0000:03:00.1

That is a **good** group if it contains only the GPU function and its audio
companion.

AIVM should not treat ``03:00.1`` as an "unexpected unrelated device" in this
case. If only ``03:00.0`` is declared, then the likely problem is either:

* the companion function needs to be auto-included in the effective passthrough
  set, or
* the UI should say "missing companion function", not
  "shared with non-companion devices".


How to determine what state you are in
--------------------------------------

1. Check whether the guest sees NVIDIA at all
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inside the guest:

.. code-block:: bash

   sudo lspci -nn | grep -i nvidia

If this is empty, do not proceed to guest driver installation.

2. Check the running and persistent libvirt config
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo virsh dumpxml aivm-2404 | grep -nA12 -B2 '<hostdev'
   sudo virsh dumpxml aivm-2404 --inactive | grep -nA12 -B2 '<hostdev'

3. Check the host binding and IOMMU group
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo lspci -nnk -s 03:00.0 -s 03:00.1
   readlink -f /sys/bus/pci/devices/0000:03:00.0/iommu_group
   readlink -f /sys/bus/pci/devices/0000:03:00.1/iommu_group

   GROUP=$(basename "$(readlink -f /sys/bus/pci/devices/0000:03:00.0/iommu_group)")
   find /sys/kernel/iommu_groups/$GROUP/devices -maxdepth 1 -type l -printf '%f\n' | sort

A good group for a GPU passthrough pair often looks like:

.. code-block:: text

   0000:03:00.0
   0000:03:00.1

4. Check whether the device is actually healthy on the host
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo lspci -nnvv -s 03:00.0
   sudo lspci -nnvv -s 03:00.1
   sudo setpci -s 03:00.0 HEADER_TYPE
   sudo setpci -s 03:00.1 HEADER_TYPE

Healthy devices should **not** return ``ff`` from ``HEADER_TYPE``.

5. Check whether QEMU has attached the device
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

On the host:

.. code-block:: bash

   sudo virsh qemu-monitor-command aivm-2404 --hmp 'info pci'

If QEMU shows NVIDIA hostdevs but the guest rejects them, the problem is likely
host-side reset / power-state recovery.

6. Check the guest kernel log
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Inside the guest:

.. code-block:: bash

   sudo dmesg -T | grep -Ei 'pci|nvidia|BAR|AER|resource|vfio'

If the guest reports ``type 7f`` / ``unknown header type 7f``, it is seeing the
same broken device state that the host saw.


Common failure modes
--------------------

Failure mode 1: VM needed a full restart
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Symptoms:

* inactive XML contains hostdevs
* current running XML does not
* guest sees no NVIDIA device

Fix:

* perform a full libvirt stop/start, not just an in-guest reboot

Failure mode 2: companion-function validation false positive
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Symptoms:

* group contains only ``03:00.0`` and ``03:00.1``
* both are bound appropriately
* AIVM says ``03:00.1`` is an unexpected IOMMU-group member

Fix:

* treat same-slot GPU/audio functions as expected companions
* build the *effective* passthrough set before IOMMU-group validation

Failure mode 3: QEMU attached the device, but config space is broken
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Symptoms:

* current XML contains hostdevs
* QEMU monitor shows the NVIDIA hostdevs
* guest logs ``unknown header type 7f``
* host logs ``stuck in D3`` or ``HEADER_TYPE=ff``

Fix:

* this is a VFIO / power / reset problem, not a guest driver problem
* do not install guest drivers yet
* first recover the host-side device state

Failure mode 4: rollback did not restore the GPU to the host
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Symptoms:

* host still shows one or both functions on ``vfio-pci``
* ``nvidia-smi`` still sees only one GPU
* AIVM or initramfs keeps reclaiming the device after reboot

Fix:

* remove the libvirt hostdev config
* remove any AIVM-generated early-boot VFIO binder
* rebuild initramfs
* reboot or cold boot


Host recovery when the GPU is stuck on VFIO
-------------------------------------------

If the goal is to get the GPU back to the host, follow this order.

1. Stop the VM
~~~~~~~~~~~~~~

.. code-block:: bash

   sudo virsh shutdown aivm-2404 || true
   sleep 5
   sudo virsh destroy aivm-2404 || true

2. Remove hostdevs from the VM
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   sudo virsh edit aivm-2404

Delete the two ``<hostdev>`` blocks for the passed-through GPU functions.

3. Remove AIVM's early-boot VFIO binder
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

In the observed case, AIVM had generated an initramfs helper like:

.. code-block:: text

   /etc/initramfs-tools/scripts/init-top/aivm-vfio-bind-aivm-2404

That script force-bound:

* ``0000:03:00.0``
* ``0000:03:00.1``

to ``vfio-pci`` during early boot.

Remove it and rebuild initramfs:

.. code-block:: bash

   sudo rm -f /etc/initramfs-tools/scripts/init-top/aivm-vfio-bind-aivm-2404
   sudo update-initramfs -u

Search for any remaining persistent VFIO capture rules:

.. code-block:: bash

   grep -RInE 'vfio-pci|vfio_pci|10de:2204|10de:1aef|03:00.0|03:00.1' \
     /etc/modprobe.d /etc/default/grub /etc/initramfs-tools /etc/kernel \
     ~/.config/aivm 2>/dev/null

4. Reattach / rebind to host drivers
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Audio function:

.. code-block:: bash

   sudo modprobe snd_hda_intel

   echo "" | sudo tee /sys/bus/pci/devices/0000:03:00.1/driver_override
   echo 0000:03:00.1 | sudo tee /sys/bus/pci/drivers/vfio-pci/unbind
   echo 0000:03:00.1 | sudo tee /sys/bus/pci/drivers_probe

GPU function:

.. code-block:: bash

   sudo modprobe nvidia nvidia_drm nvidia_modeset nvidia_uvm

   echo "" | sudo tee /sys/bus/pci/devices/0000:03:00.0/driver_override
   echo 0000:03:00.0 | sudo tee /sys/bus/pci/drivers/vfio-pci/unbind 2>/dev/null || true
   echo 0000:03:00.0 | sudo tee /sys/bus/pci/drivers_probe

Then check:

.. code-block:: bash

   sudo lspci -nnk -s 03:00.0 -s 03:00.1
   nvidia-smi

5. Reboot if needed
~~~~~~~~~~~~~~~~~~~

If the GPU function still does not come back, reboot now that the forced VFIO
binding is gone:

.. code-block:: bash

   sudo reboot

6. Cold power cycle if needed
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If a normal reboot still does not restore the card, do a full shutdown, remove
AC power, wait 30 seconds, then boot again. This may be needed when the device
was left in a bad reset / power state.


Do not install guest NVIDIA drivers until this is true
------------------------------------------------------

Inside the guest, do **not** proceed with guest driver installation until:

.. code-block:: bash

   sudo lspci -nn | grep -i nvidia

shows at least one NVIDIA device.

If guest ``lspci`` is empty, the problem is still at the PCI passthrough layer.


Power-management and reset debugging
------------------------------------

The following are useful debugging steps when the GPU is attached to QEMU but
returns ``header type 7f`` or ``HEADER_TYPE=ff``.

Disable idle D3 in ``vfio-pci``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Create:

.. code-block:: text

   /etc/modprobe.d/vfio-pci-power.conf

with:

.. code-block:: text

   options vfio-pci disable_idle_d3=1

Then rebuild initramfs and reboot:

.. code-block:: bash

   sudo update-initramfs -u
   sudo reboot

After reboot:

.. code-block:: bash

   cat /sys/module/vfio_pci/parameters/disable_idle_d3

Turn off PCI runtime PM for the device and upstream bridge
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

.. code-block:: bash

   echo on | sudo tee /sys/bus/pci/devices/0000:03:00.0/power/control
   echo on | sudo tee /sys/bus/pci/devices/0000:03:00.1/power/control
   echo on | sudo tee /sys/bus/pci/devices/0000:00:1b.0/power/control

Try explicit node-device resets
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

With the VM off:

.. code-block:: bash

   sudo virsh nodedev-reset pci_0000_03_00_0
   sudo virsh nodedev-reset pci_0000_03_00_1

These steps can help, but a platform may still lack a reset path strong enough
to recover the device.

Disable ROM BAR probing
~~~~~~~~~~~~~~~~~~~~~~~

In some cases, adding ``<rom bar='off'/>`` to each ``<hostdev>`` can remove
noise from option ROM probing. This does **not** fix a fundamentally broken
device state, but it can avoid extra ROM-related failures.

Example:

.. code-block:: xml

   <hostdev mode='subsystem' type='pci' managed='yes'>
     <driver name='vfio'/>
     <source>
       <address domain='0x0000' bus='0x03' slot='0x00' function='0x0'/>
     </source>
     <rom bar='off'/>
     <address type='pci' domain='0x0000' bus='0x0f' slot='0x00' function='0x0'/>
   </hostdev>


Physical topology limitations
-----------------------------

In the observed system, the secondary GPU was physically forced into the lower
full-length slot on an ASUS ROG STRIX Z590-E GAMING WIFI motherboard.

That matters because:

* the lower full-length slot is chipset-attached
* it can be lane-shared with specific SATA ports
* it may run at reduced width
* poor link width can explain poor host-side GPU performance

Important distinction:

* poor link width (for example ``x2``) explains poor performance
* poor link width alone does **not** explain ``header type 7f``

Example debugging commands:

.. code-block:: bash

   sudo lspci -vv -s 00:1b.0 | grep -E 'LnkCap|LnkSta'
   sudo dmesg | grep -E 'ata[1-6](\.00)?:'

In the observed case, ``ata5`` and ``ata6`` were occupied, and the upstream root
port reported:

.. code-block:: text

   LnkCap: Speed 8GT/s, Width x2
   LnkSta: Speed 2.5GT/s, Width x2

That explained poor performance on the lower GPU, but not the broken passthrough
header state.

Weekend / physical follow-up items
----------------------------------

If the current physical configuration cannot be changed immediately, it may be
best to defer passthrough and use the GPU on the host.

When there is time for hardware work, likely follow-ups are:

* move SATA devices off the ports shared with the lower GPU slot
* retest link width on the upstream root port
* update motherboard BIOS / firmware
* evaluate whether the lower slot remains too constrained for reliable passthrough
* consider whether the board / slot topology is fundamentally a poor fit for
  passthrough of that secondary GPU


Minimal "is it usable today?" decision tree
-------------------------------------------

If you just need a fast answer:

1. Inside the guest:

   .. code-block:: bash

      sudo lspci -nn | grep -i nvidia

2. If empty, on the host:

   .. code-block:: bash

      sudo setpci -s 03:00.0 HEADER_TYPE
      sudo setpci -s 03:00.1 HEADER_TYPE

3. Interpret:

   * guest sees NVIDIA -> now install guest drivers if needed
   * guest sees nothing, host ``HEADER_TYPE`` is sane -> keep debugging libvirt / guest enumeration
   * guest sees nothing, host ``HEADER_TYPE=ff`` -> stop; device is unhealthy on the host side

If ``HEADER_TYPE=ff``, this is not a guest-driver problem.


How AIVM could be extended
--------------------------

The following AIVM improvements would reduce the chance of stranding users in
these corner cases.

1. Restart-aware ``aivm code .``
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

``aivm code .`` should detect when current vs persistent libvirt config differ
in ways that require a full stop/start, especially for PCI hostdev changes.

Desired behavior:

* detect pending cold-plug changes
* prompt in interactive mode
* if accepted, stop/start the VM
* wait for readiness
* then continue opening VS Code

2. Companion-function expansion
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When a GPU primary function such as ``BB:DD.0`` is declared, AIVM should build
an *effective passthrough set* that includes expected companion functions such
as ``BB:DD.1`` when appropriate.

Validation should compare IOMMU-group members against the effective set, not
only the raw declared set.

3. Stronger host readiness checks
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Current readiness checks should be extended to detect:

* ``HEADER_TYPE=ff``
* ``Unknown header type 7f``
* ``stuck in D3`` / ``Unable to power on device`` in recent host logs
* option-ROM read failures that indicate deeper config-space problems

A device should not be labeled "ready" if it is VFIO-bound but its config space
is unreadable.

4. Clearer error taxonomy
~~~~~~~~~~~~~~~~~~~~~~~~~

Instead of collapsing many cases into a generic "not ready" error, AIVM should
differentiate between:

* ``restart_required``
* ``missing_companion_function``
* ``unrelated_iommu_group_members``
* ``host_device_bad_power_state``
* ``host_device_unreadable_config_space``
* ``persistent_vfio_boot_binding_present``

5. Safe rollback / host restoration command
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

AIVM should provide a first-class command to restore a GPU to the host, for
example:

.. code-block:: text

   aivm host pci restore 0000:03:00.0

That command should:

* stop affected VMs if needed
* remove persistent hostdev mappings
* remove any AIVM-generated initramfs VFIO bind helpers
* rebuild initramfs if needed
* reattach / reprobe host drivers
* explain when reboot or cold boot is required

6. Detect AIVM-generated early-boot VFIO binders
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When AIVM generates early-boot binding helpers under ``/etc/initramfs-tools``,
it should also:

* track them explicitly in its state
* expose them in status output
* warn the user during rollback if they still exist
* provide a supported way to remove them

7. More topology awareness
~~~~~~~~~~~~~~~~~~~~~~~~~~

AIVM could warn users when a GPU is on a suspicious path, for example:

* chipset-attached lower slot
* reduced link width
* slot sharing with SATA ports
* narrow negotiated link width compared to capability

This would not necessarily block passthrough, but it would set expectations and
help explain poor performance or instability.

8. Better user-facing docs in the CLI
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

When errors occur, AIVM should point users to a dedicated recovery page like
this one, not just emit a short command hint.

Examples:

* "The guest does not enumerate the GPU at PCI level; do not install guest
  drivers yet"
* "This device is present in QEMU but unreadable by PCI config space"
* "AIVM has installed an initramfs VFIO binder; remove it to restore the GPU to
  the host"


Recommended follow-up for this document
---------------------------------------

If GPU passthrough support is resumed later, this page should be linked from:

* GPU passthrough feature docs
* ``aivm host pci check`` help text
* rollback / restore commands
* passthrough start failures
* restart-required prompts
* troubleshooting docs

This document is intentionally conservative: if the guest does not enumerate an
NVIDIA device and the host returns ``HEADER_TYPE=ff`` or ``header type 7f``,
assume the device is not usable by the guest until the host-side state is
recovered.
