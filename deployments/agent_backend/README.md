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

The installer does not apply a production database migration.
