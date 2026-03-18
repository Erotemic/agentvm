* It might be a good idea to distinguish read-only sudo commands versus state modification sudo commands. If we say "yes" to a sudo command perhaps we have a behavior policy that by default says yes to the rest of the read-only sudo commands. If the policy is super-strict we use the current ask every time behavior, but the default should be a yes means continue to ask for commands that will modify some state, but if are just doing a sudo query log it as we currently do, but don't prompt the user, just run the query.

The way we tag the names of the folders that we attach might need to be reworked.

We need to add something to the AGENT file to let it know when and when not to care about backwards compatibility. If we are inside a feature branch or dev, we don't need to be backwards compatible with the current feature, only the last released version matters.
