# Real-card bounded reviewer

The actual `codex-review` entry in `backend/agent_worker.py` now constructs the
bounded gateway. The worker requires an explicit backend/Android scope, artifact
root, `REMIHUB_REVIEW_CODEX_BIN` (absolute reviewer-only CLI path),
`REMIHUB_REVIEW_CODEX_HOME` (dedicated owned 0700 directory), and
`REMIHUB_CODEX_MODEL=gpt-6-sol`. There is no legacy-wrapper or API-key fallback.
The CLI must be version 0.162.1 with SHA-256
`2c7a3b7a82c9faf9db5a020b80176ec48081d4b58166f59be0940e85e8c16060`.
A mismatched binary is rejected before execution. Provisioning and official
ChatGPT login belong to RemiHub; this change does not create or copy credentials.
The home must differ from the worker's HOME/CODEX_HOME, be outside the candidate,
and contain no config.toml, requirements.toml or nested .codex. Official account/read
must report ChatGPT authentication before any signed-in review thread starts.
The launch environment is cleared; provider selection is fixed to OpenAI.

## Source contract

The existing executor still verifies live HEAD, exact tracked/untracked binary
patch bytes and candidate digest before and after review; validates test evidence
(and Android's trusted offline/unsigned build evidence); freezes raw base Git
blobs without checkout filters/export rules; applies the exact patch privately;
and checks candidate immutability. Offline Git calls disable replacement objects
and lazy fetching. Blob contents are independently checked against Git object IDs.
No source repository objects, refs, index or worktree are written by freezing or
historical replay. Temporary output is confined to development artifacts or /tmp.

The real-card dossier (`remihub-real-card-review-v2`) contains:

- Exact patch bytes in base64 and SHA-256; approved card/context, handoffs,
  candidate identity, base commit, patch identity and validated evidence.
- Complete preimages and postimages for every changed path, with exact bytes,
  UTF-8 text, mode, size and SHA-256. Base images also carry Git blob IDs and
  the full base manifest has a digest. Mode-only changes are included.
- Complete bounded supporting files: ancestor AGENTS.md, referenced sibling
  modules/types (identifier matching), and tests named test_<changed module>.py.
  Selection is deterministic and never truncates files to fit.
- Explicit deleted paths and omitted candidate paths with mode, size and hash.
  Renames/copies are represented by complete old/new paths, without relying on
  display-patch parsing. Untracked additions use their exact applied postimages.

`complete_tree` is false. Supporting selection is deliberately limited, not an
assertion that dependency closure was proved. The reviewer must block when any
omitted definition, instruction, test or other context is necessary to establish
correctness. No tools can fetch missing context. A dossier that cannot include
all selected content blocks before authentication/qualification. Limits remain
256 source images, 512 KiB raw source/patch/context aggregate and 2 MiB encoded
wire size. Invalid UTF-8, NUL/binary selected files, links, special files, unsafe
paths, empty changes, malformed evidence and oversize all fail closed. Unrelated
binary assets may be explicitly omitted. Paths reject traversal, .git, control
characters, backslashes and ambiguous components. Symlinks/submodules in the base
tree still block conservatively, even when unrelated.

## Isolation and cancellation

`backend/core/codex_review_qualification.py` packages the trusted credential-free
transport qualifier. Runtime code never imports validation scripts. Each run
repeats actual SDK/CLI transport qualification, both structured verdict paths,
empty tool catalog checks, absent function/native call rejection, sentinel
checks and active cancellation. Cached proof files never enable review.
The synthetic provider is loopback-only and stopped before authenticated use.

The authenticated app-server is trusted control plane, with a private empty cwd,
no candidate working directory, empty tools/environments, all feature switches
disabled and effective configuration compared against qualification. It uses its
isolated official ChatGPT login for inference. This is a qualified tool-removal
boundary, not an OS namespace for arbitrary model tool execution. Timeout and
worker cancellation close the dedicated app-server; cancellation also propagates
through qualification. No implementer thread is resumed. A verdict cannot
approve deployment. Existing lease-loss and final candidate checks remain.

## Development validation and remaining gates

Focused reviewer/dossier/worker regressions: 83 passed. Compileall passed.
The guarded regression harness denies network/DB and live config reads and uses
synthetic database accessors. See the release validation report for final totals.
Plain worker-virtualenv discovery ran 513 tests with 26 import/config errors
(missing FastAPI and development config); dependency files were not changed.

Read-only historical replay results are recorded in
`validation/independent-review/historical-dossier-results.json`. Backend finance
candidate 0e192505579137cfbb58726e0b2e738ef5733cff serialized in 490,398 bytes
(8 source images, 316 omissions). Android AgentScreen candidate
c86127e8144744084bb6275e815dd68f67628764 serialized in 231,762 bytes
(4 images, 175 omissions). Larger Android candidate
907083d48e4614d981ef5476795a378771d8f7c4 blocked at the aggregate source budget.
These are serialization exercises only: no candidate builds, tests, signed-in
inference or passing model verdicts are claimed. Reproduce with:

```bash
PYTHONPATH=. /opt/remihub-agent/runtime/venv/bin/python validation/independent-review/historical_dossiers.py
/opt/remihub-agent/runtime/venv/bin/python -m compileall -q backend tests
/opt/remihub-agent/runtime/venv/bin/python -m unittest tests.test_codex_review_dossier tests.test_codex_review_bounded tests.test_codex_review tests.test_agent_worker -v
/opt/remihub/.venv/bin/python validation/independent-review/backend_runner.py
```

The existing frozen qualification kit was not edited or regenerated. Its checked-in
MANIFEST currently differs for frozen-source/backend/core/codex_review_bounded.py;
this local kit mismatch is not silently repaired or presented as fresh qualification.
The operator's already passed signed-in backend/Android scope and active-turn
cancellation checks for the supplied reviewer-only CLI hash are accepted as
reported prerequisite evidence. They do not automatically qualify changed source.

Remaining operational acceptance: independently review this reidentified source;
repeat fresh transport qualification and signed-in real-card reviews through the
actual worker with this implementation and dedicated home, including active
worker cancellation/lease loss; verify dossier sufficiency for each real card;
and have RemiHub provision/activate the worker settings. No production config,
DB, migration, service, build, signing, release or deployment was touched.
No schema or API changes. Rollout installs reviewed source before opt-in worker
configuration. Rollback disables the reviewer worker or restores prior reviewed
source/configuration under RemiHub control; it must not silently select a less
restricted review path. Existing candidates/evidence need fresh review after an
identity change. Preserve private auth state and the qualified frozen kit.
