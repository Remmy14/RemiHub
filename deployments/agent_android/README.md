# Android Agent deployment assets

This directory is the canonical tracked source for the protected Android Agent
deployment boundary proven by the first real Android-only card.

## Tracked assets

- `libexec/remihub-android-release-control`: root-only signing, publication,
  activation, source promotion, verification, and rollback helper.
- `libexec/remihub-android-release-validator`: network-denied offline Gradle
  validator for the exact deployment candidate.
- `libexec/remihub-android-release-counter-namespace-probe`: privileged
  `ProtectSystem=strict` write-namespace probe for the release counter parent.
- `sudoers/remihub-android-release`: the single fixed helper elevation boundary.
- `systemd/30-runtime-working-directory.conf.in`: generated immutable runtime
  selection template.
- `systemd/40-release-counter-namespace.conf`: the exact narrow namespace
  exception and privileged preflight.
- `hardening/`: permanent requirements and production lessons.

The canonical Android checkout remains source-only. Build and validation output
belongs in disposable implementation/deployment workspaces with separate
Gradle caches.

The protected helper verifies the active database release and published APK
locally. It verifies that unauthenticated update routes remain protected by a
Bearer challenge; authenticated installation proof remains the Android
client's responsibility.
