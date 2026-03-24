This document is for recording thoughts I had while thinking of features I want.


* Make it easy to add devices like GPUs or USB to the VMs

* Separate "state" from "config": 

  Probably separate the config file so the config only impacts the defaults and
  the current VM state is stored in a cache. It might even be a nice idea if we
  could "bootstrap" the state by inspecting what vms exist with virsh.

* Better provisioning

* Fix the setting sync code.

* Revisit what the default log level should be and what is logged to INFO or DEBUG or print.

* The "shared" vs "shared-root" modes have bad names. We should fix them.

* Can we detect that resources were increased and adjust as needed. (Trying to add this to aivm status, we already have some drift detectors).

* Definitely need to be able to shutdown the VMs.

* Need to fix the failure when you manually pause the VM. aivm doesn't recognize that it's not running when you try to ssh into it.


* TODO: Currently I don't see auto_approve_readonly_sudo available as a
general command line option, and maybe it should be? Perhaps the
behavior items are always overridable via the command line?

* TODO: I'm also noticing that we are detecting ssh identity when
running `aivm code .` and the vm has already been configured, so
we should already know what the identity is


* Starting from a reboot state my prompts are:

(uvpy3.13.2) joncrall@toothbrush:~/code/aivm$ aivm code . -vv
2026-03-23 14:29:28.160 | DEBUG    | aivm.cli._common:_setup_logging:184 - Logging configured at DEBUG (effective_verbosity=2, colorize=True)
2026-03-23 14:29:28.162 | DEBUG    | aivm.detect:detect_ssh_identity:71 - detecting detect_ssh_identity
2026-03-23 14:29:28.174 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 true (submitted_by=aivm.status:probe_ssh_ready:258)
2026-03-23 14:29:31.179 | DEBUG    | aivm.commands:submit:757 - RUN: virsh -c qemu:///system domstate aivm-2404 (submitted_by=aivm.cli.vm:_probe_vm_running_nonsudo:2977)
2026-03-23 14:29:31.202 | DEBUG    | aivm.commands:submit:757 - RUN: virsh -c qemu:///system net-info aivm-net (submitted_by=aivm.status:probe_network:141)
2026-03-23 14:29:31.225 | DEBUG    | aivm.commands:submit:757 - RUN: nft list table inet aivm_sandbox (submitted_by=aivm.status:probe_firewall:183)
2026-03-23 14:29:31.226 | INFO     | aivm.commands:submit:757 - RUN: sudo nft list table inet aivm_sandbox (submitted_by=aivm.status:probe_firewall:183)
2026-03-23 14:29:31.232 | DEBUG    | aivm.firewall:apply_firewall:147 - Applying nftables firewall rules
2026-03-23 14:29:31.232 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh -c qemu:///system net-dumpxml aivm-net (submitted_by=aivm.firewall:_effective_bridge_and_gateway:44)
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 - Step: Replace nftables rules for managed VM bridge
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 - Submitted by: aivm.firewall:apply_firewall:166
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 - Context: Apply firewall table aivm_sandbox
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 - Why: Clear the previous managed nftables table if present, then load the freshly rendered ruleset.
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 - Planned commands: 2
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 -   1. Remove previous nftables table inet aivm_sandbox if present
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 -      command: sudo nft delete table inet aivm_sandbox
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 -   2. Load rendered nftables rules into inet aivm_sandbox
2026-03-23 14:29:31.260 | INFO     | aivm.commands:finish_plan:613 -      command: sudo nft -f -
Approve this step? [y]es/[a]ll/[s]how/[N]o: y


Especially on the first prompt, I don't think this clearly gives an indication
of what the intent is. We have determined that we found config drift, and we
intend to fix it, so we should mark that in the appropriate location and ensure
the code is structured so this is expressed naturally.




* Maybe the intent context manager has a way to specify what it thinks the role
  is likely to be, or even have it guarantee that a role will be read-only or
  that sudo will not be necessary, and that can prevent us from using
  privileged commands in some sections.
