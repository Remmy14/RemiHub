# Git QA deployment executor

This phase-one executor proves RemiHub-controlled deployment without giving
Codex deployment authority. It accepts only `deployment` runs and is restricted
to `REMIHUB_AGENT_ENVIRONMENT=qa`.

## Approval binding

Deployment approval is accepted only from `review_ready` and only when the
current card revision has a successful implementation run with complete
workspace evidence. The queue claim binds the deployment run to:

- the approved deployment record;
- the successful implementation run for the same card revision;
- the implementation result metadata captured by the worker.

A review follow-up increments the card revision, so an older deployment approval
cannot authorize newer implementation work.

## Independent candidate validation

The deployment worker does not trust the Codex response alone. Before creating a
candidate it independently verifies:

- the deterministic card feature branch and implementation worktree;
- the worktree belongs to the configured local implementation repository;
- `HEAD` is still the reviewed base commit and contains no agent-created commit;
- changed files and porcelain status match the successful implementation run;
- the protected patch artifact belongs to that exact implementation run;
- recreating the current binary patch produces byte-for-byte identical output;
- the QA target branch has not advanced from the reviewed base commit.

Phase one permits only regular Markdown files beneath `docs/`. Symlinks,
submodules, paths outside `docs/`, migrations, backend code, frontend code,
configuration, build files, and release files fail closed.

## Immutable QA deployment candidate

RemiHub applies the approved patch to a separate worker-owned QA deployment
repository, stages only the approved files, and creates a RemiHub-authored
commit on:

```text
deployment/card-<card UUID>/r<card revision>
```

It then atomically fast-forwards the configured local target branch (normally
`qa-main`) using `git update-ref` with the reviewed base commit as the expected
old value. No remote is contacted.

A JSON manifest records the approval, implementation run, base and candidate
commits, target branch movement, changed files, patch SHA-256, and explicit
statements that no migration, Android release, or service restart occurred.
The deployment run then completes the card. Candidate creation is recoverable:
if the same validated commit and target movement already exist after an
interruption, a retry reuses them rather than creating a different commit.

## Configuration

```text
REMIHUB_AGENT_ENVIRONMENT=qa
REMIHUB_AGENT_EXECUTOR=git-deployment-qa
REMIHUB_AGENT_REPOSITORY=/absolute/path/to/implementation/source.git
REMIHUB_AGENT_WORKTREE_ROOT=/absolute/path/to/implementation/worktrees
REMIHUB_AGENT_ARTIFACT_ROOT=/absolute/path/to/implementation/artifacts
REMIHUB_AGENT_DEPLOYMENT_TARGET_REPOSITORY=/absolute/path/to/qa-deployment.git
REMIHUB_AGENT_DEPLOYMENT_WORKTREE_ROOT=/absolute/path/to/candidate/worktrees
REMIHUB_AGENT_DEPLOYMENT_ARTIFACT_ROOT=/absolute/path/to/deployment/artifacts
REMIHUB_AGENT_DEPLOYMENT_TARGET_BRANCH=qa-main
```

The deployment worker uses the existing least-privilege agent worker database
role. It does not require migration, application, signing, restart, sudo, SSH,
or release credentials.

## Deliberately out of scope

This executor does not:

- update production `main` or push a Git remote;
- run backend or frontend code from the candidate;
- apply database migrations;
- restart or health-check `remihub`;
- build, sign, or publish Android releases;
- perform production rollback.

Those capabilities must be introduced as separate, independently tested
RemiHub-controlled boundaries after this local QA candidate flow is proven.
