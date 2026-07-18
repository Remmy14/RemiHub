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
→ migration-history and reversible-pair validation
→ Git rollback reference
→ PostgreSQL custom-format backup when migrations are pending
→ controlled migration execution
→ exact runtime promotion
→ service restart
→ process and /openapi.json verification
→ target/source synchronization
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
implementation repository. Production source synchronization is transactional
and is reversed during automatic rollback.

## Manifest and retries

Each deployment run writes one protected JSON manifest below the environment's
artifact root. It binds the approval, implementation run, card revision,
implementation patch hash, exact candidate commit, migration plan, validation
evidence, backup evidence, health evidence, rollback reference, and every
attempt.

A safely rolled-back failure returns the card to a timed retry state. A retry
reuses the same immutable candidate and appends a new attempt. A retry after a
confirmed success is idempotent and performs only health verification.

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
