# Independent review workflow

The application decisions are `approve` and `request_changes`. A succeeded worker
run means review execution completed, not that the implementation was approved.
Execution errors have no application decision and retry the same frozen candidate.
Human deployment approval, trusted validation and QA remain separate requirements.

## Components

- `core/codex_review.py`: candidate identity, raw Git snapshot export, verified
  patch application, independent manifest, review contract and executor.
- `core/review_source.py`: immutable authorized source snapshots and bounded
  list/read/search/diff operations, independent of language and file naming.
- `core/codex_review_bounded.py`: dedicated authenticated CLI control process,
  no native tools, host-mediated incremental source retrieval and turn accounting.
- Worker/service state transitions: owned-run transactions, correction queuing,
  execution retry and authenticated administrator recovery.
- Existing application/worker/deployer/database exact-candidate gates, trusted
  validators, QA handoff and separate human deployment approval remain effective.

The former dossier selector, model/catalog qualification module, evidence-only
retry, Android-parser retry, mandatory severity/kind schema and third-review
permanent block are retired. Historical review evidence is preserved.

## Model response and source inspection

Final responses use this small contract:

```json
{"decision":"request_changes","summary":"One correction is required.","findings":[{"file":"frontend-web/src/CodexUsage.tsx","issue":"Card updates leave the summary stale.","required_change":"Refresh the relevant summary after card changes."}]}
```

An approval has `decision: "approve"` and empty findings. `file` and positive
`line` are optional. Required issue/change text and the summary have modest size
bounds. Malformed JSON or inconsistent findings are execution failures.

The installed CLI does not reliably advertise dynamic source tools in the tested
configuration. The reviewer therefore uses the verified no-native-tools boundary.
To retrieve source, it returns a control response such as:

```json
{"response":{"source_request":{"operation":"read","repository":"candidate","path":"backend/routers/agent.py","offset":0,"limit":100,"query":""}}}
```

The host performs that bounded operation against captured bytes and returns a
`source_result` in the next turn of the same independent review thread. The model
can request `list`, `search` (literal text), `read` or verified `diff`. Follow
`next_offset` until null. Read/diff offsets count decoded characters; list/search
offsets count entries. Optional `limit` is 1–100 for result lists. Individual text
chunks contain at most 16,000 characters. The host permits up to 128 inspection
rounds within one configured execution timeout. Reaching a technical budget is
an execution failure, not evidence that application code is defective.

Repository names are explicit: `candidate`, `base`, and configured supporting
scope snapshots such as `backend` or `android`. Supporting commits are recorded.
The manifest comes from independently compared exported blob hashes and applied
patch bytes. The implementer cannot choose what source the reviewer may inspect.
The implementer's completion message remains in the run's message context.

No model-controlled filesystem operations exist. Snapshots reject symlinks,
hardlinks and special files, filter protected credential paths (including registry,
netrc, package-publishing, Git and signing credentials) in every snapshot, and retain
immutable bytes. Tool paths cannot traverse directories or select a host file.
The CLI control directory is separate from source, has no inherited executable
configuration, and has no shell, MCP/app/plugin, browser or network tools. Network
transport for the intended authenticated model is distinct from model tools.

## Transitions

| Outcome | Execution | Card/next step |
|---|---|---|
| Implementation complete | Succeeded | Review queued for exact candidate |
| Approve | Review succeeded | Review ready; existing validation/QA/human deployment path |
| Request changes, allowance remains | Review succeeded | Implementation correction queued atomically |
| Correction allowance exhausted | Review succeeded | `review_corrections_paused`; Continue Corrections or Cancel |
| Timeout, malformed output, transport/source failure | Failed attempt | Same review queued with backoff while execution retry allowance remains |
| Execution retry allowance exhausted | Review failed | Retry Review remains available; no correction cycle is consumed |
| Administrator Retry Review | Same candidate/ordinal | Fresh execution retry allowance; original events/usage retained |
| Administrator Continue Corrections | New implementation run | Same approved revision/scope, actionable findings and audited continuation |

The default permits three automatic implementation corrections, so the fourth
negative review pauses. Review ordinals are positive and are not capped by the
schema. Operational attempt counts remain separate and are never reset to reuse
usage keys. Expired leases are reclaimed under existing ownership checks and consume
the same absolute execution allowance as reported failures. An exhausted reclaimed
review fails recoverably before model execution; an administrator can grant a fresh
window without changing the candidate or review ordinal.

## Configuration and auditing

Use the existing worker environment configuration:

| Setting | Default/purpose |
|---|---|
| `REMIHUB_CODEX_MODEL` | Explicit reviewer model/version required; no catalog hash pin |
| `REMIHUB_CODEX_REASONING_EFFORT` | Optional reviewer reasoning override |
| `REMIHUB_REVIEW_CODEX_BIN` | Explicit executable CLI path |
| `REMIHUB_REVIEW_CODEX_HOME` | Dedicated reviewer-owned mode-0700 profile |
| `REMIHUB_REVIEW_TIMEOUT_SECONDS` | 900 seconds for the whole review execution |
| `REMIHUB_REVIEW_CORRECTION_LIMIT` | 3 automatic corrections; 0–100 |
| `REMIHUB_REVIEW_EXECUTION_RETRIES` | 2 automatic execution retries; 0–100 |
| `REMIHUB_REVIEW_RETRY_SECONDS` | 30-second execution backoff |

