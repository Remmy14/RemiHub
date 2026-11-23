# Manually closed card workspace cleanup

The administrator's existing Close Card API is the sole card-status transition.
The helper has fixed read-only SQL and no card-close call or database write.
Deployment/implementation/review completion, timers and age never close cards.
A persisted Closed status plus an exact user-attributed card.closed event is
required. Closure is independent of cleanup success. There is no new daemon.

Install the root-owned helper at /usr/local/libexec/remihub-agent-card-cleanup
(mode 0755) and 97-card-cleanup.conf in the existing production deployment
service's drop-in directory. Its privileged ExecStartPost is fixed purpose; it
does not broaden the main worker's user or filesystem permissions. The existing
timer runs reconciliation after successful run-once worker execution. Failure
is logged but does not change the deployment result. A failed worker's next
successful poll retries cleanup. No database migration, API or UI change occurs.

Root-owned /etc/remihub-agent-card-cleanup.json (0600) has exactly these keys:
enabled (boolean), automatic_since (timezone-aware ISO timestamp),
historical_cards (canonical UUID list), hold_cards (UUID list), hold_paths
(absolute path list), max_resources (1..10). New manual closures after the
activation watermark are automatic; earlier closures require an explicit
reviewed historical card allowlist. Holds override automatic and historical
eligibility. There is no age-based deletion. The historical allowlist can be
cleared after reconciliation. Use --dry-run --historical before enabling it.

Root-owned /var/lib/remihub-agent/card-cleanup (0700) retains compact per-card
resource results and latest.json. Existing journald records each invocation.
Per-card records retain removal intent and Git head/branch across crashes;
recovery recognizes absent/unregistered worktrees without recreating or pruning.
Global and existing per-card workspace flocks prevent concurrent removals.

Removal requires matching scope, deterministic UUID/revision path and branch,
implementation ownership metadata, no active/retryable run, no process usage,
complete process visibility, exact Git registration/common directory/owner,
clean tracked/untracked state, and only explicitly classified ignored build
outputs. Locked, stale, dirty, uncertain or retained resources defer. Git-native
worktree removal runs as the real repository owner without force or broad
safe.directory overrides. Branches, tags, commits and shared objects remain.

Frozen patches, review/validation evidence, deployment artifacts/manifests,
release metadata, rollback refs/backups, shared caches, runtime directories,
application files, media and historical preservation backups are excluded.
Build outputs are reclaimed only as part of a verified removable worktree;
standalone artifacts with ambiguous ownership are retained. Dirty worktrees
remain deferred even when a frozen patch exists: the initial policy does not
assume that patch preserves every local/ignored file.

Inspect latest.json and per-card JSON as root, or journalctl for the existing
production deployment unit. Disk estimates use du; filesystem free-byte deltas
are affected by unrelated activity and are not exact attribution. Deferred
resources remain visible in each reconciliation report. Audit metadata is small
but follows existing card history retention; no workspace backups are created.

Rollout: verify exact candidate and current baselines; run tests and a real
controlled Git/process fixture; run production dry-run; install helper/policy/
state/drop-in while the existing timer is briefly stopped and oneshot idle;
reload systemd, run one bounded sweep, then restore the prior timer state.
Do not restart applications or cancel work. Historical cleanup uses the same
engine with a reviewed card allowlist, bounded batches and fresh checks.

Rollback disables policy.enabled and removes only this post-hook, followed by
systemd daemon-reload. Preserve state/evidence and existing source history.
Removed worktrees can be recreated from their preserved branches if needed;
dirty worktrees were not removed. Do not reset repositories or delete history.
