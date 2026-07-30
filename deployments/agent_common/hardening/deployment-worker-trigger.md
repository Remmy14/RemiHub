# Deployment worker wake boundary

Deployment approval and retry commit durable queue state before requesting a
worker wakeup. The HTTP service does not receive root privileges and does not
invoke `sudo`.

## Immediate wake path

`backend.core.agent_deployment_trigger` writes one exact request marker:

- `/run/remihub-agent/deployment-trigger/backend.request`
- `/run/remihub-agent/deployment-trigger/android.request`

The runtime directory is created by
`remihub-agent-deployment-trigger.conf` with owner `alex`, group `storage`, and
mode `0750`. Marker creation is the only API-side operation.

Root-owned systemd path units observe those exact marker files. Their paired
oneshot services consume the marker and invoke the fixed-scope
`/usr/local/libexec/remihub-agent-deployment-trigger` helper. The helper retains
an exact allowlist of the two protected deployment services and starts them with
`systemctl --no-block`.

This design remains compatible with `remihub.service` running under
`NoNewPrivileges=yes`. No sudoers entry is installed for the HTTP-service user.

## Fallback polling

Immediate wakeups are an optimization, not the durability boundary. The
database queue remains authoritative.

Independent calendar timers poll the queue even when a marker cannot be
created or consumed:

- Android at every minute boundary;
- backend at thirty seconds past every minute.

Calendar scheduling is intentional. A transition-relative
`OnUnitInactiveSec=` timer can become permanently `active (elapsed)` if it is
enabled after the target oneshot service has already become inactive.

Each deployment worker remains run-once and claims only an approved,
scope-matching deployment run. Starting an empty worker does not create,
approve, retry, or modify a card.

## Database role grant

Planning completion persists both `repository_scope` and Android
`base_branch = master`. The restricted production and QA worker roles therefore
need column-limited update access to both fields.

The protected SQL asset is:

`deployments/agent_common/sql/agent-worker-card-update-grants.sql`

It grants only:

`UPDATE (repository_scope, base_branch) ON agent.cards`

Role grants intentionally remain outside ordinary application migrations so
the automatic backend deployment SQL policy does not need to permit role or
privilege administration.

## Rollback

Restore the previous timers and sudo-based trigger assets only together. A
partial rollback can leave the API unable to wake workers.

The protected rollback SQL revokes only the newly required `base_branch`
privilege. It does not revoke the older `repository_scope` grant.
