Security Model (Malicious-Guest Focus)
=====================================

Purpose and scope
-----------------

This document describes the security model for ``aivm`` under its intended use:
running potentially untrusted agent code inside a local libvirt/KVM VM while
preserving host safety and reasonable operator usability.

The primary question this model answers:

* If the guest is malicious, what paths exist for it to affect or learn about
  the host beyond explicitly shared resources?

This document is about *threats and mitigations* at the VM boundary. It is not
a complete operational hardening guide for all host services.

References:
* QEMU security guide: `QEMU Security`_
* libvirt QEMU/KVM driver overview: `libvirt QEMU driver`_


Threat model
------------

Primary attacker:

* Malicious code running inside the guest VM as the configured guest user
  (which may have root/sudo inside the VM).

In-scope goals:

* Prevent guest code from reading/modifying host data outside explicitly shared
  host folders during normal operation.
* Reduce guest access to host-local/private networks by default (when firewall
  isolation is enabled and correctly applied).
* Keep host-privileged operations explicit, observable, and user-approved.

Out-of-scope assumptions (but see notes below):

* The host user account is trusted.
* The operator keeps the host OS, kernel, and virtualization stack patched.
* Physical/firmware attacks are not addressed here.

Important nuance:

* ``aivm`` cannot “prove” the absence of VM-escape bugs. Even with correct
  configuration, a sufficiently capable attacker may escape via vulnerabilities
  in the virtualization stack (QEMU device emulation, KVM, vhost backends,
  virtiofsd, microarchitectural side-channels). See `Historical examples`_.

References:
* QEMU’s statement on attack surface (emulated devices, monitor): `QEMU Security`_
* KVM escape case study (example of kernel-level breakout): `Project Zero: EPYC escape (CVE-2021-29657)`_


Trust boundaries and data classification
----------------------------------------

Host boundary (trusted):

* Host user account and local filesystem.
* Host ``sudo`` privileges when explicitly approved by the operator.

Guest boundary (untrusted):

* All code and data inside the VM, including guest root.
* Guest network traffic and any external inputs it consumes.

Shared boundary (explicitly extended trust by user decision):

* Host directories exported into the VM (e.g. virtiofs mounts).
* Anything written into a shared tree by the guest is considered *guest-controlled*.

Network boundary:

* Guest has WAN egress by design (to support development and API access).
* When firewall isolation is enabled, guest access to common private/LAN ranges
  is intended to be blocked unless explicitly allowed.

References:
* libvirt filesystem sharing + virtiofs configuration: `libvirt Domain XML (filesystems/virtiofs)`_
* libvirt network filtering concepts (optional): `libvirt NWFilter`_


What “explicitly shared folders” really means (shared-folder pivot risk)
------------------------------------------------------------------------

Even without a hypervisor escape, a malicious guest can compromise the host
*indirectly* by modifying shared content that the host later executes or trusts.

Common examples:

* modifying a repository so the host later runs a poisoned build step
  (Makefiles, build scripts, packaging metadata, test harnesses);
* adding or changing shell/profile files or editor tooling inside shared trees;
* inserting malicious artifacts that are later opened by the host (documents,
  notebooks, model files, etc.).

Guidance:

* Treat shared trees as **untrusted input** to the host.
* Avoid sharing directories that contain host secrets (``~/.ssh``, ``~/.aws``,
  ``~/.kube``, ``.env`` files, password stores).
* Prefer sharing a minimal project subtree rather than ``$HOME``.
* Prefer read-only sharing when possible.

References:
* virtiofs overview and constraints: `libvirt virtiofs guide`_


Guest-to-host attack surface (how escapes and host-impact happen)
-----------------------------------------------------------------

From the guest’s perspective, the host “surface” consists of:

1) QEMU userspace device emulation (and helpers)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The guest interacts with virtual hardware devices (virtio-net, virtio-blk, etc.)
implemented by QEMU and/or related components. Bugs here can allow guest-to-host
code execution in the *QEMU process context*.

