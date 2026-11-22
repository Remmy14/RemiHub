# Finance health monitoring

The existing Finance worker calls the stored-evidence Card 1 assessment before
its monthly snapshot check and again after the check when collection has
committed new evidence. The first assessment on each New York calendar day is
eligible even when the evidence has not changed. Later same-day assessments
run only when the stored evidence fingerprint changes. A failed evidence read
or completeness check rolls back the monitoring transaction and leaves the
previous assessment in place. Monitoring never contacts Plaid or valuation
providers and does not alter the snapshot decision.

The monitor targets the **following** snapshot month. For a March 1, 2027
snapshot, reminder dates are February 26 and February 28 in
`America/New_York`. The three-day reminder may run on the following local day
if a worker cycle was missed. The one-day reminder has no catch-up after its
scheduled day. Both windows are marked processed even when no notice is
appropriate. Reminders use current account evidence; a successful prior-month
snapshot or `READY` result does not guarantee the upcoming collection. A new
critical alert suppresses an equivalent reminder in the same cycle.

The snapshot worker still selects its month with the server-local `date.today()`
and checks every six hours. QA sets `TZ=America/New_York`, but the production
runtime timezone is not established by this repository. On a UTC server, the
March attempt could start at 00:00 UTC March 1, which is 19:00 New York time
on February 28. The pre-collection monitor runs first in that cycle, though
the existing notification worker and device delivery may complete later.
No snapshot schedule or timezone setting is changed here.

`GET /finance/preflight` remains a live, read-only Card 1 assessment.
`GET /finance/monitoring` is admin-protected and returns the last automated
assessment date/time, current and previous readiness, a sanitized summary,
active issue references, and up to 50 recent logical events. It does not
return account values, provider errors, or notification payloads. Cards 3 and
4 may use this endpoint for presentation. Android currently opens the app on
a Finance push; Finance-specific navigation is outside this card.

Migration `0025_finance_health_monitor` adds a singleton assessment state,
one row per issue identity, and logical notification events. A row lock on
the singleton and unique event keys prevent duplicate logical notifications
across retries and overlapping worker calls. `insert_notification()` uses the
same transaction. Notice recipients are active admin users, selected from the
existing authenticated user table and passed through `data.user_id`. The
schema migration is DDL-only; the worker initializes its singleton row with
an idempotent insert on first use. The
existing Firebase delivery worker may retry an already persisted notification;
it cannot guarantee exactly-once physical delivery if it crashes after FCM
accepts a message but before marking that row sent.

Issue resolution uses evidence for the particular issue code. A corrected
manual value can clear its missing-value issue while a stale review date
remains, and a new review date can clear review-due while a value problem
remains. A successful active-manual-inventory read that no longer contains an
asset closes its old issues without a recovery notice; an unavailable inventory
does not clear them. A connection reauthorization issue is retired without a recovery
push when no included account still depends on that connection. A verified
fix can generate an issue-specific resolution notice; uncertain evidence
leaves the issue open. If a Plaid item is disabled, its old reauthorization
issue is retired. A replacement item produces a recovery notice only when
every included account has verified current collection evidence at or after
the replacement's successful connection update. Replacement connection
success alone does not prove that an account recovered.

The existing successful-snapshot notice now also targets active admins and
omits net worth from the push body. The snapshot record and response still
contain their original values; only the notification payload changes.

QA and production migrations, service restarts, and rollback remain
operator-controlled. The migration's down file discards monitoring history.
