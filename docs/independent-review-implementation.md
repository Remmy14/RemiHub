# Independent review implementation — development validation

Implemented in `/home/alex/remihub-infra-dev` for the user-approved
`INDEPENDENT-REVIEW-PLAN.md`. No deployment, migration application, live database,
Android build/signing, restart or runtime validation was authorized or performed.

## Baseline and paths

Preserved the pre-existing reconciled changes in planning/implementation and
associated tests/docs: permanent phase-aware card-thread instructions, explicit
implementation phase prompt and refusal of empty implementation artifacts.
Planning source/docs were not rewritten by this implementation. Existing launchers,
workflow guidance and the approved plan remain present. No Git reset, checkout,
commit or production checkout edit was performed.

Runtime/source reconciliation remains a rollout provenance check: only development
files and the specifically requested installed CLI schema were inspected here.
`DEVELOPMENT-WORKFLOW.md` records the earlier runtime/source gap; this task does not
claim a new reconciliation of the running release or operator wrapper bytes.

Both backend and Android implementation scopes now queue independent review.
Combined/auto scopes cannot run the reviewer. Android continues to require trusted
offline validation, unsigned APK/package evidence and the existing signing/user
approval boundaries. No separate Android application checkout was assigned, so
application-source edits and Gradle checks were unavailable. Android worker,
validator and shared deployment behavior were inspected and tested synthetically.

## Workflow, API and schema

- `review_queued` → `reviewing` is a separate leased `review` run. The implementer
  cannot complete directly into deployable `review_ready`.
- A cycle belongs to the authorized implementation revision. Attempts 1–3 are
  reserved when review runs are inserted. A unique `(card_id, card_revision,
  review_attempt)` index prevents resetting the budget using another cycle.
  Worker reclaim preserves the same run/ordinal. Implementation retry preserves
  cycle, ordinal and correction findings; reviewer execution failures cannot use
  the generic retry action to mint a new review budget.
- The candidate digest binds implementation run, revision, scope, base commit,
  binary patch hash and validation evidence. Binary patches include untracked
  files. Before review and approval, the current HEAD and patch must still match.
- The reviewer receives a fresh ephemeral SDK thread, a frozen base-plus-patch
  directory without shared Git metadata, card discussion/approved-plan context,
  instructions, diff and validation evidence. It uses deny-all approvals and a
  read-only sandbox. Source/tool output is explicitly untrusted. Frozen-tree
  mutation, malformed output, missing evidence, quota/executor errors and timeout
  cannot yield a passing review.
- A pass remains human-approval-only. Actionable changes queue a correction on the
  same implementation branch/thread/revision. The third failure or blocked review
  enters human attention with no runnable follow-up. Correction metadata is also
  included when the implementer must recover from remote compaction failure.
- Run completion, handoff and audit events share the existing owned-run transaction.
  Lease token/expiry, run status, card revision and durable review identity fence
  stale completions. No reviewer creates an approval or deployment run.
- Existing administrator-only agent routes remain unchanged. Run response models
  expose a structured `independent_review` projection; the existing web card shows
  progress, attempt limit, verdict and actionable findings. No new public route.
- Deployment approval/retry queries require a persisted pass for the exact current
  implementation run/revision/digest. The deployment claim loads that verdict
  from the review row, and the shared backend/Android deployment validator checks
  the actual patch again. Legacy `review_ready` cards receive no automatic pass.

Migration `0020_independent_review` extends status/phase checks, adds durable review
identity columns/index and grants worker run-insertion capability. A database
trigger also requires review plus explicit human approval before queued/active
deployment states, including when older application code attempts the transition.
The trigger does not replace filesystem checks in application code.

This migration deliberately requires RemiHub's separately approved schema rollout:
the application's conservative automatic migration policy rejects constraint
replacement, functions and privilege changes. That policy was not weakened.
The down migration only reverses an unused schema; it refuses to erase review
history/in-flight cards and leaves a deployment-denying constraint after rollback.
After scheduling has begun, roll back scheduling/readers while retaining schema,
audit records and the deployment gate. Do not deploy this change through an
unmodified automatic migration path that will reject 0020.

## Required rollout and rollback checks (not executed)

1. RemiHub verifies exact source/release provenance, wrappers and in-flight cards,
   stops incompatible workers, obtains schema approval/backups and applies 0020
   using its migration identity. Exercise up/down and trigger/grants on an
   explicitly authorized disposable/QA database before production.
2. Install compatible API/readers and worker source while keeping scheduling
   stopped. Confirm API access to candidate evidence: unreadable worker artifacts
   fail approval closed. No credentials should be copied to the API.
