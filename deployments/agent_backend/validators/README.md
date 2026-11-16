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

After source review, run `bash check-sandbox.sh` from this directory.
The command is read-only and accepts no arguments. It verifies the source digest and existing target
identity/permissions, and fails on a mismatch. Future infrastructure activation
must include this check; ordinary application releases neither install this
file nor authorize changing it.

There is no installation or restoration command in this source adoption. The
previous preparation's mutating installer was removed after independent review
identified check/replace races and insufficient backup verification. In the
current installation no replacement is needed: the target already has the exact
reviewed bytes. Repeating the check does not change any files or services.

A mismatch is a stop condition, not permission to copy this file over a newer
runtime. Any future byte replacement requires a separate independently reviewed
operator procedure addressing deployment admission, privileged concurrent writers,
secure package/directory/ACL ownership, verified backups, atomic publication,
postchecks and exact-current rollback safeguards. This commit does not supply or
authorize that procedure. Historical root installers can still overwrite files
if an administrator explicitly replays them; they must not be used for this
adoption. A diagnostic check cannot enforce root administrator behavior.

After source promotion, retain candidate and rollback refs. If adoption must be
withdrawn, use a separately reviewed forward source revert. Never restore the
broken predecessor merely to match historical source or reset branches backward.
The installed working validator, deployed application bytes, frontend assets and
running services remain unchanged by source adoption.

## Security and regression

Preserve unshared namespaces, cleared environment, read-only source/dependency
mounts, and credential exclusion. Verify real initdb/start/query/stop with a
private Unix socket, synthetic account lookup, denied external networking,
read-only source mounts, and missing credential paths. Use only disposable
fixtures; never load production configuration or databases for these tests.
No schema migration, worker stage, deployment eligibility exception or review
bypass is part of this change.
