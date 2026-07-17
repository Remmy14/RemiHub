# Agent worker foundation

The agent worker is a process boundary between RemiHub's HTTP API and long
running development work. The API creates durable runs; a worker claims one run
at a time and reports its result later. Android requests never remain open while
work is executing.

The queue and lease boundary is shared by fake QA execution and the later
planning-only Codex executor. It does not create Git worktrees, build Android,
restart RemiHub, or deploy a release.

## Lease and recovery model

Every claim receives a new random `lease_token`, a worker ID, and an expiration
time. Starting or finishing work requires all three values to still match. A
worker can reclaim a `claimed` or `running` run after its lease expires. The new
token fences off the stale process, so the stale process cannot later overwrite
the reclaimed result.

The worker records:

- attempt count;
- last heartbeat time;
- lease expiration;
- next available time;
- temporary blocking reason;
- result message and structured metadata.

Claiming uses `FOR UPDATE ... SKIP LOCKED`, so two worker processes cannot claim
the same row. PostgreSQL's existing unique active-run index remains a second
line of defense.

## Temporary blocking

A transient usage or service limit changes the run and card to `blocked`, clears
the active lease, records a user-visible reason, and sets a retry time. The card
continues to occupy the one-open-card slot. Once the retry time arrives, a
worker may claim the same run with a new token. A temporary block does not
consume another maximum-attempt slot.

A blocked card can be cancelled. It cannot be closed directly; cancellation or
a terminal worker result must happen first.

## Worker database role

The intended login roles are:

- `remihub_qa_agent_worker` for QA;
- `remihub_agent_worker` for production.

Create the applicable role before migration `0004_agent_worker_leases` so the
migration can grant it access. The worker receives only:

- schema usage;
- read access to cards, messages, approvals, and runs;
- insert access to messages and events;
- column-limited update access to card workflow fields and run execution fields.

It cannot create or delete cards, approvals, runs, users, notifications, or any
other RemiHub data. It receives no access to unrelated public-schema tables.

At startup, the process verifies the exact PostgreSQL database, session role,
and current role for its configured environment. A QA worker therefore refuses
to start if its configuration accidentally points at the production database,
even when the credentials are otherwise valid.

## Executor safety

`REMIHUB_AGENT_EXECUTOR` defaults to `disabled`, causing startup to fail closed.
The `fake` executor is retained for deterministic QA and requires all three of:

```text
REMIHUB_AGENT_ENVIRONMENT=qa
REMIHUB_AGENT_EXECUTOR=fake
REMIHUB_AGENT_ALLOW_FAKE_EXECUTOR=true
```

The fake executor only advances workflow states and writes an explicit message
that it performed no repository or deployment operation. It cannot run when the
environment is `production`. The real `codex-planning` executor is described in
`docs/codex-planning-executor.md` and advertises only the planning phase.

Useful worker settings are:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `REMIHUB_AGENT_POLL_SECONDS` | 5 | Empty-queue polling interval |
| `REMIHUB_AGENT_LEASE_SECONDS` | 120 | Claim expiration time |
| `REMIHUB_AGENT_HEARTBEAT_SECONDS` | 30 | Active lease renewal interval |
| `REMIHUB_AGENT_MAX_ATTEMPTS` | 3 | Attempts before permanent failure |
| `REMIHUB_AGENT_RUN_ONCE` | false | Process at most one run, useful for QA |
| `REMIHUB_AGENT_WORKER_ID` | host and PID | Human-readable worker identity |

The worker runs an independent heartbeat thread while an executor is active.
The default heartbeat is every 30 seconds against a 120-second lease. A lost
lease fences the stale executor from recording completion even if its external
work finishes later. The shared PostgreSQL connection pool uses psycopg2's
thread-safe pool implementation so the executor and heartbeat may acquire
independent connections safely.

Executors advertise their supported phases. Those phases are included in the
database claim query, so a planning-only executor cannot claim implementation
or deployment work. A retry after a temporary `blocked` state does not consume
another maximum-attempt slot; the user may leave the card blocked or cancel it.

## Successful fake planning probe

A QA card begins as `planning_queued`. Running the worker once should produce:

```text
card: planning_queued -> planning -> awaiting_implementation_approval
run:  queued -> claimed -> running -> succeeded
```

The result contains one agent message, claim/start/success audit events, an
attempt count of one, and no remaining lease token or expiration.

## Rollback

The `0004` down migration first marks claimed, running, or blocked work as
failed, then removes lease metadata and the `blocked` card state. Stop the
worker before downgrade. A rollback cannot resume an interrupted attempt.
