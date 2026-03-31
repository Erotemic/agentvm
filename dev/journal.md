# 2026-03-30

I'm noticing that when I run `aivm detach .`, the detach might fail, but we seem to ignore that and proceed as if the command worked fine. 

(uvpy3.13.2) joncrall@toothbrush:/data/crfm-helm-audit$ aivm detach .
2026-03-30 13:11:09.210 | INFO     | aivm.cli.vm:_detach_shared_root_host_bind:2203 - RUN: sudo mountpoint -q /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit
2026-03-30 13:11:09.216 | INFO     | aivm.commands:_render_sudo_prompt_context:845 - About to request sudo for state-changing host operations:
2026-03-30 13:11:09.216 | INFO     | aivm.commands:_render_sudo_prompt_context:849 -   Unmount shared-root bind target /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit
2026-03-30 13:11:09.216 | INFO     | aivm.commands:_render_sudo_prompt_context:851 -   Planned sudo commands:
2026-03-30 13:11:09.216 | INFO     | aivm.commands:_render_sudo_prompt_context:854 -     1. sudo umount /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit
Continue? [y]es/[a]ll/[N]o: y
2026-03-30 13:11:10.311 | INFO     | aivm.cli.vm:_detach_shared_root_host_bind:2214 - RUN: sudo umount /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit
2026-03-30 13:11:10.317 | ERROR    | aivm.cli.vm:_detach_shared_root_host_bind:2214 - Command failed code=32 cmd=sudo umount /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit stderr=umount: /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit: target is busy. stdout=
2026-03-30 13:11:10.317 | WARNING  | aivm.cli.vm:main:1113 - Could not detach shared-root host bind mount for VM aivm-2404 source=/data/crfm-helm-audit guest_dst=/data/crfm-helm-audit token=hostcode-crfm-helm-audit: Command failed (code=32): ['sudo', 'umount', '/var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit']
umount: /var/lib/libvirt/aivm/aivm-2404/shared-root/hostcode-crfm-helm-audit: target is busy.
2026-03-30 13:11:10.318 | INFO     | aivm.store:save_store:272 - Writing config store to /home/joncrall/.config/aivm/config.toml
2026-03-30 13:11:10.318 | INFO     | aivm.store:save_store:274 -   Reason: Remove attachment record for /data/crfm-helm-audit from VM aivm-2404.
Detached /data/crfm-helm-audit from VM aivm-2404 (shared-root mode)
Detached shared-root guest bind mount.
Updated config store: /home/joncrall/.config/aivm/config.toml