Additionally, **QEMU’s monitor interfaces** (QMP/HMP) are extremely sensitive;
if exposed to an attacker, they can instruct QEMU to access host files or spawn
processes (depending on configuration). ``aivm`` should treat QEMU monitor access
as host-privileged.

References:
* QEMU security guide (emulated devices, monitor risk): `QEMU Security`_

2) Kernel virtualization paths (KVM + vhost backends)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Some “fast path” virtio implementations use kernel backends (e.g. vhost-net).
This can shift attack surface into the host kernel, which increases impact if
a kernel bug is exploited.

libvirt documents that for virtio NICs the backend driver can be either:

* ``qemu``: userspace backend
* ``vhost``: kernel backend

and that the default will try ``vhost`` if present and silently fall back to
``qemu`` otherwise.

References:
* libvirt interface driver backend (qemu vs vhost, default behavior):
  `libvirt Domain XML (interface driver backend)`_

3) Host filesystem sharing daemon (virtiofsd)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

virtiofs introduces a shared filesystem daemon (virtiofsd) that must be treated
as security-sensitive: it mediates guest filesystem requests to host storage.

virtiofsd is designed to reduce “escape” risks using:

* seccomp syscall filtering,
* capability dropping, and
* sandbox modes (e.g. “namespace” mode uses ``pivot_root`` so the shared tree
  becomes the daemon’s root).

However, virtiofsd and its configuration remain additional surface area; historic
bugs have existed that allowed a privileged guest to leverage the shared directory
to access host devices.

References:
* virtiofsd security/sandbox behavior: `virtiofsd documentation`_
* libvirt virtiofs config (sandbox mode, idmap, readonly): `libvirt Domain XML (filesystems/virtiofs)`_
* Example virtiofsd privilege-escalation class issue (CVE-2020-35517): `CVE-2020-35517 record`_

4) Microarchitectural side channels (cross-VM or guest→host leakage)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Even without “code execution on the host,” modern CPUs have had speculative-
execution vulnerabilities that can allow information leakage across isolation
boundaries (including VM boundaries), depending on CPU model and mitigation state.

References:
* Linux kernel documentation for L1TF/L1 Terminal Fault: `Linux kernel doc: L1TF (CVE-2018-3646 class)`_
* Ubuntu vulnerability note for L1TF (virtualization impact): `Ubuntu: L1TF vulnerability page`_

5) Denial of Service (DoS) against the host
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

A malicious guest may attempt to degrade host availability via CPU, memory,
disk, or network exhaustion (fork bombs, memory pressure, disk fill, network
flooding). These are often easier than escapes.

Mitigations typically involve cgroups/resource limits and sensible disk sizing.

References:
* QEMU security guide notes cgroups/resource limits as a control: `QEMU Security`_


Historical examples (selected)
------------------------------

This section exists to justify a cautious stance: VM escapes and boundary bugs
have happened repeatedly across hypervisors and device stacks.

Examples relevant to KVM/QEMU/libvirt-style deployments:

* **VENOM / QEMU FDC (CVE-2015-3456)**: a guest-triggerable bug in QEMU’s floppy
  disk controller emulation, widely referenced as a “guest to host” risk class.
  (`CVE-2015-3456 (NVD)`_, `Red Hat VENOM advisory`_)

* **vhost-net guest→host kernel escape during migration (CVE-2019-14835)**:
  a kernel vhost-net issue demonstrating the risk of kernel backends in some
  configurations. (`Red Hat vhost-net CVE-2019-14835`_)

* **KVM breakout not relying on QEMU userspace (CVE-2021-29657)**: a KVM/AMD
  vulnerability writeup and exploitation discussion. (`Project Zero: EPYC escape (CVE-2021-29657)`_,
  `CVE-2021-29657 (NVD)`_)

* **QEMU virtio-net use-after-free (CVE-2021-3748)**: a virtio device bug that
  could allow a malicious guest to crash QEMU or potentially execute code in the
  QEMU process context depending on conditions and versions.
  (`CVE-2021-3748 (NVD)`_)

