# Canonical consolidation order

1. Preserve backend blocking behavior while allowing complete trusted Android
   validation to supersede untrusted sandbox failures.
2. Persist Android `base_branch=master` atomically at planning completion.
3. Track the proven protected release helper, validator, namespace probe,
   sudoers rule, and systemd fragments.
4. Preserve separate implementation and deployment Gradle caches and prove
   both offline after dependency or build-logic changes.
5. Preserve `ProtectSystem=strict`; reopen only the exact deployment counter
   parent and guard it with the privileged namespace probe.
6. Verify database/APK identity locally and the unauthenticated Bearer boundary
   separately from Android-client authenticated installation proof.
7. Build and test in disposable workspaces; never leave ignored build residue
   in the canonical Android checkout.
8. Capture evidence before final assertions and include disk/inode/retention
   preflight in the installation/promotion checkpoint.
9. Promote through the protected backend deployment pipeline.
10. Rebuild installed runtime/assets from the promoted tracked source, prove
    equivalence, and only then remove temporary overrides.
