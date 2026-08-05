# Permanent release-counter namespace hardening

The live Android release counter is operational state, not tracked source. It
must live at:

```text
/var/lib/remihub-agent/android-release-counter/release_version.json
```

The directory must be `root:storage` mode `0750`; the counter must be
`alex:storage` mode `0640`. The canonical backend checkout must remain clean.

The canonical Android deployment service definition must include:

```ini
[Service]
ReadWritePaths=/var/lib/remihub-agent/android-release-counter
ExecStartPre=+/usr/local/libexec/remihub-android-release-counter-namespace-probe
```

The probe must be installed root-owned, non-writable by the deployment user,
and must:

- verify the counter is a non-symlink regular file with exact ownership/mode;
- hash the counter before and after;
- atomically create, fsync, and delete a temporary file in the state directory;
- fsync the state directory;
- prove the counter content did not change.

Permanent integration tests must run the probe under:

1. `ProtectSystem=strict` without `ReadWritePaths`, expecting EROFS;
2. the final service namespace with the exact state-directory exception, expecting pass;
3. the unprivileged deployment identity outside elevation, expecting permission denial.

Do not place the mutable counter back in `/opt/remihub`, replace this with
`ProtectSystem=false`, or grant a broad writable `/opt`.
