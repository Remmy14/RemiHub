# Deployment worker wake boundary

Deployment approval and retry commit durable queue state before requesting a
worker wakeup. The HTTP service does not receive root privileges and does not
invoke `sudo`.

## Immediate wake path

`backend.core.agent_deployment_trigger` writes one exact request marker:

- `/run/remihub-agent/deployment-trigger/backend-qa.request`
- `/run/remihub-agent/deployment-trigger/backend-production.request`
- `/run/remihub-agent/deployment-trigger/android.request`

The runtime directory is created by
`remihub-agent-deployment-trigger.conf` with owner `alex`, group `storage`, and
mode `0750`. Marker creation is the only API-side operation.

Root-owned systemd path units observe those exact marker files. Their paired
oneshot services consume the marker and invoke the fixed-scope
`/usr/local/libexec/remihub-agent-deployment-trigger` helper. The helper retains
an exact allowlist of the protected deployment services and starts them with
`systemctl --no-block`.

Fresh backend deployment approvals and normal deployment retries request the QA
deployment worker first. Backend production wakeups are explicit and reserved
for production-stage retries such as GitHub synchronization recovery. Android
wakeups keep their existing request marker and target service.

This design remains compatible with `remihub.service` running under
`NoNewPrivileges=yes`. No sudoers entry is installed for the HTTP-service user.

## Fallback polling

Immediate wakeups are an optimization, not the durability boundary. The
database queue remains authoritative.

Independent calendar timers poll the queue even when a marker cannot be
created or consumed:

- Android at every minute boundary;
- backend QA at fifteen seconds past every minute;
- backend production at thirty seconds past every minute.

The QA timer targets `remihub-agent-deployment-qa.service`; the production
timer targets `remihub-agent-deployment-production.service`. Production polling
remains necessary because a QA-successful backend deployment is requeued with
the same run/card/candidate and becomes production-eligible only after
`deployment_pipeline.stage = "qa_succeeded"` is recorded.

Calendar scheduling is intentional. A transition-relative
`OnUnitInactiveSec=` timer can become permanently `active (elapsed)` if it is
enabled after the target oneshot service has already become inactive.

Each deployment worker remains run-once and claims only an approved,
scope-matching deployment run. Starting an empty worker does not create,
approve, retry, or modify a card.

## Database role grants

Planning completion persists both `repository_scope` and Android
`base_branch = master`. The restricted production and QA worker roles therefore
need column-limited update access to both fields.

Exact deployment approval and retry binding is persisted in `agent.events`.
The same restricted worker roles therefore also need read-only access to that
table when claiming deployment work or reconstructing an exact retry binding.

The protected SQL assets are:

- `deployments/agent_common/sql/agent-worker-card-update-grants.sql`
- `deployments/agent_common/sql/agent-worker-events-select-grant.sql`

They grant only:

- `UPDATE (repository_scope, base_branch) ON agent.cards`
- `SELECT ON agent.events`

The event grant does not add `UPDATE`, `DELETE`, `TRUNCATE`, `REFERENCES`, or
`TRIGGER` privileges. The existing `INSERT` privilege remains unchanged so
workers can continue writing audit events.

Role grants intentionally remain outside ordinary application migrations so
the automatic backend deployment SQL policy does not need to permit role or
privilege administration.

## Rollback

Restore the previous timers and sudo-based trigger assets only together. A
partial rollback can leave the API unable to wake workers.

The card-update rollback SQL revokes only the newly required `base_branch`
privilege. It does not revoke the older `repository_scope` grant.

The event-read rollback SQL revokes only `SELECT ON agent.events`. It does not
revoke the worker's existing `INSERT` privilege.
