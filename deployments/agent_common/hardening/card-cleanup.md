# Manual Close Card authorizes permanent workspace discard

Only the authenticated administrator's existing Close Card action can close a
card. The cleanup helper never closes/cancels cards and its fixed SQL is READ
ONLY. Completed, Failed and Cancelled are not deletion authority: persisted
Closed status plus the exact user-attributed card.closed event is required.
The existing lifecycle permits manual closure of Completed, Failed and Cancelled
cards. Active work must complete or be stopped through existing operator actions.

Manual closure explicitly authorizes permanently discarding every disposable,
exclusively owned worktree and its contents, including modified/untracked source,
unique uncommitted edits, ignored files and failed/abandoned build outputs.
No cleanliness, review, deployment-success or source-preservation proof is needed.
Uncommitted data may be unrecoverable. Branches, tags, commits and shared Git
objects remain; no history rewrite or garbage collection is performed.

The helper retains fixed repository/root/account boundaries, deterministic card
UUID/revision paths and branches, persisted ownership, Git registration/common
repository checks, filesystem ownership, no-follow root validation, cross-card
isolation, explicit holds, active/retryable-run checks, process visibility and
existing per-card flocks. Nested repositories, mounted/shared directories and
uncertain identity defer deletion. Symlink targets are never traversed/deleted.
Process cwd/root identities and mount aliases are checked as well as file/map
paths. Confirmed zombies/kernel tasks have no userspace execution; errors reading
an active/unclassified process fail closed. No process is killed by cleanup.

Eligible worktrees are removed as their actual repository owner using a single
Git worktree remove --force. Native Git worktree locks remain retention holds;
no double force, unrestricted recursive directory removal or global prune is
used. The helper does not examine source contents or invoke Git status/filters.
All build/temp contents inside the verified workspace are included. Outside it,
patches, run/review/validation/deployment records, manifests, releases and rollback
artifacts remain retained. Unattributed review-/index temp directories must not
be guessed into a card association. The current inventory identified no retained
standalone temporary directories in those pipeline patterns.

Existing root-owned /etc/remihub-agent-card-cleanup.json uses enabled,
hold_cards, hold_paths and max_resources (1..10). Legacy automatic_since and
historical_cards fields are accepted for rollback compatibility but no longer
gate eligibility: this policy applies to old and future verified manual closures.
Keep those legacy fields during the initial rollout so the prior helper remains
config-compatible if rollback is needed. Unknown keys fail closed. Holds always
win over closure authorization.

The existing enabled minute deployment timer continues to run the fixed cleanup
post-hook after successful idle polls as well as successful deployments. No new
worker, daemon, timer, migration or card-close API hook is introduced. Long or
failed deployments/maintenance can delay a pass; later successful polls retry.
Card closure never waits for deletion and remains Closed on failure/deferral.

Compact root-owned per-card intents/results and latest.json under
/var/lib/remihub-agent/card-cleanup survive restarts. A cleanup intent is not
closure authority: recovery rechecks the live card, holds, activity and exact
registration. If interrupted removal lost only the .git marker, the original
inode-bound marker may be restored from unchanged repository metadata before
Git-native removal resumes. Missing/unregistered completed removals are recognized
without recreating source. Disabled policy prevents destructive recovery too.
No workspace backup or source-history verification system is added.

Before rollout run --dry-run --historical and report all eligible paths and size
estimates. Do not install/enable this policy until historical deletion has been
explicitly reviewed/authorized. While existing Agent dispatch is briefly fenced
and idle, adopt the exact source, deploy the Close Card warning through the normal
frontend build/QA/asset activation procedure, then replace the helper atomically.
A Git source fast-forward alone does not deploy the compiled frontend warning.
Freshly check active jobs, expected-old refs, helper identities, collisions,
processes, holds and parity; never cancel work to make cleanup eligible.
Run bounded batches, record before/after space observations and preserve all
unclosed/unresolved resources. Restore only previously active units and verify an
idle timer invocation. Publish backend source through the protected non-forced
GitHub helper with expected-old verification; do not publish Android changes.

Rollback: disable enabled, preserve state/evidence and restore the previous
reviewed helper through an expected-current-hash atomic replacement. No Git reset
or application-data change is needed. Removing the added post-hook and reloading
systemd also stops cleanup. Discarded unique/uncommitted source is not promised
recoverable: manual closure intentionally relinquishes it.
