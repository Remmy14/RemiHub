# Backend deployment installation assets

This directory contains the root-owned runtime helper, isolated validator, QA
application wrapper, systemd units, sudo rule, installation package template,
and destructive-safe QA verification harness for the consolidated backend
deployment executor.

`install-package.sh` is copied to the root of the generated ZIP next to:

```text
repository.patch
assets/
MANIFEST.sha256
```

It accepts only the collected production base commit
`6fc38758b6d40d81173af625d0ed186afc582e76`, creates a tested RemiHub-authored
commit, records a rollback tag and protected backup directory, archives the
pre-install QA/production deployment repositories and system paths for exact
installer rollback, installs immutable runtime assets, runs actual QA
backup/migration/failed-health restoration proofs, and leaves both deployment
workers static and inactive.

The installer also places a root-owned, non-writable Git configuration at
`/opt/remihub-agent/deployment/config/git-safe-directory.ini`. It permits Git
to inspect only the fixed cross-owner repositories and implementation worktree
root required by the deployment boundary; no global user configuration is
modified. Planning synchronization is deliberately sourced from the verified
production checkout, which is readable by its owning `alex` account, rather
than from the mode-`750` implementation bare repository owned by
`remihub-agent`.

The planning checkout is also hardened from Git index metadata after every
source synchronization or rollback. Its owner remains `alex`, its group is
`remihub-agent`, directories are setgid `2750`, regular tracked files are
`0640`, and executable tracked files are `0750`. The helper verifies the
planning worker can resolve the exact commit and read its entry module before
source synchronization is accepted. Installer worker checks require five
consecutive active states so a short-lived start followed by an import failure
cannot be mistaken for readiness.

The QA application clone is created with `--no-hardlinks` before runtime
hardening. This prevents local-clone object inodes from being shared with the
deployment target bare repository. Runtime hardening is performed by the fixed
root helper from Git index metadata: committed `100644` files become `0640`,
committed `100755` files become `0750`, application directories are setgid
`2750`, and `.git` remains root-only. The same hardening runs after every QA
promote and restore, with a `0027` umask, so new candidate files remain readable
by `remihub-qa-app` without becoming world-readable or appearing as Git mode
changes. The verifier records a clean-runtime assertion after every health,
promotion, and restoration transition. After initial hardening, the installer
runs `git fsck --full --strict` and resolves `qa-main` as `remihub-deployer`, so
object readability is proven before production promotion.

The installer resolves the installed PostgreSQL major version, binds the
workers and QA verifier to the physical versioned `pg_dump` and `pg_restore`
executables, and verifies both as `remihub-deployer`. It does not rely on the
generic Ubuntu `pg_wrapper`. Database-proof failures are printed into the
protected installer log before rollback.

Production backend deployment also requires
`/opt/remihub-agent/config/qa-parity-reader.ini`. The installer copies it to
`/opt/remihub-agent/deployment/config/qa-parity-reader.ini` as a
root-owned, `remihub-deployer`-readable file. This config must use a
least-privilege QA database role that can only read
`public.schema_migrations`; the production worker uses it only to compare
version, name, and checksum histories before any production mutation. The
worker refuses production deployment when the QA parity config is missing or
when the QA parity connection identifies as the same database as the production
migrator connection.

The QA runtime account receives only execute traversal through the fixed
`/opt/remihub-agent/deployment`, `deployment/config`, and `deployment/qa`
ancestors. Its application and log directories remain separately owned, while
the repository, worktree, artifact, and configuration files retain restrictive
ownership and modes. The installer validates the entire path chain as both
`remihub-deployer` and `remihub-qa-app` before repository seeding, prints the
full ownership chain on failure, and repeats runtime checks with systemd
`ExecStartPre` before Python starts.

The complete QA proof now runs before production is stopped or promoted. A QA
health failure preserves `systemctl status` and the last 200 journal entries in
the protected verification record and prints them before installer rollback.

Backend cards may now include reviewed `frontend-web/` React/Vite source
changes. The backend deployment worker keeps the existing backend scope and
creates a frontend artifact only when an approved candidate changes
`frontend-web/`. The source-controlled frontend policy requires Node
`v22.22.2`, npm `10.9.7`, `package-lock.json`, `npm ci --ignore-scripts`,
`npm run lint`, and `npm run build`. Dependency lifecycle scripts are disabled
during package preparation. Application secrets, deployment configs, service
helpers, host sockets, and production environment files are not provided to
frontend commands.

