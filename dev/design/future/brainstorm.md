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
2026-03-23 14:32:28.884 | INFO     | aivm.commands:__exit__:361 - RUN [1/2]: sudo nft delete table inet aivm_sandbox (submitted_by=aivm.firewall:apply_firewall:175)
2026-03-23 14:32:28.904 | INFO     | aivm.commands:__exit__:361 - RUN [2/2]: sudo nft -f - (submitted_by=aivm.firewall:apply_firewall:183)
2026-03-23 14:32:28.950 | INFO     | aivm.firewall:apply_firewall:192 - Firewall rules applied (table=inet aivm_sandbox).
2026-03-23 14:32:28.950 | DEBUG    | aivm.vm.lifecycle:create_or_start_vm:878 - Creating or starting VM aivm-2404
2026-03-23 14:32:28.950 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh dominfo aivm-2404 (submitted_by=aivm.vm.lifecycle:_vm_defined:120)
2026-03-23 14:32:28.977 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh domstate aivm-2404 (submitted_by=aivm.vm.lifecycle:create_or_start_vm:884)
2026-03-23 14:32:29.004 | INFO     | aivm.commands:flush_through:786 - About to run privileged state-changing host operations via sudo:
2026-03-23 14:32:29.004 | INFO     | aivm.commands:flush_through:786 -   Create/start VM 'aivm-2404' or update VM definition.
Continue? [y]es/[a]ll/[N]o: y
2026-03-23 14:32:50.516 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh start aivm-2404 (submitted_by=aivm.vm.lifecycle:create_or_start_vm:900)
2026-03-23 14:32:51.741 | INFO     | aivm.vm.lifecycle:create_or_start_vm:906 - VM started: aivm-2404
2026-03-23 14:32:51.742 | DEBUG    | aivm.commands:submit:757 - RUN: virsh -c qemu:///system domstate aivm-2404 (submitted_by=aivm.cli.vm:_probe_vm_running_nonsudo:2977)
2026-03-23 14:32:51.765 | DEBUG    | aivm.commands:result:200 - RUN: virsh -c qemu:///system dumpxml aivm-2404 (submitted_by=aivm.vm.share:_dumpxml_text:166)
2026-03-23 14:32:51.790 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:32:51.790 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 true (submitted_by=aivm.status:probe_ssh_ready:258)
2026-03-23 14:32:54.796 | DEBUG    | aivm.vm.lifecycle:wait_for_ip:1065 - Waiting for VM IP via DHCP lease
2026-03-23 14:32:54.797 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh domiflist aivm-2404 (submitted_by=aivm.vm.lifecycle:_mac_for_vm:1035)
2026-03-23 14:32:54.826 | INFO     | aivm.vm.lifecycle:wait_for_ip:1080 - Using cached IP as fallback while waiting for lease discovery: 10.77.0.195
2026-03-23 14:32:54.826 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh net-dhcp-leases aivm-net (submitted_by=aivm.vm.lifecycle:wait_for_ip:1096)
2026-03-23 14:32:54.853 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh domifaddr aivm-2404 (submitted_by=aivm.vm.lifecycle:wait_for_ip:1113)
2026-03-23 14:32:54.881 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 true (submitted_by=aivm.vm.lifecycle:wait_for_ip:1135)
2026-03-23 14:32:57.887 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh domstate aivm-2404 (submitted_by=aivm.vm.lifecycle:wait_for_ip:1162)
2026-03-23 14:32:57.915 | INFO     | aivm.vm.lifecycle:wait_for_ip:1184 - Waiting for VM network: vm=aivm-2404 elapsed=3s state=running leases_seen=0 domifaddr_ipv4_rows=0 mac=52:54:00:13:83:bf
2026-03-23 14:32:59.916 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh net-dhcp-leases aivm-net (submitted_by=aivm.vm.lifecycle:wait_for_ip:1096)
2026-03-23 14:32:59.949 | INFO     | aivm.commands:submit:757 - RUN: sudo virsh domifaddr aivm-2404 (submitted_by=aivm.vm.lifecycle:wait_for_ip:1113)
2026-03-23 14:32:59.977 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 true (submitted_by=aivm.vm.lifecycle:wait_for_ip:1135)
2026-03-23 14:33:01.675 | INFO     | aivm.vm.lifecycle:wait_for_ip:1152 - Writing VM IP cache to /home/joncrall/.cache/aivm/aivm-2404/aivm-2404.ip
2026-03-23 14:33:01.675 | INFO     | aivm.vm.lifecycle:wait_for_ip:1154 - VM reachable via cached IP fallback: 10.77.0.195 (saved to /home/joncrall/.cache/aivm/aivm-2404/aivm-2404.ip)
2026-03-23 14:33:01.675 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=3 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 true (submitted_by=aivm.vm.lifecycle:wait_for_ssh:1291)
2026-03-23 14:33:01.892 | INFO     | aivm.vm.lifecycle:wait_for_ssh:1299 - SSH is ready on 10.77.0.195
2026-03-23 14:33:01.892 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/aivm; if mountpoint -q /home/joncrall/code/aivm; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/aivm 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/aivm ;; esac; else sudo -n mount -t virtiofs hostcode-aivm /home/joncrall/code/aivm; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:02.136 | DEBUG    | aivm.commands:result:200 - RUN: virsh -c qemu:///system dumpxml aivm-2404 (submitted_by=aivm.vm.share:_dumpxml_text:166)
2026-03-23 14:33:02.159 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /data/crfm-helm-public; if mountpoint -q /data/crfm-helm-public; then opts="$(findmnt -n -o OPTIONS --target /data/crfm-helm-public 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /data/crfm-helm-public ;; esac; else sudo -n mount -t virtiofs hostcode-crfm-helm-public /data/crfm-helm-public; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:02.388 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:02.388 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_dvc
2026-03-23 14:33:02.388 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_dvc
2026-03-23 14:33:02.388 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_dvc (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:02.395 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/data/joncrall/dvc-repos/shitspotter_dvc guest_dst=/data/joncrall/dvc-repos/shitspotter_dvc token=hostcode-shitspotter_dvc detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_dvc, expected_source=/data/joncrall/dvc-repos/shitspotter_dvc, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:02.395 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_expt_dvc
2026-03-23 14:33:02.395 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_expt_dvc
2026-03-23 14:33:02.395 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_expt_dvc (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:02.402 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/data/joncrall/dvc-repos/shitspotter_expt_dvc guest_dst=/data/joncrall/dvc-repos/shitspotter_expt_dvc token=hostcode-shitspotter_expt_dvc detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-shitspotter_expt_dvc, expected_source=/data/joncrall/dvc-repos/shitspotter_expt_dvc, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:02.402 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/aiq-magnet; if mountpoint -q /home/joncrall/code/aiq-magnet; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/aiq-magnet 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/aiq-magnet ;; esac; else sudo -n mount -t virtiofs hostcode-aiq-magnet /home/joncrall/code/aiq-magnet; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:02.626 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:02.626 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/every_eval_ever; if mountpoint -q /home/joncrall/code/every_eval_ever; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/every_eval_ever 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/every_eval_ever ;; esac; else sudo -n mount -t virtiofs hostcode-every_eval_ever /home/joncrall/code/every_eval_ever; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:02.852 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:02.853 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-geowatch
2026-03-23 14:33:02.853 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-geowatch
2026-03-23 14:33:02.853 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-geowatch (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:02.859 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/geowatch guest_dst=/home/joncrall/code/geowatch token=hostcode-geowatch detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-geowatch, expected_source=/home/joncrall/code/geowatch, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:02.859 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-labelme
2026-03-23 14:33:02.859 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-labelme
2026-03-23 14:33:02.859 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-labelme (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:02.865 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/labelme guest_dst=/home/joncrall/code/labelme token=hostcode-labelme detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-labelme, expected_source=/home/joncrall/code/labelme, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:02.865 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-line_profiler
2026-03-23 14:33:02.865 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-line_profiler
2026-03-23 14:33:02.865 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-line_profiler (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:02.871 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/line_profiler guest_dst=/home/joncrall/code/line_profiler token=hostcode-line_profiler detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-line_profiler, expected_source=/home/joncrall/code/line_profiler, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:02.871 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/networkx_algo_common_subtree; if mountpoint -q /home/joncrall/code/networkx_algo_common_subtree; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/networkx_algo_common_subtree 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/networkx_algo_common_subtree ;; esac; else sudo -n mount -t virtiofs hostcode-networkx_algo_common_subtre /home/joncrall/code/networkx_algo_common_subtree; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:03.101 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:03.101 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/operadic_consistency; if mountpoint -q /home/joncrall/code/operadic_consistency; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/operadic_consistency 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/operadic_consistency ;; esac; else sudo -n mount -t virtiofs hostcode-operadic_consistency /home/joncrall/code/operadic_consistency; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:03.337 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:03.337 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-paper-g1-and-mcc
2026-03-23 14:33:03.338 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-paper-g1-and-mcc
2026-03-23 14:33:03.338 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-paper-g1-and-mcc (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:03.343 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/paper-g1-and-mcc guest_dst=/home/joncrall/code/paper-g1-and-mcc token=hostcode-paper-g1-and-mcc detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-paper-g1-and-mcc, expected_source=/home/joncrall/code/paper-g1-and-mcc, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:03.344 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-mcc-proof
2026-03-23 14:33:03.344 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-mcc-proof
2026-03-23 14:33:03.344 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-mcc-proof (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:03.350 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/paper-g1-and-mcc/mcc-proof guest_dst=/home/joncrall/code/paper-g1-and-mcc/mcc-proof token=hostcode-mcc-proof detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-mcc-proof, expected_source=/home/joncrall/code/paper-g1-and-mcc/mcc-proof, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:03.351 | DEBUG    | aivm.commands:submit:757 - RUN: ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new -i /home/joncrall/.ssh/id_crallvision_ed25519 agent@10.77.0.195 'set -euo pipefail; sudo -n mkdir -p /home/joncrall/code/scriptconfig; if mountpoint -q /home/joncrall/code/scriptconfig; then opts="$(findmnt -n -o OPTIONS --target /home/joncrall/code/scriptconfig 2>/dev/null || true)"; case ",$opts," in *,rw,*) : ;; *) sudo -n mount -o remount,rw /home/joncrall/code/scriptconfig ;; esac; else sudo -n mount -t virtiofs hostcode-scriptconfig /home/joncrall/code/scriptconfig; fi' (submitted_by=aivm.vm.share:ensure_share_mounted:456)
2026-03-23 14:33:03.573 | INFO     | aivm.store:save_store:283 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-23 14:33:03.573 | INFO     | aivm.commands:flush_through:782 - Step: Inspect shared-root host bind state
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 - Submitted by: aivm.cli.vm:_ensure_shared_root_host_bind:2078
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 - Context: Attach and reconcile shared-root mapping
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 - Why: Determine whether the VM-specific bind target already points at the requested host folder.
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 - Planned commands: 1
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 -   1. Inspect current source for host bind target
2026-03-23 14:33:03.574 | INFO     | aivm.commands:flush_through:782 -      command: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-xcookie
2026-03-23 14:33:03.574 | DEBUG    | aivm.commands:flush_through:782 -      detail: target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-xcookie
2026-03-23 14:33:03.574 | INFO     | aivm.commands:result:200 - RUN [1/1]: sudo findmnt -n -o SOURCE --target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-xcookie (submitted_by=aivm.cli.vm:_ensure_shared_root_host_bind:2084)
2026-03-23 14:33:03.580 | WARNING  | aivm.cli.vm:_restore_saved_vm_attachments:2858 - Skipping saved shared-root attachment restore for VM aivm-2404 to avoid disrupting an active mount: source=/home/joncrall/code/xcookie guest_dst=/home/joncrall/code/xcookie token=hostcode-xcookie detail=Refusing to replace existing shared-root host bind mount during automatic restore (target=/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-xcookie, expected_source=/home/joncrall/code/xcookie, actual_source=/dev/nvme2n1p3). Use an explicit attach/detach command to reconcile this mount.
2026-03-23 14:33:03.580 | INFO     | aivm.cli.vm:_restore_saved_vm_attachments:2945 - Restored 6 saved attachment(s) for VM aivm-2404
2026-03-23 14:33:03.581 | DEBUG    | aivm.cli.vm:_upsert_ssh_config_entry:2539 - SSH config entry for host 'aivm-2404' already up to date in /home/joncrall/.ssh/config
2026-03-23 14:33:03.581 | INFO     | aivm.commands:submit:757 - RUN: code --remote ssh-remote+aivm-2404 /home/joncrall/code/aivm (submitted_by=aivm.cli.vm:main:674)
Opened VS Code remote folder /home/joncrall/code/aivm on host aivm-2404
Folder registered in /home/joncrall/.config/aivm/config.toml


Especially on the first prompt, I don't think this clearly gives an indication
of what the intent is. We have determined that we found config drift, and we
intend to fix it, so we should mark that in the appropriate location and ensure
the code is structured so this is expressed naturally.



