# Consolidated backend deployment executor

The backend deployment executor extends the proven documentation-only QA
foundation to ordinary RemiHub backend work while preserving the separation
between Codex implementation and RemiHub-owned deployment authority.

## End-to-end flow

```text
approved implementation revision
→ independent worktree/patch verification
→ immutable RemiHub-authored candidate commit
→ isolated compile and complete unittest suite
→ complete candidate migration-history and reversible-pair validation
→ QA migration-history parity check before production mutation
→ Git rollback reference
→ PostgreSQL custom-format backup when migrations are pending
→ controlled migration execution
→ exact runtime promotion
→ service restart
→ process and /openapi.json verification
→ post-validation migration-history parity check
→ target/source synchronization
→ post-deployment GitHub synchronization for production
→ completed, safely rolled back for retry, or failed closed
```

The same executor class runs in QA and production. Environment-specific systemd
units provide different target repositories, database credentials, runtime
services, health URLs, and backup roots.

## Automatically permitted changes

- UTF-8 Python regular files below `backend/`.
- UTF-8 Python tests below `tests/`.
- UTF-8 Markdown documentation below `docs/`.
- New `NNNN_name.up.sql` and `NNNN_name.down.sql` migration pairs.

Automatic deployment rejects dependencies, build files, systemd or OS changes,
secrets, configuration, Android files, binaries, symlinks, executable mode
changes, submodules, modified historical migrations, and protected agent/auth/
migration control-plane modules.

New automatic migrations must be paired, transactional, ordered after all
historical versions, and free of privileged, destructive, nontransactional, or
data-mutating up statements. Down scripts may not use `CASCADE`, mutate
application data, or remove a table/schema/type/index/sequence/view/trigger,
column, or constraint that the matching up script did not create or add. These
static inverse checks complement—rather than replace—the required human review.
More powerful migrations remain manual.

## Authority separation

The deployment queue worker runs as `remihub-deployer`, which has no interactive
shell and no general root access. Its only sudo permission is the fixed
root-owned command:

```text
/usr/local/libexec/remihub-backend-deployment-control
```

That helper accepts only fixed QA/production services and repositories, validates
all commits, branches, rollback references, current branches, clean tracked
state, and compare-and-swap expectations, and never accepts a path argument.

Candidate-authored Python and tests run first through
`remihub-backend-validation-sandbox`. The sandbox has:

- a read-only candidate worktree;
- the existing application virtual environment read-only;
- a fresh PID and network namespace;
- no database connection;
- no production configuration or secrets;
- no deployment helper, sudo, systemctl, or nested Bubblewrap access;
- only `/tmp` as writable storage.

The queue worker receives migration credentials and the narrow sudo capability,
but it never imports candidate Python. It reads validated SQL as data and uses
the trusted migration runner from its immutable release.

Backend QA and production workers use `ProtectSystem=strict`. Both expose only
`/var/lib/remihub-agent/android-release` as writable under `/var/lib` so the
fixed root deployment helper can acquire the same root-owned release lock used
by Android publication. This serializes preservation of
`deployments/release_version.json` without granting the unprivileged worker
broader filesystem access.

## Database handling

The collected production roles show that `remihub_migrator` and
`remihub_qa_migrator` are `NOINHERIT` logins with permission to `SET ROLE` to
the owning role. The executor therefore explicitly uses:

```text
QA owner role:         remihub_qa_owner
Production owner role: remihub
```

The same fixed role is passed to `pg_dump --role` and issued with `SET ROLE`
before migration inspection or execution. The worker is also bound to
installer-verified, versioned `pg_dump` and `pg_restore` binaries under
`/usr/lib/postgresql/<major>/bin`; it never relies on Ubuntu's generic
`pg_wrapper` during a deployment. A custom-format dump is verified with
`pg_restore --list` and its SHA-256 is recorded before any migration is applied.

If a later step fails, newly applied migrations are downgraded in reverse order.
Promotion, service start, target-ref update, and source synchronization are
handled as potentially ambiguous operations: rollback reads the observed durable
state and uses idempotent compare-and-swap restoration. If code or database
restoration fails, the run fails closed and preserves the backup and complete
attempt manifest for manual recovery.

Every deployment candidate also derives the complete expected migration history
from its own `backend/database/migrations` tree, not just migrations added by
the current card. Each expected row contains only the migration `version`,
`name`, and up-file SHA-256 checksum. QA deployments re-read
`public.schema_migrations` after candidate runtime and health validation and
must match that complete history exactly before the QA target ref advances.
Production deployments independently re-read QA history before creating a
rollback reference, backing up or migrating production, stopping the service,
promoting runtime code, advancing refs, or synchronizing sources. This check
runs even when the candidate adds no migration files. After production runtime
health succeeds, production history is re-read and must also match exactly
before target/source synchronization and success reporting; a mismatch enters
the normal protected rollback path.

