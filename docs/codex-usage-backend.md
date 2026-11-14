# Codex usage backend contract

All endpoints require the existing administrator Firebase session and return
`{"success": true, "data": ...}`. They never launch Codex or read its auth files.
Migration `0021_agent_codex_usage` must be applied by RemiHub before enabling
the endpoints or worker telemetry. The migration creates separate quota cache
execution-usage, and worker stage-configuration tables; it does not change existing run evidence.

The automatic migration validator prohibits `DO` and role grants. After the
migration, RemiHub's protected database-permission provisioning step must grant
the following table privileges under the owning role. Apply the QA commands
to `remihub_qa_app` and `remihub_qa_agent_worker` first, then the production
commands to `remihub_app` and `remihub_agent_worker` after separate approval:

```sql
GRANT SELECT ON agent.codex_execution_usage, agent.codex_quota_snapshots, agent.codex_stage_configuration TO remihub_qa_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.codex_execution_usage TO remihub_qa_agent_worker;
GRANT SELECT, INSERT, UPDATE ON agent.codex_quota_snapshots TO remihub_qa_agent_worker;
GRANT SELECT, INSERT, UPDATE ON agent.codex_stage_configuration TO remihub_qa_agent_worker;

GRANT SELECT ON agent.codex_execution_usage, agent.codex_quota_snapshots, agent.codex_stage_configuration TO remihub_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.codex_execution_usage TO remihub_agent_worker;
GRANT SELECT, INSERT, UPDATE ON agent.codex_quota_snapshots TO remihub_agent_worker;
GRANT SELECT, INSERT, UPDATE ON agent.codex_stage_configuration TO remihub_agent_worker;
```

Existing `agent` schema usage grants remain necessary. The worker's `DELETE`
permission replaces an unavailable attempt placeholder with one turn record.
These grants belong to the protected operator-controlled grant path described
in `docs/agent-worker-foundation.md`, not an application migration. Do not
start the collector until migration and grant checks pass. Without API read
permission, `/agent/usage/quota` reports `unavailable` with
`DatabasePermissionUnavailable`; worker telemetry remains best effort.

## Available sources

The installed Codex CLI 0.162.1 schema captured in
`docs/codex-usage-schema/0.162.1` defines
`account/rateLimits/read`. It can report account identity, independent limit
buckets, each bucket's primary/secondary window, used percentage, duration,
reset time, plan type, credit balance and available reset-credit count. Fields
may be absent. The collector requests no reset-credit details and performs no
redemption. No absolute quota size, window start timestamp, monetary value,
or entitlement to purchase more usage is inferred. The CLI is not installed
in the implementation sandbox, so live account values are unverified here.

The SDK's turn `usage.last` counters provide input, output, cached input,
reasoning output and total tokens when returned. Session `usage.total` is
cumulative and is never aggregated. Cached input is part of input tokens;
reasoning output is part of output tokens. Review uses the bounded app-server
gateway's `thread/tokenUsage/updated` notification when it provides a last-turn
breakdown. A turn with no report is marked unavailable, including failures.
An unavailable attempt is inserted before execution and replaced when a turn
report arrives, including when later validation fails. The bounded reviewer
retains the last valid usage report if its turn later fails, while the review
verdict remains blocked. Deployment currently invokes no model, so its configuration status is
unsupported and it has no token records.

Placeholder and turn writes take the same transaction-scoped PostgreSQL
advisory lock for each run and attempt, so an overlapping placeholder cannot
remain beside a reported turn. Turn writes wait at most 100 ms in the
execution path; a stalled telemetry connection continues in a bounded daemon
writer and does not delay the card transition. Telemetry connections and SQL
statements have five-second timeouts. A run-once worker drains pending turn
writes and unavailable-attempt placeholders before successful exit, with a
finite 30-second bound for each drain. It logs a warning if a write has not
completed successfully by then, without failing a completed agent run.

Exact actual model and reasoning effort are null unless the gateway verifies
them. Planning and implementation expose configured model overrides separately;
an SDK/account default is not guessed. The qualified reviewer exposes its
pinned model as actual only when its boundary proof is returned. No stage
reasoning override exists in current worker settings.

## Endpoints

- `GET /agent/usage/quota`: `data.status` is `available`, `unavailable`,
  `stale` (older than 900 seconds) or `collection_failed`. `source`,
  `collected_at`, `last_attempt_at`, `freshness_seconds`, and `error_code`
  explain freshness. `buckets[]` retain separate limit IDs and `windows[]`
  include `name`, `used_percent`, `remaining_percent`, `duration_minutes`,
  `reset_at`, and per-window status. Null means unavailable, never zero.
  Additional window objects from a future response retain their source field
  names; a window without valid utilization is explicitly unavailable. The
  reset timestamp is null when missing or unrepresentable, while valid
  utilization and credits remain available. The
  aggregate `windows` metric is available only when at least one window has
  valid utilization.
  `ordinary_usage_allowed` and `available_reset_credits` are nullable.
  `metric_statuses` distinguishes unsupported absolute allocation and window
  starts from unavailable, stale, or failed optional metrics.
- `GET /agent/usage/summary?from_time=<ISO8601>&to_time=<ISO8601>`:
  defaults to the last 30 days; maximum span 366 days. `token_accounting`
  contains `overall`, `by_stage`, `by_card`, and `by_model` aggregates.
  Each reports execution, available, and unavailable counts. Token totals
  are null when no reported usage exists. These totals do not measure
  subscription quota.
- `GET /agent/usage/executions`: same period parameters, optional `card_id`,
  `limit` 1–100 and `offset` 0–100000. Returns `total`, `items`, `limit`,
  and `offset`. Each item identifies run, card, revision, stage, attempt,
  reviewer attempt, turn, model/config, token fields, timestamps, source,
  and availability.
- `GET /agent/usage/configuration`: current worker-reported configured model
  and reasoning per stage, separately from the last actual execution model and
  reasoning. `observed_at` identifies the latest attempt, while
  `actual_model_observed_at` identifies the latest execution that reported an
  actual model. Workers report at startup and every 60 seconds from a separate
  thread, including during long executions. A report older
  than 180 seconds is `stale`; current configured fields become null while
  last reported fields retain the historical observation. Before the first
  worker report, configuration is `unavailable`, even if executions exist.
  Deployment is `unsupported` because its executor does not invoke Codex.

## Collection and operations

After RemiHub deploys migration `0021`, an operator may run
`python -m backend.agent_usage_collector` as a separate process in an existing
authenticated Codex worker context with `REMIHUB_CODEX_BIN` set to its approved
absolute executable. The collector starts no model turn. It samples every five
minutes; each read runs in a child process with a 15-second deadline. The probe
starts a private process group so a timeout terminates it and its app-server
subprocess before the next refresh. It stores only normalized quota values
under one worker cache slot and clears the snapshot
on a failed refresh because account identity cannot be reverified. The API never contacts Codex. If the collector
is absent, quota status is `unavailable`. Worker telemetry is best effort and
cannot change review or deployment decisions. No credentials, conversation
content, or raw app-server responses are persisted.

Roll back the collector and API code before removing the migration. A database
downgrade drops usage history and is therefore destructive; retain the tables
unless RemiHub explicitly approves that data loss.