3. Provision a dedicated reviewer outer wrapper and credential-isolated filesystem
   boundary. `codex-review` requires an explicit backend/android scope, artifact
   root and `REMIHUB_REVIEW_CODEX_BIN`; it never falls back to the implementation
   wrapper. The SDK sandbox alone does not hide credentials. Probe read-only
   candidate access, denied DB/signing/deployment/auth-file access, denied tool
   network, denied writes, SDK compatibility, cancellation and process lifetime
   limits, including initialization hangs. These probes were not performed here.
4. Enable reviewer capability for each proven scope, then implementation scheduling.
   Old candidates need fresh review; never grandfather a pass. Preserve existing
   QA-to-production and human deployment authorization gates.
5. Rollback stops new scheduling. Preserve review/audit history and the gate; never
   roll back to a worker/API combination that silently permits unreviewed deploys.
   Applying the guarded down migration is not a history-erasing recovery shortcut.

## Development checks

See the final task report for the final test counts. All candidate integration
tests use temporary Git repositories, mocked SDK/DB calls and synthetic validation.
Focused checks cover first-pass success, fail/fix/pass, third failure, malformed
verdicts, missing tests, stale revision/digest/HEAD, changed untracked/binary files,
review-time mutation, reviewer edits, duplicate/expired/cancelled completion fences,
durable retry identity, quota/executor failure, SDK cancellation/timeout,
unsupported scopes and forbidden deployment without a matching persisted review.

The broad discovery attempt used the existing worker venv without installing or
changing dependencies. A temporary runner guarded host-process database/network
connections, credential/config reads and writes outside development/temp files.
The host sandbox also restricts subprocesses. An initial existing Android approval
test left the deployment wake-up unmocked: its `/run` write was denied. The test now
mocks that boundary, and no wake-up or runtime change succeeded.

Full HTTP/application checks require FastAPI and other dependencies plus an
explicit synthetic configuration fixture. They were unavailable in the assigned
worker environment. Frontend dependency/typecheck tooling is absent; no build was
run. Database SQL/grants/triggers were not live-tested. These are unresolved
validation items, not claims of successful rollout.

Final recorded results (2026-11-04):

- Existing Python: `/opt/remihub-agent/runtime/venv/bin/python` (read-only use;
  dependencies were not installed or changed).
- `-m compileall -q backend tests`: passed.
- `git diff --check`: passed.
- Focused `unittest` modules `test_codex_review`, `test_codex_implementation`,
  `test_codex_planning`, `test_agent_state`, `test_agent_worker`,
  `test_agent_worker_service`, `test_agent_service_scope`, `test_agent_deployment`,
  `test_android_deployment`, `test_agent_frontend_contract`, `test_migration_runner`:
  **290 tests passed**, 24.023 seconds.
- Full discovery through the temporary guarded runner: **483 tests attempted,
  26 errors**, 26.425 seconds. The errors are missing `fastapi`, `requests`,
  `bs4`, `plexapi`, or missing synthetic `config/config.ini`; no behavioral
  assertion failures remained. This is not a passing full-suite result.
- Offline installed CLI schema generation: passed. All three captured schema JSON
  files parsed and matched their SHA-256 manifest.
- Frozen snapshot tests include binary untracked files and export-ignore/export-subst
  attributes. The snapshot now reads raw Git blobs instead of using `git archive`;
  export attributes cannot hide or substitute base-file bytes.

No frontend typecheck/build, Android build, live SQL/grant/trigger execution,
credential-isolation probe, authenticated usage read, deployment or restart was
performed. The temporary test runner/logs and generated full protocol bundle
remain under `/tmp`; selected installed schemas are preserved in the development
checkout for review.

## Human review revision handoff (2026-11-04)

Implemented on top of the preserved uncommitted independent-review candidate.
The exhausted `changes_requested` third review now advertises **Request Revision**
next to Cancel. The web form shows each exact finding and correction, requires an
explicit Fix or Defer / Ignore choice for every finding plus operator notes, and
clears the form when the latest run/revision changes. There is no default finding
decision. The browser request builder rejects incomplete decisions and stale
review identity before calling the endpoint.

`POST /agent/cards/{card_id}/request-revision` uses the existing strict
administrator dependency, derives the actor from authentication, and accepts only
review run ID, candidate digest, indexed decisions and notes. It locks the card,
requires the latest run to be the succeeded, exhausted third `changes_requested`
review on the blocked current revision, checks its durable cycle/attempt/source
identity, and verifies the latest implementation candidate and live patch. The
server binds each decision to the persisted finding; clients cannot supply finding
text, actor identity or authority metadata. Malformed coverage, stale/cancelled
state and changed/unavailable candidates fail closed.