The production worker receives a separate
`REMIHUB_AGENT_DEPLOYMENT_QA_PARITY_DATABASE_CONFIG` that points to
`/opt/remihub-agent/deployment/config/qa-parity-reader.ini`. That file must
contain a least-privilege QA database login with read-only access to
`public.schema_migrations`; it is never used for backup, upgrade, downgrade, or
other QA mutation. The worker refuses production deployment when the parity
configuration is absent or when the QA parity and production migrator
connections report the same database identity.

## Runtime and source promotion

QA promotes into the fixed `qa-runtime` checkout and controls
`remihub-backend-qa.service` on loopback port 8001. The QA wrapper imports the
candidate application with background workers disabled. The QA service account
has execute-only traversal through the fixed deployment, configuration, and QA
ancestors and read access only to its application/configuration plus write
access to its log directory; it is not added to the deployment or agent groups.
The installer validates the complete ancestor chain as the real service
accounts before repository seeding, and systemd `ExecStartPre` repeats those
runtime checks.

The installer completes compile/tests, database backup and reversible migration
proofs, candidate health, forced-health rollback, same-candidate retry, and
restored-base health before production is stopped or promoted. Failed QA health
checks preserve service status and journal output in the protected verification
record.

Production promotes into `/opt/remihub` on `main`, controls `remihub.service`,
and verifies loopback port 8000. Only after health succeeds does the executor
atomically advance `production-main` and synchronize:

- `/opt/remihub-agent/repositories/remihub-implementation.git` `main`;
- `/opt/remihub-agent/repositories/remihub-planning` `main`.

All ref movements use expected-old commit checks. The implementation source is
updated by its owning `remihub-agent` account from the deployment target. The
`alex`-owned planning checkout fetches the same verified candidate from the
`alex`-owned production runtime instead of crossing into the mode-`750`
implementation repository. After every planning reset, the fixed root helper
restores `alex:remihub-agent` ownership, setgid traversal, group-readable files,
and Git-index executable modes. It then proves the `remihub-agent` planning
worker can resolve the exact commit and read `backend/agent_worker.py`.
Production source synchronization is transactional and applies the same
hardening after automatic restoration.

Only after production deployment, health verification, target promotion, and
local source synchronization have all succeeded may the executor request
post-deployment GitHub synchronization. The GitHub stage is not part of the
transactional production rollback boundary. A GitHub-only failure must be
recorded separately and must not roll back an otherwise successful production
deployment, database migration, local source synchronization, or release
metadata publication.

## Manifest and retries

Each deployment run writes one protected JSON manifest below the environment's
artifact root. It binds the approval, implementation run, card revision,
implementation patch hash, exact candidate commit, migration plan, validation
evidence, backup evidence, health evidence, rollback reference, migration
parity evidence, and every attempt. Migration parity evidence records only
expected and observed `version`, `name`, and `checksum` rows plus the check
time; it never records database configuration paths, users, passwords, URLs, or
other protected values.

A safely rolled-back failure returns the card to a timed retry state. A retry
reuses the same immutable candidate and appends a new attempt. A retry after a
confirmed success is idempotent and performs only health verification. A retry
after a GitHub-only failure performs production health verification and GitHub
synchronization only; it must not repeat migrations, promotion, service restart,
release metadata publication, or local source synchronization.

GitHub-only blocked deployments also expose structured recovery metadata through
the Agent card response. Clients receive a machine-readable GitHub sync status,
retryability flag, blocker code, last synchronization error, candidate commit,
deployment run ID, and a flag proving production was already deployed. The
explicit administrative retry action is bound to the exact card and deployment
run. It requeues that existing blocked run for the protected production worker;
the API process does not construct the deployment executor or invoke sudo
helpers directly. The worker transitions the run/card to normal completion only
after the protected GitHub helper records verified synchronization. If the
helper reports a retryable blocker, production remains untouched and the
structured blocker is updated. If the helper reports remote divergence or
protected-source integrity failure, the state is marked non-retryable for
manual recovery.

## Static services

The installer creates but does not enable or start the deployment workers:

```text
remihub-agent-deployment-qa.service
remihub-agent-deployment-production.service
```

Start exactly one run after the corresponding deployment approval exists:

```bash
sudo systemctl start remihub-agent-deployment-qa.service
sudo systemctl start remihub-agent-deployment-production.service
```

Inspect the run and manifest before cleaning any candidate worktree or branch.
