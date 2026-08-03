# Backend GitHub synchronization runtime

The production backend deployment worker remains network-denied. After local
production deployment, health verification, target promotion, and protected
source synchronization, it may call one fixed root control helper through a
narrow sudo rule.

The root helper proves the exact candidate across canonical, implementation,
planning, and production repositories, verifies production health and the
expected operational release metadata delta, writes one root-owned request,
and starts the separate one-shot GitHub synchronization service.

The synchronization service runs as `remihub-github-sync`, not as the deployment
worker and not using Alex's live home directory. It uses a locally provisioned
copy of the existing repository deploy key, an explicit known-hosts file, a
fixed repository URL and branch, batch-only SSH, strict host-key checking, an
explicit commit refspec, a normal non-force push, and post-push `ls-remote`
verification.

The GitHub stage is allowed to run only after the production deployment and
protected local source synchronization have succeeded. Before pushing, the
service must read the configured GitHub branch and prove that the remote commit
is either the expected pre-deployment base commit or the exact deployed
canonical commit. Missing, divergent, intermediate, or otherwise unexpected
remote state is refused rather than repaired silently.

Synchronization uses a normal fast-forward push of the exact deployed canonical
commit. It must never use `--force`, `--force-with-lease`, or any equivalent
history-rewriting option. After the push, GitHub is read again and the
configured branch must resolve to the exact deployed commit; any other result is
recorded as a GitHub synchronization failure.

A GitHub failure must never cause production code, database state, protected
local source refs, or release metadata to be rolled back. Application-level
integration must record GitHub failure separately and retry only this control
helper after repeating production health verification. That retry path must not
repeat migrations, promotion, service restart, release metadata publication, or
local source synchronization.

## API recovery state

The card API exposes structured `deployment_recovery` data for backend
deployment runs so clients do not parse `blocked_reason` or free-form worker
messages. The GitHub synchronization state distinguishes local deployment
incomplete, GitHub synchronization pending, GitHub synchronization running,
retryable GitHub synchronization failure, verified GitHub synchronization, and
non-retryable remote divergence or integrity failure. The structure includes the
GitHub synchronization status, retryability, stable blocker code, last sync
error, deployed candidate commit, exact deployment run ID, and whether
production has already been deployed successfully.

Administrators may request an exact-run retry through the Agent API. The API
does not execute repository or sudo operations in the web-service process.
Instead it validates and requeues the exact blocked deployment run for immediate
claim by the existing protected production deployment worker. That worker
reuses the successful-attempt path: it repeats production health verification,
then invokes the existing protected GitHub synchronization helper, without
repeating validation, migrations, promotion, restart, target advancement, or
local source synchronization. The action is idempotent: an already-verified run
is a no-op, and a remote already at the candidate is recorded as verified
success.

## Permanent permissions rule

Do not infer privileges from names, ownership, a generic sudo check, or a
previously successful transition. Prove every exact invoking-user,
target-user, repository, helper, credential, resource, and operation boundary
live. Preserve the resolved UID/GID/groups, parent-path traversal,
owner/group/mode/ACL, Git `safe.directory`, `HOME` and credential context,
deploy-key path and permissions, known-hosts path and permissions, sudo policy,
fixed repository URL and branch, exact read/push/reread commands, return code,
stdout, and stderr in cumulative handoff evidence.
