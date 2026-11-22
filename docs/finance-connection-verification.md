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
