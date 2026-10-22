# RH-Storage worker API

These endpoints are served by the RemiHub backend under `/rh-storage`. They
use the backend's existing `RH_STORAGE_DATABASE_URL` (or `[RHStorage].db_url`)
and never expose a PostgreSQL connection to the browser. All requests need a
Firebase bearer token belonging to an active RemiHub user, even when the
application runs in transition auth mode. Mutations additionally require the
RemiHub `admin` role. The existing `GET /rh-storage/status` contract is unchanged.

## Routes

| Method and path | Body | Permission | Result |
| --- | --- | --- | --- |
| `GET /rh-storage/workers/config` | None | Authenticated user | Configuration |
| `PATCH /rh-storage/workers/config` | `{"default_workers": 5}` | Admin | Configuration |
| `POST /rh-storage/workers/boost` | `{"workers": 5, "duration_minutes": 120}` | Admin | Configuration |
| `DELETE /rh-storage/workers/boost` | None | Admin | Configuration |
| `GET /rh-storage/workers` | None | Authenticated user | Configuration and monitoring |

Worker counts must be integers from 1 through 16. Durations are integer minutes:
15, 60, 120, or 240. The boost is stored as `boost_workers` and `boost_until`
in a single PostgreSQL `UPDATE`, using PostgreSQL's clock. Clearing it sets
both fields to null. RH-Storage detects configuration updates itself; these
routes do not restart it.

Every successful response has `{"success": true, "data": ...}`. Configuration
responses contain:

| Field | Meaning |
| --- | --- |
| `default_workers` | Permanent target |
| `boost_workers`, `boost_until` | Stored temporary target and UTC expiration; may remain populated after expiry |
| `boost_active` | Whether `boost_until` is later than PostgreSQL `NOW()` |
| `effective_workers` | Boost target while active, otherwise permanent target |
| `updated_at` | PostgreSQL configuration update time, UTC |
| `server_now` | PostgreSQL time used to evaluate the effective target, UTC; use it to offset frontend countdowns |

`GET /rh-storage/workers` returns `data.configuration` with those fields plus:

| Field | Meaning |
| --- | --- |
| `monitoring_status` | `available`, `no_recent_heartbeats`, or `ambiguous` |
| `observed_daemon_pid`, `observed_hostname`, `observed_at` | PID, host, and most recent heartbeat for the selected daemon process; null if none or ambiguous |
| `counts` | Aggregate counts, or null if process selection is ambiguous |
| `workers` | Individual heartbeat records for the selected daemon process |

Heartbeats at most 45 seconds old are fresh. Fresh rows are grouped by
`(hostname, pid)` because workers are coroutines sharing the daemon PID. Only
the group with the newest heartbeat contributes to the result. If two groups
have exactly the same newest heartbeat time, monitoring is `ambiguous` and
counts are null. Previous process groups are excluded even during the 45-second
overlap after a restart. With no fresh group, the request still succeeds with
`monitoring_status: "no_recent_heartbeats"`, zero counts, and an empty list.
Database failures return `503` instead.

`counts` contains `active`, `busy`, `idle`, `retiring`, `healthy`, `unhealthy`,
and `observed_gap`. Active means fresh and not `stopped`; `busy` means
`copying`; `idle` means explicitly `idle`; `retiring` is a separate flag.
Fresh `idle`, `copying`, and `stopping` workers count as healthy; `recovering`
and unrecognized states count as unhealthy. `last_error` is shown but does not
by itself establish current health because it may be historical.
`observed_gap` is `max(effective_workers - active, 0)`, an observed shortfall,
not a count of identified missing workers. Active workers above the target
during scale-down are expected and do not become unhealthy.

Each `workers` item contains `worker_id`, `hostname`, `pid`, `state`, `retiring`,
`last_seen_at`, `heartbeat_age_seconds`, `last_error`, `pool_id`, `job_id`,
`job_type`, `rel_path`, `pool_name`, `pool_mountpoint`, `source_branch_id`,
`source_branch_path`, `source_branch_registered_path`, `target_branch_id`,
`target_branch_path`, `target_branch_registered_path`, `bytes_copied`,
`total_bytes`, `progress_percent`, `job_started_at`, `job_elapsed_seconds`,
`last_progress_at`, and `last_progress_age_seconds`.

Job details use a left join from heartbeat to job and pool. The source and
target paths are taken from `jobs.payload`, exactly as the RH-Storage worker
does for replication. The `*_registered_path` fields are optional cross-checks
from `branches`; they never replace payload paths. Branch IDs are strings in
the JSON response. Missing jobs, absent payload keys, non-replication jobs,
and missing progress values yield null details. A percentage is null unless
both byte values exist and `total_bytes > 0`; otherwise it is bounded to
0–100. Age and elapsed fields are seconds based on PostgreSQL `server_now`.
The API returns branch paths and `rel_path` separately so the frontend can
describe source and destination without inventing an absolute file path.

