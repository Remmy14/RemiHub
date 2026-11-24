# Backend deployment rollback recovery

A safely rolled-back deployment is a non-retryable block. The executor records
trusted rollback identity, environment and revision, checks restored migration
history, and requires operator correction rather than repeating the approved
candidate. Blocked runs with explicit `deployment_recovery.retryable=false`
have infinite availability and are excluded from deployment claims only while Blocked. A normal Queued QA
handoff or expired Running production execution may carry non-retryable display
metadata and must remain eligible under its normal phase/lease guards. Missing
metadata retains the established temporary-block behavior for unrelated work.

For legacy rolled-back attempts, the deployment manager validates the retained
manifest, current target, service health and exact base migration history before
recording a permanent block. It does not rebuild or apply migrations. A changed
baseline instead fails the existing candidate before execution, retaining the
existing supported baseline-mismatch return-to-implementation path.

The administrator-only existing Return to Implementation action accepts verified
backend QA rollback blocks, bound to the exact run, card revision and candidate.
It rejects production handoffs, production deployments, inconsistent identities,
incomplete rollback evidence and a successful deployment of the revision. The
existing transaction increments the revision and queues implementation with user
instructions, merging `refresh_base=true` into the new run metadata while retaining
its fresh review cycle. The existing workspace manager preserves dirty tracked
and untracked work in a retained stash, fast-forwards only and fences conflicts
before validation. Finalize and publish infrastructure baseline changes before
invoking recovery; never refresh manually or reuse a stale frozen patch. Old approvals and all run/review records remain historical;
modified source requires fresh trusted validation, independent review and human
deployment approval. No direct status or approval SQL is an activation step.

## Runtime activation

Source adoption alone does not update the protected worker runtime. During an
idle maintenance window, retain the previous release and create a new root-owned
`/opt/remihub-agent/runtime/releases/deployment-recovery-v1` copy. Replace only
`backend/core/agent_deployment.py` and
`backend/services/agent_worker_service.py` with their exact maintained bytes.
Preserve every other runtime file and all existing isolation/service settings.
Install the maintained `deployments/agent_backend/systemd/99-deployment-recovery.conf`
as the final drop-in for each existing backend QA and production deployment
service; reload systemd after verifying no active jobs. No new service or worker
is required. Record original release file hashes, source commit, override bytes,
service state and rollback references. Validate imports and focused tests against
the materialized release before admitting jobs. The API loads maintained source
and requires a controlled normal service restart to load its recovery guard.

Retain a targeted existing QA service admission hold while reconciling a legacy
blocked run. Never cancel active work. After the natural worker reconciliation,
use the authenticated existing Return to Implementation action. Remove only the
operation's holds and restore previously active units. Do not reuse the old
approval, manually apply migration SQL, reset baselines or modify unrelated cards.

To roll back worker activation, fence the existing deployment services, retain
all evidence and remove only this operation's runtime override. Do not restore
an old worker while a legacy retryable faulty deployment remains eligible.
Keep its admission fence until safe operator recovery is complete.