One transaction persists the user message, implementation authorization for a new
revision, queued correction, full decision/actor/prior-review audit event and card
transition. The new revision gives the explicit human-authorized cycle its own
three-review budget under the existing per-revision uniqueness constraint; the
migration and review index need no further changes. Card locking serializes
competing requests. A retried successful submission receives a conflict after the
state changes, rather than creating another cycle. Existing review/run/event rows
are retained. Feature branch, worktree and implementation thread remain attached
to the card. The reviewer still starts a fresh ephemeral thread.

Authenticated handoff history travels through implementation, independent review,
automatic correction rounds, failures/retries and later human-authorized cycles.
Both normal and recovery implementation prompts include it; reviewer prompts
receive it separately from untrusted discussion/repository data. It is authoritative
for scope within safety constraints, including explicit deferrals; it cannot waive
validation, credential restrictions, signing or human deployment approval. Worker
completion takes this history and cycle identity from the claimed queue record,
never from executor-provided metadata. Arbitrary card text cannot mint authority.
All-deferred decisions still queue implementation and a fresh independent review;
they never manufacture a pass.

New reviewer findings have `kind: required | advisory`. The rubric reserves required
corrections for concrete defects and separates optional improvements/preferences.
Legacy findings without `kind` remain required. A pass requires zero required
findings; advisory suggestions may accompany a pass and are labeled separately in
the UI. Automatic corrections carry required findings only. Both API approval and
shared backend/Android deployment validation parse the verdict under this rule;
there is no deployment bypass.

Rollout remains RemiHub-controlled: the existing approved 0020 schema rollout must
precede compatible API, web and worker installation. Keep deployment gates and
history on rollback; stop scheduling incompatible workers rather than erasing
human decisions or resetting budgets. No migration, build, restart, deployment,
service operation or live database access was performed for this handoff.

### Handoff validation

Synthetic tests exercise the real service transaction with mocked connection
boundaries and temporary Git candidates: complete audit attribution, exact finding
coverage, stale revision/review/candidate rejection, modified worktree, unsupported
scope, duplicate submission, transaction rollback, all-deferred decisions, history
across later cycles/rounds, failed-run metadata preservation, executor authority
forgery, and advisory/required behavior in both deployment gates. Browser contract
tests execute the actual TypeScript request builder and API client through Node's
native type erasure with only the network boundary mocked. UI assertions check
required confirmation gating, reset identity and finding labels. FastAPI boundary
tests were added for authenticated attribution, strict access, validation and
stale-state conflicts, but cannot execute in this environment without FastAPI.

Validation uses the existing read-only Python executable
`/opt/remihub-agent/runtime/venv/bin/python`; no dependencies were installed or
changed. A temporary runner at `/tmp/remihub-human-review-runner.py` denies host
network/database connections, live configuration reads, runtime commands and
writes outside development/temp paths. An initial runner rejected a harmless
`NamedTemporaryFile` directory-open event; allowing the assigned `/tmp` directory
fixed those harness errors. An initial gate test also caught an error-message
regression, corrected before the passing run.

- Final focused checks: see the recorded result below.
- Full guarded discovery: **496 tests attempted, 26 environment errors**, 29.070
  seconds; no assertion failures. Missing dependencies are `fastapi`, `requests`,
  `bs4` and `plexapi`; other errors require absent synthetic `config/config.ini`.
  This is not a passing full-suite result.
- `compileall -q backend tests` and `git diff --check`: passed.
- Frontend typecheck, React DOM/browser rendering and production bundling remain
  unavailable because frontend dependencies/tooling are absent. The executable
  TypeScript contract check is not a substitute for those checks.
- Live PostgreSQL locking, SQL/grants/triggers and schema rollback were not tested;
  transaction/race checks are synthetic. Dedicated reviewer sandbox runtime probes
  and Android application builds remain outside this assigned checkout/task.

Final handoff focused result: **317 tests passed**, 28.321 seconds. Modules:
`test_human_review_handoff`, `test_codex_review`, `test_codex_implementation`,
`test_codex_planning`, `test_agent_state`, `test_agent_worker`,
`test_agent_worker_service`, `test_agent_service_scope`, `test_agent_deployment`,
`test_android_deployment`, `test_agent_frontend_contract`, `test_migration_runner`,
`test_agent_workspace`, and `test_agent_reimplementation_safety`. Logs remain in
`/tmp/human-review-final-validated.log` and `/tmp/human-review-full-final.log`.
