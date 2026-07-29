# Lessons learned

## Trusted validation precedence must be consistent

Codex-reported sandbox checks remain valuable review evidence, but the protected
Android validator is the authoritative build gate. The approval API and the
deployment runtime must apply the same precedence.

Backend behavior remains strict: failed implementation tests are blocking.
Android behavior may supersede sandbox-only failures only when protected trusted
validation proves success, offline Gradle, denied networking, unchanged
protected build files, an unsigned APK, and the expected package.

## A pre-candidate failure is not a rollback proof

This deployment failed before candidate materialization, version reservation,
build, signing, publication, database activation, or source promotion. The
failpoint remained armed. Never call that a successful rollback.

## Preserve immutable runtime provenance

Do not modify the old versioned runtime in place. Create a new runtime directory,
validate it, and activate it through a reversible systemd override.

## Canonical hardening remains required

The installed recovery is not a substitute for committing the same fix and
regression tests to canonical backend source. The exact patch is included in the
package and must be carried into every successor handoff until committed.

## Verify every external command option on the target toolchain

The v1.0.0 runtime installer used `install --reference`, but GNU `install` does
not provide that option. `chmod`, `chown`, and `touch` have similarly named
reference options, which made the unsupported assumption look plausible.

Permanent rule: every external command option used by an operational package
must be exercised by the extracted package's non-mutating verification path.
Static syntax checks do not validate utility option compatibility.

## Track temporary mutations before the first mutating command

The failed copy was assigned to a local path, but cleanup only removed the final
runtime after a successful rename. Every temporary protected path must be tracked
before creation and removed on any failure before commit/requeue.


## Privileged child access does not make parent-side `cwd` safe

v1.0.1 invoked `subprocess.run(..., cwd=NEW_RUNTIME)` while the command itself
began with `sudo -u remihub-deployer`. Python changes directory before launching
`sudo`, so the directory change occurred as the invoking user, who correctly
lacks access to the protected runtime tree. The service user never got a chance
to run.

The durable pattern is:

1. Keep the orchestrator's process in an accessible directory.
2. Establish the target service-user identity and supplementary groups.
3. Change directory inside that child process.
4. Verify traversal and required file readability as the actual service user.
5. Only then execute tests or the worker.

Operational package tests must reject parent-side `cwd` use for protected paths.


## Test the installed runtime against its intentional dependency boundary

The v1.0.2 recovery ran the complete backend test directory with the minimal
agent-worker virtual environment. The 182 worker-compatible tests passed, but
three production FastAPI boundary modules could not import because FastAPI is
intentionally absent from `requirements-agent.txt`.

The correct fix is not to install FastAPI into the worker environment. That
would expand the trusted runtime and can recreate the documented Pydantic
version conflict between the Codex SDK and the production API.

Operational runtime packages must:

1. inspect the actual service interpreter and dependency contract;
2. run focused tests for changed behavior;
3. run every test compatible with that runtime;
4. explicitly list and justify any excluded API-only test modules; and
5. fail if any unexpected module is excluded or any selected module fails.
