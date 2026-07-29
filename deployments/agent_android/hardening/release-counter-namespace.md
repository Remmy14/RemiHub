# Permanent release-counter namespace hardening

The canonical Android deployment service definition must include:

```ini
[Service]
ReadWritePaths=/opt/remihub/deployments
ExecStartPre=+/usr/local/libexec/remihub-android-release-counter-namespace-probe
```

The probe must be installed root-owned, non-writable by the deployment user,
and must:

- verify the counter is a non-symlink regular file;
- hash the counter before and after;
- atomically create, fsync, and delete a temporary file in the parent directory;
- fsync the parent directory;
- prove the counter content did not change.

Permanent integration tests must run the probe under:

1. `ProtectSystem=strict` without `ReadWritePaths`, expecting EROFS;
2. the final service namespace with the exact write exception, expecting pass;
3. the unprivileged deployment identity outside elevation, expecting permission
   denial.

Do not replace this with `ProtectSystem=false` or a broad writable `/opt`.
