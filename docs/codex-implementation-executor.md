# Codex implementation executor

The implementation executor is the second Codex boundary. It can claim only
`implementation` runs after an explicit implementation approval or a review
follow-up. It resumes the card's existing planning thread, writes only in a
card-specific worktree, and stops at `review_ready`. It cannot claim planning
or deployment work.

## Approval and thread boundary

Implementation requires both:

- a durable implementation run already queued by RemiHub's approval workflow;
- the persistent `codex_thread_id` created during planning.

A missing thread fails closed. The executor does not infer approval from a
successful planning run and cannot queue its own implementation work.

Every resumed thread and turn explicitly uses:

- `ApprovalMode.deny_all`;
- `Sandbox.workspace_write`;
- the assigned worktree as `cwd`;
- repository and implementation safety instructions;
- structured output for the review summary and validation attempts.

The executor records the SDK turn ID, duration, version, and token usage when
available, but does not treat those measurements as billing or account quota.

## Outer process sandbox

`workspace_write` is not the only process boundary. The implementation executor
requires `REMIHUB_CODEX_BIN` to point to an operator-installed executable
wrapper. The service refuses to start without that absolute executable path.

The approved wrapper must launch the real bundled Codex runtime in an outer
Bubblewrap boundary that:

- accepts only a worktree below the designated implementation root;
- clears the inherited environment before starting Codex;
- restores only the minimum `HOME`, `CODEX_HOME`, `PATH`, locale, and runtime
  values;
- gives the card worktree and private Codex state only their intended access;
- hides worker database configuration, production configuration, production
  source, signing material, SSH material, user homes, storage mounts, and
  backups;
- isolates `/proc` so Codex cannot inspect the queue worker's environment;
- leaves deployment commands, sudo, Docker control, and systemd control
  unavailable.

The wrapper is operational security configuration, not repository code. It
must be installed and probed before an implementation worker is enabled. An
arbitrary executable technically satisfies the path check, so deployment
validation must verify the wrapper's owner, mode, contents, and effective
filesystem view.

## Git workspace model

The worker uses a local Git source repository that contains no usable remote
credentials. The intended source is a dedicated bare repository refreshed by
an operator-controlled promotion step, not the live production checkout.

Each card receives deterministic values:

```text
feature branch: agent/card-<card UUID>
worktree:       <worktree root>/card-<card UUID>
```

The worker persists both values while it still owns the run lease. Existing
metadata must match the deterministic pair exactly. A per-card filesystem lock
outside the worktree prevents two reclaimed processes from writing the same
worktree concurrently.

If the branch or worktree was created just before lease-backed persistence
failed, the next owner verifies and recovers that same workspace. It never
blindly replaces an existing path or accepts a worktree attached to another Git
repository or branch.

Review follow-ups reuse the same branch and worktree while the card remains
open. A separate lifecycle step will remove historical worktrees only after the
card no longer needs review or deployment evidence.

## Validation and result capture

Codex may run focused tests inside its own sandbox. The credential-bearing queue
worker does not execute modified repository code directly. This avoids giving
unreviewed code a normal subprocess that inherits the worker's database and
Codex process context.

Codex must report every validation command as `passed`, `failed`, or `not_run`.
Those reports are advisory review evidence. Independently of the model's
response, the worker captures:

- current feature branch and `HEAD`;
- porcelain Git status;
- changed file names;
- tracked diff statistics;
- a binary patch containing tracked and untracked files.

The patch is written under the protected agent artifact root with mode `0640`.
The card then moves to `review_ready`; no commit, push, merge, migration,
build, signing, release, service restart, or deployment occurs.

## Lease loss and recovery

The shared heartbeat continues while Codex runs. If the lease is lost, the
worker invokes the SDK turn interrupt handle and fences all database completion.
The per-card worktree lock remains held until the interrupted executor unwinds,
so a reclaimed run cannot write concurrently.

Temporary SDK limits move the card to `blocked` and preserve the workspace for a
later retry. Invalid output, missing thread state, conflicting workspace
metadata, or Git validation failures fail closed.

## Configuration

The implementation worker is selected with:

```text
REMIHUB_AGENT_EXECUTOR=codex-implementation
REMIHUB_AGENT_REPOSITORY=/absolute/path/to/local/source.git
REMIHUB_AGENT_WORKTREE_ROOT=/absolute/path/to/implementation/worktrees
REMIHUB_AGENT_ARTIFACT_ROOT=/absolute/path/to/protected/artifacts
REMIHUB_CODEX_BIN=/absolute/path/to/approved/codex-sandbox-wrapper
```

Optional settings:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `REMIHUB_AGENT_GIT_TIMEOUT_SECONDS` | 120 | Timeout for worker-owned Git inspection |
| `REMIHUB_CODEX_MODEL` | SDK/account default | Explicit model override |
| `REMIHUB_CODEX_RETRY_SECONDS` | 900 | Delay after a temporary Codex block |
| `REMIHUB_AGENT_HEARTBEAT_SECONDS` | 30 | Active lease renewal interval |

Run implementation as a separate service from the planning worker. The two
executors advertise disjoint phases, and the database claim query enforces that
split.
