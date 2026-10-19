# Manual financial accounts API

All endpoints require a Firebase bearer token for an active RemiHub administrator,
including when `REMIHUB_AUTH_MODE=transition`. They are independent of Plaid.

| Method | Path | Result |
| --- | --- | --- |
| GET | `/finance/manual-accounts` | Active accounts |
| GET | `/finance/manual-accounts?include_archived=true` | Active and archived accounts |
| POST | `/finance/manual-accounts` | Creates account, HTTP 201 |
| GET | `/finance/manual-accounts/{id}` | Account by UUID, including archived |
| PATCH | `/finance/manual-accounts/{id}` | Partial update, archive or restore |

Successful responses have `{"success":true,"data":...}`; list `data` is an
array. An account looks like:

```json
{
  "id": "b0f18a35-eec0-4bea-ab71-a93cc080087e",
  "name": "Arbol 401(k) — Voya / ADP TotalSource",
  "classification": "asset",
  "asset_category": "retirement",
  "current_balance": "12345.67",
  "as_of_date": "2026-10-18",
  "updated_at": "2026-10-19T13:00:00+00:00",
  "include_in_net_worth": true,
  "notes": null,
  "active": true
}
```

Create with `POST` and JSON like:

```json
{"name":"Arbol 401(k) — Voya / ADP TotalSource","classification":"asset","asset_category":"retirement","current_balance":"12345.67","as_of_date":"2026-10-18","include_in_net_worth":true,"notes":null}
```

`name`, `classification`, `asset_category`, `current_balance`, and
`as_of_date` are required. `include_in_net_worth` defaults to `true` and
`notes` to `null`. A name must have 1–200 characters and no leading or
trailing whitespace. Notes may have at most 2,000 characters. Balance must be
a **string** containing a finite nonnegative decimal with at most two decimal
places. The API returns two decimal places. Dates use `YYYY-MM-DD`, cannot
be in the future, and have no server-defined stale threshold. `updated_at`
is an ISO timestamp. Booleans are JSON booleans.

Allowed categories: `cash`, `brokerage`, `retirement`, `hsa`, `education`,
`home`, `vehicle`, `other`, and `liability`. Classification is `asset` or
`liability`. A liability requires category `liability`; an asset cannot use
that category. A PATCH may provide any subset of mutable fields. To change
an asset to a liability, supply `classification:"liability"` (the category
changes atomically), optionally with `asset_category:"liability"`. To change
a liability to an asset, supply both `classification:"asset"` and an asset
category. A category-only change to or from `liability` changes the derived
classification. Clear notes with `"notes":null`.

Examples: `PATCH` with `{"current_balance":"13000.00","as_of_date":"2026-10-19"}`;
archive with `{"active":false}`; restore with `{"active":true}`. Archived
accounts remain retrievable but are omitted from the default list and future
snapshots. `include_in_net_worth:false` likewise excludes future snapshots.
Writes do not regenerate existing snapshots; `/finance/summary` and
`/finance/history` remain historical snapshot views.

Malformed UUIDs, unknown fields, invalid values, empty patches, and
contradictory classification/category combinations produce HTTP 422.
Missing IDs produce 404. Duplicate active names (case-insensitive), reserved
automated valuation identities, and protected fallback conflicts produce 409.
Unauthenticated requests produce 401; non-admin users produce 403.
Validation responses use FastAPI's `detail` field, which clients may display.

`valuation_key` is intentionally not client-writable. Existing manual home
and vehicle fallbacks retain their keys. When Zillow or KBB provides a current
value for a key, the matching manual fallback is omitted from that monthly
snapshot. If a provider is unavailable, an active and included fallback is
used. Liability balances remain positive and appear under `liability` in
snapshot items; totals subtract them from assets.

## QA migration and rollout checks

Before applying migration `0016`, RemiHub must inspect QA's actual
`finance_manual_assets` schema and PostgreSQL version. Check normalized name
duplicates with `SELECT lower(btrim(label)), count(*) FROM
public.finance_manual_assets GROUP BY 1 HAVING count(*) > 1`, and duplicate
non-null valuation keys with `SELECT valuation_key, count(*) FROM
public.finance_manual_assets WHERE valuation_key IS NOT NULL GROUP BY 1
HAVING count(*) > 1`. Check invalid monetary rows before adding the check
constraint. Resolve conflicts explicitly, without deleting or merging records
silently. Apply the migration before application code. Exercise upgrade and
downgrade on a disposable PostgreSQL clone; confirm existing UUIDs, amounts,
dates, notes, keys, and snapshot history remain intact. Downgrade drops the
`active` flag and therefore loses archive state; do not downgrade after
archiving without an approved recovery of that state. Validate administrator,
non-admin, and unauthenticated HTTP requests in QA in both required and
transition modes before production promotion.