* **virtiofsd host escalation via device special files (CVE-2020-35517)**:
  illustrates why virtiofs shares must be treated as a security boundary extension.
  (`CVE-2020-35517 record`_)

* **virglrenderer / 3D acceleration guest→host escape (CVE-2019-18389)**:
  highlights why enabling graphics/3D acceleration increases surface area.
  (``aivm`` defaults should avoid graphics/3D for untrusted guests.)
  (`Ubuntu: CVE-2019-18389`_)

These are *examples*, not an exhaustive list.


Design decisions and recommended hardening options
--------------------------------------------------

This section links configuration choices to concrete security outcomes.

Default device minimization
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Recommendation: keep the VM as “boring” as possible.

* Avoid graphics/3D acceleration, SPICE/VNC, USB passthrough, and other complex
  device stacks for untrusted guests.
* Each additional virtual device is another parser/emulation surface exposed
  to hostile guest input (per QEMU’s own security guide).

References:
* QEMU device emulation and sensitive configuration discussion: `QEMU Security`_

Networking: choose userspace backend when isolation matters more than performance
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

For virtio NICs, prefer userspace backend in high-risk scenarios:

* High-isolation mode: set interface driver backend to ``name='qemu'`` (userspace).
* Default/performance mode: allow libvirt default (often ``vhost`` if available).

Rationale: kernel backends (e.g. vhost-net) can increase kernel attack surface;
userspace backends can reduce the “host kernel” component of the guest-facing path
at the cost of performance.

References:
* libvirt: interface backend driver semantics and defaulting behavior:
  `libvirt Domain XML (interface driver backend)`_

Filesystem sharing: treat virtiofs as an explicit trust extension
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Policy recommendations:

* Make host folder sharing **opt-in** and visibly signaled to operators.
* Support a per-share **read-only** mode where available and functional.
* Prefer configuring virtiofsd with **namespace sandboxing**.
* Prefer **id-mapping** so guest IDs map to unprivileged host IDs where supported
  (reduces risk of UID/GID confusion and limits host-side impact).

References:
* libvirt virtiofs guide (overview + constraints): `libvirt virtiofs guide`_
* libvirt domain XML example showing virtiofs sandbox + idmap + filesystem readonly:
  `libvirt Domain XML (filesystems/virtiofs)`_
* virtiofsd sandbox/security mechanisms: `virtiofsd documentation`_

Host confinement: reduce blast radius if QEMU/virtiofsd is compromised
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

Even with good device choices, defense-in-depth matters:

* Ensure QEMU processes run confined where possible (AppArmor/SELinux “sVirt”).
* Use QEMU seccomp sandboxing and namespaces when supported by the stack.
* Apply resource limits (cgroups) to mitigate DoS.

References:
* QEMU security guide (SELinux/AppArmor, namespaces, seccomp, cgroups): `QEMU Security`_
* libvirt QEMU driver security topics (sVirt/AppArmor/SELinux concepts):
  `libvirt QEMU driver`_

Firewall isolation: fail closed for “I expected isolation” workflows
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

If ``aivm`` offers a mode that claims to isolate the guest from common private
address ranges, it should:

* validate that rules are applied before launching the guest’s “online” workflows,
* warn loudly and/or abort on failure, and
* provide a documented escape hatch for users who knowingly accept risk.

Optional references:
* libvirt NWFilter overview as a complementary approach: `libvirt NWFilter`_


Operational profiles (recommended defaults)
-------------------------------------------

High Isolation Mode (for unknown/untrusted workloads)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* No shared folders (virtiofs disabled).
* Userspace NIC backend (``driver name='qemu'``) to avoid vhost-net kernel path.
* Strict SSH host key handling; no ``StrictHostKeyChecking=no`` style probing.
* Tight CPU/RAM/disk limits; disposable VM image.
* Strong host confinement (SELinux/AppArmor sVirt; QEMU seccomp/namespaces).

Balanced Dev Mode (default developer productivity)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Minimal shared folder(s), narrow project subtree only.
* Writable share only when required; warn on risky paths.
* Default libvirt networking, plus firewall isolation if available.
* Host and virtualization stack patched.

