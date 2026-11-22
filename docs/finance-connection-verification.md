# Finance connection verification

The existing six-hour Finance worker calls `/accounts/get` once per enabled Item
after acquiring a short database lease. An optional `/item/get` check runs first
only when `health_plaid_item_get_enabled=true` is explicitly configured after
the operator confirms its pricing and entitlement. `/accounts/get` supplies
cached inventory and balances. Verification compares included account IDs with
that inventory and applies the monthly collector's `balances.current` validator
without writing account or snapshot records. Item errors, missing accounts, and
invalid cached balances are recorded separately from transient API failures.
The API reports each account's `inventory_status` and retains the Item-wide
aggregate as `item_inventory_status`; a missing sibling does not block an
available account's cached readiness. A cached-only success does not clear a
previously confirmed Item authentication failure.

The locally installed Plaid SDK describes `/accounts/get` as free and cached,
and `/accounts/balance/get` as paid and real-time. No real-time balance request
is made by this monitor. The monthly snapshot still uses `/accounts/get`; the
discovery command uses `/accounts/balance/get` and is not invoked here. Current
Plaid contract terms, Item product entitlements, account-specific support,
rate limits, and any `/item/get` pricing were not available in the offline
implementation environment. Until that check is enabled, Item status remains
unknown unless `/accounts/get` returns a confirmed authentication error.
The default is one free cached call per enabled Item per eligible six-hour
cycle (about four per Item per day). With `/item/get` enabled, the maximum is
two calls per Item per cycle (about eight per day). Failed calls are not
retried in the same cycle.

The November 28 and November 30 worker cycles use the same cached checks before
the scheduled consolidated reminder. They do not perform a full dry run. A
successful cached check does not prove that a future provider update or live
balance request will succeed. `live_balance_tested` remains false; no deeper
verification is enabled. A technical failure keeps uncertainty visible and
does not prevent the December 1 snapshot attempt.

Apply migration `0026` before deploying code that reads verification evidence.
The migration adds a per-Item evidence table and a singleton lease. It does
not modify historical balances or issue history. The first monitoring cycle
retires legacy `PLAID_DATA_STALE` issues silently as policy reclassifications.
The Card 3 dashboard can continue parsing the existing response, but a focused
follow-up should show the new connection and inventory fields separately from
`last_monthly_balance_collection`; its current “Last successful evidence” label
does not make that distinction clear. RemiHub controls QA migration, review,
deployment, and any rollback. A code rollback can leave the additive tables
intact to retain verification history.

## Manual administrator verification

After migration `0027`, an authenticated administrator can send
`POST /finance/verification/run` with the empty JSON object `{}`. The API
returns HTTP 202 and `{ "success": true, "data": { ... } }` with
`execution_id`, `trigger`, `status`, `requested_at`, `started_at`,
`completed_at`, `reused`, `retry_at`, `summary`, `results`, and `error_code`.
The status is one of `pending`, `running`, `completed`,
`partially_completed`, or `failed`. An already running check returns its
existing execution when available; a scheduled check has no manual execution
ID. HTTP 429 returns `detail.code=FINANCE_VERIFICATION_COOLDOWN` and
`detail.retry_at`. Unknown request fields, including credentials, methods,
account selectors, and force flags, are rejected. Poll an accepted execution
with `GET /finance/verification/runs/{execution_id}`; an unknown ID returns 404.

`summary` includes distinct connection and included-account counts, methods
actually used, completion, and `live_balance_tested=false`. `results.connections`
contains Item IDs, labels, outcome, connection and inventory states, method,
timestamp, included/available/missing account counts, issue code, explanation,
and next action. `results.accounts` uses the existing preflight account IDs,
health, verification, issue, and recommended-action fields with institution
and short-mask display labels. The API does not
return Plaid payloads, credentials, account numbers, or balances.

The manual task uses the same Item verifier as the six-hour worker. A database
lease prevents overlap across API processes and the worker; a 30-minute manual
cooldown does not change scheduled eligibility. A due worker check that meets
an active manual run is recorded and replayed after the manual lease releases;
the pending check survives an interrupted handoff and is claimed by a later
worker or API background recovery task. The scheduled claim records monitoring
recovery before checking Items, and replay reconciliation sends no notifications.
This does not change the worker loop. Each provider
request has a 15-second timeout. One Item is checked at a time; a run stops after
its 15-minute budget. Results and progress are stored per Item. A process restart
leaves a pending/running row that is marked interrupted when its lease expires;
the API can then accept a later run under the cooldown policy. Monitoring
issues are reconciled without sending reminders or push notifications. The
manual path cannot call snapshot collection or modify stored financial values.
Cached inventory success remains distinct from live balance collection.