When an approved candidate changes `frontend-web/`, the validator resolves the
exact candidate commit and tree as `remihub-deployer`. The protected npm cache
control exports `frontend-web/` from those Git objects, rejects links and
special files, parses the lockfile against the installed registry policy, and
runs `npm ci --ignore-scripts` in a network-enabled transient unit. Ordinary QA
and production workers remain externally network denied. Prepared content is
published below `/var/cache/remihub-agent/npm/<lockfile-sha256>` only after a
root-owned cryptographic manifest covers every cache entry plus the exact
lockfile, policy, Node, npm, registry, and lifecycle identities. Every offline
validator or artifact build verifies that complete manifest before use;
corrupt or mismatched caches fail closed and are never repaired implicitly.

The frontend artifact is stored below the existing protected deployment
artifact root at
`<artifact-root>/<card-id>/<deployment-run-id>/frontend-web/<candidate-commit>/`.
It contains `manifest.json` and `dist.tar`. The canonical artifact identity is
the SHA-256 of a canonical JSON manifest containing normalized relative paths,
file types, modes, sizes, and file content SHA-256 values. Tar container bytes
are recorded separately as packaging evidence. The archive itself must still be
deterministic: entries are emitted in manifest order, timestamps are `0`,
uid/gid are `0`, owner/group names are `root`, directories are `0755`, and
files are `0644`.

The root deployment helper verifies the packaged archive against the canonical
manifest immediately before installation and verifies the installed
`frontend-web/dist` content after service health succeeds. It preserves the
current environment's exact frontend dist before mutation and restores it
during rollback. QA uses
`/opt/remihub-agent/deployment/qa/frontend-backups`; production uses
`/var/backups/remihub-agent/frontend-web/production`. Frontend installation is
inside the same protected stop/promote/start/verify boundary as backend source
promotion and database rollback.

The installer does not apply a production database migration.

## CRITICAL SERVER-WIDE PERMISSION SAFETY RULE — NON-NEGOTIABLE

**There will not be a third server-wide permissions incident.**

This rule is release-blocking and must be propagated in every future cumulative
RemiHub handoff README and successor handoff.

- No installer, rollback, recovery, or deployment package may recursively or
  archive-preservingly restore into `/` or another system parent in a way that
  can copy staging/backup ownership or mode onto `/`, `/usr`, `/etc`, `/opt`,
  `/var`, `/home`, `/dev`, or another critical parent.
- `cp -a "$BACKUP/system-root/." /` and every equivalent broad root restore are
  permanently forbidden.
- Rollback may restore only explicitly enumerated leaf paths whose exact
  pre-mutation existence/state was captured before mutation.
- If exact pre-mutation state was not captured, rollback MUST FAIL CLOSED and
  must not infer "absent", delete the path, or attempt a permission repair.
- Critical parent ownership/modes and `/dev/null` identity must be captured
  before protected mutation and independently reverified afterward.
- Recursive chmod/chown of existing system/deployment trees is forbidden as a
  rollback or repair technique. New isolated release/staging trees may still be
  hardened recursively when their entire contents were created by that run.
- An unsafe rollback is worse than a controlled stop: preserve evidence and stop
  rather than guessing.

The 2026-08-08 incident that changed `/` to mode 0750 is a regression case for
this invariant.

## Fresh QA runtime frontend bootstrap

The protected installer recreates `/opt/remihub-agent/deployment/qa/application`
from Git before complete QA verification. Generated `frontend-web/dist` content
is intentionally not tracked in Git, so a fresh QA runtime cannot serve `/race`,
`/race/draft`, or `/storage` until a verified frontend artifact is installed.

Before `qa-verify.sh` runs frontend route checks, the installer now:

1. creates one fixed, exact candidate worktree below the protected QA worktree
   root;
2. calls protected `frontend-prepare` for the exact candidate commit/tree and
   exact `package-lock.json`;
3. invokes the existing `LocalFrontendArtifactBuilder` in QA mode to perform its
   two deterministic offline builds from the prepared cache;
4. installs the resulting manifest/archive through protected `frontend-install`;
5. verifies the installed files through protected `frontend-verify`;
6. requires a real, non-symlink `frontend-web/dist/index.html` readable by
   `remihub-qa-app`;
7. removes only the exact installer-created bootstrap worktree.

The bounded `frontend-web/package.json` changed-file tuple supplied to the
builder is an explicit installer-bootstrap signal. It is not a candidate diff
and does not weaken ordinary deployment changed-file semantics.

QA route verification remains mandatory. Missing generated frontend content is
repaired before verification; `/race/draft` and the other QA frontend routes
are never skipped merely because a fresh Git runtime has no `dist`.

All bootstrap cleanup remains subject to the CRITICAL SERVER-WIDE PERMISSION
SAFETY RULE — NON-NEGOTIABLE. There will not be a third server-wide permissions
incident.