Convenience Mode (explicitly less safe)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

* Broad writable shares (including home directories) permitted with extra warnings.
* Fewer restrictions on networking and provisioning.
* Not appropriate for untrusted code.

References:
* libvirt driver and domain XML references above for the underlying knobs.


Use of SSH keys
---------------

What ``aivm`` does:

* Reads configured key *paths* to run local ``ssh/scp`` commands.
* Injects the configured public key into cloud-init for guest access.
* Stores identity/public key *paths* in config, not private key contents.

What ``aivm`` does not do:

* Copy private key material into the VM.
* Intentionally persist private key contents in the config store.

Operator guidance:

* Do not use SSH agent forwarding into the VM.
* Keep per-VM ``known_hosts`` files; avoid disabling host key checking.

(See also QEMU security notes about “sensitive configuration” patterns when
management channels are overly powerful.)

References:
* QEMU security guide (sensitive management interfaces): `QEMU Security`_


Host package installation and supply chain
------------------------------------------

``aivm host install_deps`` may install host packages (e.g. libvirt, qemu, nftables,
SSH client tools).

Risks introduced:

* Expanded host attack surface from additional privileged services/binaries.
* Supply-chain and package trust risk inherited from host repositories.

Operational expectation:

* Treat host patch cadence and repository trust as part of the security posture.

References:
* libvirt QEMU driver overview and deployment prerequisites: `libvirt QEMU driver`_


Operator checklist
------------------

For “malicious guest” scenarios, minimum recommended operator practices:

* Keep host kernel + qemu + libvirt updated.
* Prefer “High Isolation Mode” for unknown workloads.
* Share only minimal project subtrees; keep secrets outside shared trees.
* Avoid graphics/3D acceleration; avoid passthrough devices unless required.
* Prefer userspace NIC backend for high-risk guests.
* Ensure host confinement (SELinux/AppArmor) is enabled where available.
* Apply resource limits to mitigate DoS.
* Treat shared content as untrusted: do not auto-execute artifacts from shares.


References
----------

.. _QEMU Security: https://www.qemu.org/docs/master/system/security.html

.. _libvirt QEMU driver: https://libvirt.org/drvqemu.html

.. _libvirt virtiofs guide: https://libvirt.org/kbase/virtiofs.html

.. _libvirt Domain XML (filesystems/virtiofs): https://libvirt.org/formatdomain.html#filesystems

.. _libvirt Domain XML (interface driver backend): https://libvirt.org/formatdomain.html#setting-nic-driver-specific-options

.. _libvirt NWFilter: https://libvirt.org/formatnwfilter.html

.. _virtiofsd documentation: https://qemu.weilnetz.de/doc/7.1/tools/virtiofsd.html

.. _CVE-2015-3456 (NVD): https://nvd.nist.gov/vuln/detail/CVE-2015-3456
.. _Red Hat VENOM advisory: https://access.redhat.com/articles/1444903

.. _Red Hat vhost-net CVE-2019-14835: https://access.redhat.com/security/vulnerabilities/kernel-vhost

.. _Project Zero: EPYC escape (CVE-2021-29657): https://projectzero.google/2021/06/an-epyc-escape-case-study-of-kvm.html
.. _CVE-2021-29657 (NVD): https://nvd.nist.gov/vuln/detail/CVE-2021-29657

.. _CVE-2021-3748 (NVD): https://nvd.nist.gov/vuln/detail/CVE-2021-3748

.. _CVE-2020-35517 record: https://www.cve.org/CVERecord?id=CVE-2020-35517

.. _Linux kernel doc: L1TF (CVE-2018-3646 class): https://www.kernel.org/doc/html/latest/admin-guide/hw-vuln/l1tf.html
.. _Ubuntu: L1TF vulnerability page: https://ubuntu.com/security/vulnerabilities/l1tf

.. _Ubuntu: CVE-2019-18389: https://ubuntu.com/security/CVE-2019-18389