Errors follow FastAPI's `{"detail": ...}` format: `401` for missing or invalid
authentication, `403` for non-admin mutations, `422` for invalid payloads,
and `503` for an unavailable RH-Storage connection, query, or singleton
configuration. Database error text and credentials are not returned.

## Deployment database privileges

Before deployment, RemiHub must check privileges **as the configured RH-Storage
connection identity**. This implementation cannot inspect production secrets
or connect to production PostgreSQL. The final queries need `CONNECT` on the
`rh_storage` database; `USAGE` on schema `public`; `SELECT` on
`public.worker_scaling` and `public.worker_heartbeats`; `SELECT` on the columns
`job_id`, `pool_id`, `type`, `rel_path`, `payload`, and `target_branch_id` in
`public.jobs`; `SELECT` on `pool_id`, `name`, and `mountpoint` in
`public.pools`; `SELECT` on `branch_id`, `pool_id`, and `path` in
`public.branches`; and `UPDATE` only on `default_workers`, `boost_workers`,
`boost_until`, and `updated_at` in `public.worker_scaling`.

The existing `/rh-storage/status` route already reads jobs, pools, and branches,
but that does **not** prove the role can read the newly used `jobs.payload`,
`jobs.target_branch_id`, or `branches.branch_id`. Check those explicitly.
Report each missing permission and grant only what is absent through the
RemiHub-controlled database process. No RH-Storage table change is required.

An operator can run the following read-only check through the configured
connection. Each result must be true before deployment:

```sql
SELECT
  has_database_privilege(current_user, 'rh_storage', 'CONNECT') AS database_connect,
  has_schema_privilege(current_user, 'public', 'USAGE') AS schema_usage;

SELECT relation_name, column_name, privilege,
       has_column_privilege(
         current_user, format('public.%I', relation_name), column_name, privilege
       ) AS granted
FROM (VALUES
  ('worker_scaling', 'singleton', 'SELECT'),
  ('worker_scaling', 'default_workers', 'SELECT'),
  ('worker_scaling', 'boost_workers', 'SELECT'),
  ('worker_scaling', 'boost_until', 'SELECT'),
  ('worker_scaling', 'updated_at', 'SELECT'),
  ('worker_scaling', 'default_workers', 'UPDATE'),
  ('worker_scaling', 'boost_workers', 'UPDATE'),
  ('worker_scaling', 'boost_until', 'UPDATE'),
  ('worker_scaling', 'updated_at', 'UPDATE'),
  ('worker_heartbeats', 'worker_id', 'SELECT'),
  ('worker_heartbeats', 'hostname', 'SELECT'),
  ('worker_heartbeats', 'pid', 'SELECT'),
  ('worker_heartbeats', 'state', 'SELECT'),
  ('worker_heartbeats', 'pool_id', 'SELECT'),
  ('worker_heartbeats', 'job_id', 'SELECT'),
  ('worker_heartbeats', 'last_seen_at', 'SELECT'),
  ('worker_heartbeats', 'last_error', 'SELECT'),
  ('worker_heartbeats', 'retiring', 'SELECT'),
  ('worker_heartbeats', 'bytes_copied', 'SELECT'),
  ('worker_heartbeats', 'total_bytes', 'SELECT'),
  ('worker_heartbeats', 'job_started_at', 'SELECT'),
  ('worker_heartbeats', 'last_progress_at', 'SELECT'),
  ('jobs', 'job_id', 'SELECT'),
  ('jobs', 'pool_id', 'SELECT'),
  ('jobs', 'type', 'SELECT'),
  ('jobs', 'rel_path', 'SELECT'),
  ('jobs', 'payload', 'SELECT'),
  ('jobs', 'target_branch_id', 'SELECT'),
  ('pools', 'pool_id', 'SELECT'),
  ('pools', 'name', 'SELECT'),
  ('pools', 'mountpoint', 'SELECT'),
  ('branches', 'branch_id', 'SELECT'),
  ('branches', 'pool_id', 'SELECT'),
  ('branches', 'path', 'SELECT')
) AS needed(relation_name, column_name, privilege);
```

Table-level `SELECT` also satisfies each column check; column-level grants
can be used when a narrower grant is needed. When absent, the narrow mutation
grant is:

```sql
GRANT UPDATE (default_workers, boost_workers, boost_until, updated_at)
ON public.worker_scaling TO runtime_role;
```

Replace `runtime_role` with the identity established by the protected runtime
connection, and apply only the grants that the check finds missing. Do not
grant write access to jobs, branches, pools, or heartbeats.