Actual CLI version, server-selected model/reasoning, thread/turn IDs, source
inspection records, candidate digest, supporting references and findings remain
in review evidence. Every source-request and final-decision turn has its own usage
record. Failed turns retain reported counters; unavailable counters are not zero.
Existing quota collection and best-effort telemetry behavior are preserved.

## Administrator APIs

All routes retain administrator authentication and conflict handling:

- `POST /agent/cards/{card_id}/reviews/{review_run_id}/retry`
- `POST /agent/cards/{card_id}/reviews/{review_run_id}/continue-corrections`

Both bind `candidate_digest` to the exact latest implementation/review/revision.
Authorized review corrections request a source-baseline refresh before implementation.
The workspace manager retains the original WIP in a named Git stash, advances only
by fast-forward to the approved source ref, and reapplies the candidate. Overlapping
source changes must be resolved before trusted validation; the host clears only
resolved conflict index entries. Original patch artifacts and stashes are retained,
and the successful implementation records the old/new base and stash identity.
Ordinary review execution retries never enter this writable implementation path.

Continuation accepts optional `finding_indexes` and notes; selecting only some
findings requires an explanation. It records an administrator handoff and never
creates a deployment approval. This also supports historical blocked reviews
with actual findings, without card-ID or filename-specific branches. A historical
execution failure with no findings uses Retry Review. Old review rows/messages
are not rewritten by continuation.

## Migration and release boundary

Migration 0022 expands status/ordinal constraints and updates the database trigger
to recognize the new approval decision. Exact implementation identity and separate
human deployment approval remain mandatory. Existing historical passes remain
readable; no rows are grandfathered into a new approval.

These are protected Agent infrastructure changes. The ordinary automatic deployer
must continue rejecting protected control-plane source and unrestricted schema
changes. Stage/review an immutable candidate and use an explicitly approved
RemiHub operator release procedure, QA first, then the identical production commit.
Do not fabricate an Agent card or approval to obtain release eligibility.

Rollback before new decisions can restore the prior schema through the migration
runner. The down migration refuses to discard history or strand new paused states
or ordinals. After new decisions exist, retain the expanded schema and preserve
compatible API/state readers; pause review dispatch and prepare a compatible
forward repair rather than silently restoring incompatible old workers.

Validation covers real temporary Git candidates, source security, cross-language
inspection, real CLI credential-free hostile transport cases, actual PostgreSQL
claims/retries/corrections/continuation/QA handoff/deployment trigger behavior,
HTTP administrator boundaries and actual TypeScript action/status functions.
Real-model toy-candidate rehearsal is separate evidence from a protected release
approval; neither synthetic fixtures nor a process exit status approves a release.


Partial correction-base recovery is fenced by a host-managed Git ref under
`refs/remihub-base-refresh-pending/`. The marker is recorded before stashing or
advancing HEAD, and a separate `refs/remihub-preserved/<stash>` retains the old
candidate. A base that adds a formerly untracked candidate path can prevent Git
from restoring that path even without unmerged index entries. Such failures stop
all retries and validation; matching the new HEAD cannot bypass the marker.
The execution error identifies the stash, affected source and marker. An operator
must inspect the old candidate and approved versions, explicitly restore/reconcile
all preserved source, resolve conflicts, then clear that exact marker with a
compare-and-swap deletion. Only then retry the failed implementation through the
normal authenticated action. Never clear the marker merely to make a retry pass.


Each model turn uses the app-server's structured `outputSchema`. A single
`response` transport envelope contains either the source request or the final
application decision shown above. The gateway unwraps it and retains strict host
validation; it does not repair malformed JSON, strip trailing text, or reinterpret
invalid decisions. Structured generation also constrains approval to no findings
and request_changes to at least one finding. Finding file/line remain optional.
Read/diff always return bounded 16,000-character chunks; limit is 1-100 entries
for list/search, not a requested character count. Conflicted paths are recorded
outside the worktree index and checked before validation even after staging or
an implementation-worker retry.


An approval cannot precede delivery of the complete verified diff. If the model
approves before requesting all chunks, the host supplies missing bounded chunks
and asks it to reassess. This delivers required candidate evidence without a
filename/language selector or exact tool-call prerequisite. Request Changes may
finish once an actionable material defect is identified.

The protected backend deployment helper now promotes clean expected branches by
fast-forward only. Explicit rollback retains both commits and uses non-forcing
checkout plus compare-and-swap. Local tracked edits, untracked/ignored-file
collisions and divergent source fail without overwriting work. All helper Git
commands disable hooks, fsmonitor, submodule recursion and configured external
content filters. Fixed paths/services, argument checks, candidate-parent and
expected-ref checks, source hardening, frontend integrity and existing privilege
separation remain intact. Protected Agent source still requires operator release.
