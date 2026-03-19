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
