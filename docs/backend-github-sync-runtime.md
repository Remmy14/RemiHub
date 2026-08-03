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

A GitHub failure must never cause production code, database state, protected
local source refs, or release metadata to be rolled back. Application-level
integration must record GitHub failure separately and retry only this control
helper.

## Permanent permissions rule

Do not infer privileges from names, ownership, a generic sudo check, or a
previously successful transition. Prove every exact invoking-user,
target-user, resource, and operation boundary live. Preserve the resolved
UID/GID/groups, parent-path traversal, owner/group/mode/ACL, Git
`safe.directory`, `HOME` and credential context, sudo policy, command, return
code, stdout, and stderr in cumulative handoff evidence.
