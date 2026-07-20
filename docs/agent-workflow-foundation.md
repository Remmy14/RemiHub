# Agent workflow foundation

This foundation stores RemiHub's development-agent workflow in the existing
`remihub` PostgreSQL database. The objects live in a separate `agent` schema;
this is not a separate database.

## Safety boundary

The schema/API foundation itself does not invoke Codex, create Git worktrees,
run builds, apply migrations, restart services, or deploy releases. It records
administrator requests and creates durable queued runs for a separate worker;
later worker increments consume that queue while preserving these boundaries.

All `/agent` endpoints require a valid Firebase bearer token for an active
RemiHub administrator. They remain strict even while legacy API routes use
`REMIHUB_AUTH_MODE=transition`.

## Stored objects

| Table | Purpose |
| --- | --- |
| `agent.cards` | One durable feature or change request and its current state |
| `agent.messages` | User, agent, worker, and system conversation history |
| `agent.runs` | Durable planning, implementation, or deployment work queue |
| `agent.approvals` | Revision-specific implementation and deployment decisions |
| `agent.events` | Append-only audit history for user-visible workflow actions |

PostgreSQL unique indexes enforce both of these invariants:

- At most one non-terminal card can be open.
- At most one run can be queued, claimed, or running.

A failed card still occupies the open-card slot until it is retried, cancelled,
or closed. A completed or cancelled card releases the slot; closing it hides it
from the default card list.

Worker migration `0004_agent_worker_leases` also adds a temporary `blocked`
state. A blocked card retains the open-card slot and records when and where its
run should resume.

Migration `0005_agent_repository_scope` adds `agent.cards.repository_scope`.
Existing cards are backfilled to `backend`, preserving their historical
behavior. New cards start at `auto`; a successful planning run resolves that
scope to exactly one of:

- `backend`
- `android`
- `backend_and_android`

The resolved scope is stored on the card, in planning run metadata, and in the
planning success audit event.

## Card lifecycle

The intended successful path is:

```text
planning_queued
  -> planning
  -> awaiting_implementation_approval
  -> implementation_queued
  -> implementing
  -> review_ready
  -> deployment_queued
  -> deploying
  -> completed
  -> closed
```

An agent can instead move planning to `awaiting_feedback`. A user follow-up
from either planning wait state queues a new planning run. A user follow-up
from `review_ready` queues another implementation run. Each follow-up advances
the card revision. Implementation approval is recorded against the approved
plan revision; review feedback remains inside that authorized implementation
phase. Deployment approval is recorded against the final revision presented
for release.

During the bootstrap dual-repository milestone, implementation and deployment
approval remain backend-only. Cards scoped to `android` or
`backend_and_android` can be planned and discussed, but approval returns a
state conflict until component-specific implementation worktrees and Android
validation are installed. Cards that still have `repository_scope = 'auto'`
cannot enter implementation.

Within one open card, a future worker may resume the same Codex thread. Once a
deployed card is completed or closed, a new change starts as a new card and a
fresh Codex thread against the then-current repository.

## Administrator API

| Method and path | Effect |
| --- | --- |
| `POST /agent/cards` | Create the only open card and queue its first planning run |
| `GET /agent/cards` | List cards; `include_closed=true` includes closed history |
| `GET /agent/cards/{id}` | Return a card with messages, runs, approvals, and events |
| `POST /agent/cards/{id}/messages` | Add feedback and queue the appropriate next run |
| `POST /agent/cards/{id}/approve-implementation` | Record approval and queue implementation |
| `POST /agent/cards/{id}/approve-deployment` | Record approval and queue deployment |
| `POST /agent/cards/{id}/cancel` | Cancel a cancellable card and its active run |
| `POST /agent/cards/{id}/close` | Close a completed, cancelled, or failed card |

The first client message may include a `client_message_id` UUID. Follow-up
messages may also include one; the database rejects a duplicate within the same
card so an Android retry cannot silently enqueue the same work twice.

Card creation still requires only title and description. The server assigns
`repository_scope = 'auto'`, and list/detail responses include the current
scope.

## Migration and rollback

Migration `0003_agent_workflow_foundation` creates the schema. Apply and test it
against `remihub_qa` before production. Its down migration drops the entire
`agent` schema and all card history, so it is a destructive recovery operation
that requires an explicit backup and approval.

When the normal `*_migrator` connection applies the migration, it derives the
matching `*_app` role and grants only the application access required by these
APIs. A separate, narrower worker database role will be provisioned with the
worker rather than embedded in this application migration.

## Next increment

The worker increment will claim queued runs with a lease, invoke Codex with
phase-appropriate filesystem permissions, append agent responses, and advance
card/run state. Build, signing, migration, service restart, validation, and
rollback remain RemiHub-owned deployment operations behind explicit approval.

The next Android increment should add component-specific implementation
worktrees and independent Android validation. Until then, Android planning has
visibility only; it has no build, signing, publication, or deployment
authority.
