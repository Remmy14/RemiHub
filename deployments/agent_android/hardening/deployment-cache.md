# Permanent deployment-cache hardening

The installer/readiness workflow must:

1. Treat implementation and deployment Gradle caches as separate protected artifacts.
2. Hydrate both from the exact canonical Android commit and complete validator task set.
3. Run both installed validators offline with diagnostic UUIDs.
4. Require success, denied networking, offline Gradle, unchanged protected build files, expected package identity, and unsigned release APK evidence.
5. Record cache path, owner, mode, service identity, validator hash, Gradle tasks, hydration log hash, and offline proof hash.
6. Fail readiness if either cache lacks a dependency required by the exact canonical build.
7. Re-run readiness after dependency or build-logic changes.

## Failpoint-aware readiness

Cache-maintenance tooling must understand the release failpoint lifecycle. Before reservation it may be `armed`; after a successful reservation it must be `sealed` and fully bound to that reservation. Only a deployment that reaches the configured post-promotion boundary may mark it `consumed`.


## Content-addressed cache snapshots

Do not validate cache backups with allocated-size comparisons. Generate and
retain deterministic source and snapshot tree manifests, require manifest
equality before mutation, and use the same protected manifest to verify a
rollback copy before atomic replacement.
