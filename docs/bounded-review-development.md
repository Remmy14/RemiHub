# Reviewer source and execution boundary

The active design is documented in [independent-review-implementation.md](independent-review-implementation.md).
This replaces the former pinned model/catalog, frozen dossier selection and
per-review qualification design. Historical versions remain in Git history.

The model has no native tools. It can ask the trusted host for a bounded
`source_request`, and the host consults immutable explicitly authorized repository
snapshots. It cannot choose an arbitrary host path, execute a command, write
source, read authentication or deployment controls, or fetch repository content.
The CLI's intended model transport uses the dedicated reviewer profile.

Source access is independent of language and file naming. Candidate/base snapshots
and approved supporting repository commits are frozen before inspection. Changed
files are determined from raw Git blobs and the verified candidate patch, not the
implementer's claimed file list. Export attributes/filters cannot hide or rewrite
reviewed blobs. The candidate and artifact identity are verified before and after
review, and deployment independently checks the exact reviewed candidate.

Read/diff results are chunked; list/search results are paginated. Technical limits,
missing source and malformed review output remain operational failures that retain
the candidate and correction count. They never become a third review decision.

Use `tests/test_review_transport.py` with the existing SDK environment and explicit
`REMIHUB_TEST_CODEX_BIN` to rehearse the actual CLI without credentials. Its private
synthetic provider never forwards requests. Tests check empty native tool catalogs,
rejected injected function/native shell/patch calls, source retrieval, control-home
sentinel isolation, interruption, and per-turn usage. Real-model rehearsal must
use immutable staged code and the existing reviewer service security boundary;
it is not permission to activate or promote a production release.
