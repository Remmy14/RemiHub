# Backend implementation sandbox source

This directory establishes the maintained source for the implementation sandbox
at `/opt/remihub-agent/validators/backend_validation_sandbox.py`. It belongs to
the same protected infrastructure tree as the existing QA/production validation
helper; these are two separate launchers. Do not replace one with the other.

Provenance: the November 12 scope-validator operator package installed the
Python launcher with SHA-256
`ae5aa1e84c020c24d3980218ebf04799493cb989fa5b9f6b2fe5d089c652857d`.
The November 15 repair added a namespace-local `/etc` and synthetic passwd/group
entries, allowing PostgreSQL to resolve its effective UID without mounting host
accounts. Its exact tested and installed bytes are now maintained here:
`1580e3ff3120bb9a13d4ff725bb4a74d5b7414ecaca4072b42219d791cf1eee8`.
All other launcher behavior is preserved. This directory does not adopt the
other independently installed validators or grant application deployment access.

Before this adoption the launcher existed only in untracked development and
operator-staged sources. No tracked application source published this file.
The historical scope-validator ACTIVATION.md performs the actual install; it is
historical evidence and must not be replayed over the repaired installation.
The older `deployments/agent_backend/install-package.sh` is a bootstrap installer
pinned to another base and does not publish this launcher. Do not repurpose it.

## Check and publication

After source review, run `bash install-sandbox.sh --check` from this directory.
The default is read-only. It verifies the source digest and existing target
identity/permissions, and fails on a mismatch. Future infrastructure activation
must include this check; ordinary application releases neither install this
file nor authorize changing it.

Only an explicitly approved operator procedure may run `--install`. It accepts
only the known pre-repair predecessor and retains a private rollback copy. It
refuses unknown/newer files, symlinks, hardlinked targets and unexpected target
ownership/mode. On the currently repaired server it is an idempotent check and
performs no writes. This is not a universal installer or a source-parity runner.

Before any real replacement: verify no active implementation jobs; independently
review exact source/script; approve the maintenance window and backup/rollback;
secure the reviewed package as root-owned and not writable by the worker; verify
installation-directory ownership/ACLs; apply through the operator procedure;
then run the disposable PostgreSQL/containment regression in the worker context.
Do not stop/restart production application services for this file-only repair.

Rollback is an operator-authorized atomic restore of the exact saved file after
checking the current target still equals the installed repair. Restoring the
predecessor reintroduces the PostgreSQL failure; never restore it just to make a
hash match. Retain history and backups. A Git-source rollback alone must not
silently downgrade the runtime validator.

## Security and regression

Preserve unshared namespaces, cleared environment, read-only source/dependency
mounts, and credential exclusion. Verify real initdb/start/query/stop with a
private Unix socket, synthetic account lookup, denied external networking,
read-only source mounts, and missing credential paths. Use only disposable
fixtures; never load production configuration or databases for these tests.
No schema migration, worker stage, deployment eligibility exception or review
bypass is part of this change.
