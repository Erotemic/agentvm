* It might be a good idea to distinguish read-only sudo commands versus state modification sudo commands. If we say "yes" to a sudo command perhaps we have a behavior policy that by default says yes to the rest of the read-only sudo commands. If the policy is super-strict we use the current ask every time behavior, but the default should be a yes means continue to ask for commands that will modify some state, but if are just doing a sudo query log it as we currently do, but don't prompt the user, just run the query.

The way we tag the names of the folders that we attach might need to be reworked.

We need to add something to the AGENT file to let it know when and when not to care about backwards compatibility. If we are inside a feature branch or dev, we don't need to be backwards compatible with the current feature, only the last released version matters.


NOTE: the reattach of mounted drives is still broken, it seemed fixed for a
bit, but it is certainly still there. Confirmed, even when aivm does the VM
startup the issue still exists. maybe it was fixed in
ae261eba4505d06a99d70b2e7bf7a8f61f6ae8fa but then the claude changes broke it?

We may have fixed this now somewhat.

----


I’m checking the test harness and config-path logic in the repo to see whether tests are intentionally hitting your real ~/.config/aivm/config.toml or whether cleanup is just missing.

Thought for 2m 31s

Yes — almost certainly.

aivm uses a single global config store for defaults, VMs, networks, and attachments, and the README explicitly says the global store is ~/.config/aivm/config.toml. In the code, store_path() resolves to .../config.toml, and save_store() writes to path or store_path(), so anything that saves without an explicit config path hits the default global store.

Given that your leftover attachment entries point at /tmp/pytest-of-joncrall/..., the tests were clearly creating temporary project directories under pytest, but persisting attachment metadata into the normal store instead of an isolated test store. That means the tests are either calling the store layer directly without a temp path, or more likely invoking CLI flows that eventually call save_store(...) with the default config path. The detach path you were patching is one example of a CLI path that writes back to the config store.

So I would treat this as a test isolation bug. The clean fix is to make tests force an isolated config location every time, for example by:

passing --config <tmp_path>/config.toml to CLI tests,
or monkeypatching the app config base (HOME / XDG_CONFIG_HOME, depending on what ub.Path.appdir() honors on your platform) so store_path() resolves inside tmp_path,
and ideally adding an autouse fixture that fails if a test ever touches the real ~/.config/aivm/config.toml.

A practical pattern for pytest would be:

@pytest.fixture
def isolated_aivm_config(tmp_path, monkeypatch):
    cfg_root = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_root))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    return cfg_root / "aivm" / "config.toml"

and then make every CLI test pass --config explicitly anyway. Belt and suspenders is worth it here.

To clean the current clutter from your real store, remove any [[attachments]] entries whose host_path starts with /tmp/pytest-of-joncrall/. They are test artifacts, not real long-lived attachments.

I couldn’t directly open the specific test files in GitHub browse mode from here, so I can’t point to the exact fixture yet. But from the global-store codepath plus the pytest temp paths in your config, the answer is effectively yes


We should be careful when we request attaching nested directories like:

/home/joncrall/code/paper-g1-and-mcc/mcc-proof

and then attaching

/home/joncrall/code/paper-g1-and-mcc
