# Codex planning executor

The first real Codex integration is intentionally limited to planning. It can
inspect a designated Git checkout and return a repository-informed plan. It
cannot claim implementation or deployment runs.

## Thread lifecycle

The first planning run starts a persistent Codex thread. The worker saves the
thread ID to `agent.cards.codex_thread_id` while it still owns the run lease and
before it starts the turn. Later user follow-ups resume that same thread and
send the latest user message. Closing completed work and creating a new card
therefore creates the fresh thread previously chosen for new work.

Every thread start, resume, and turn explicitly uses:

- `ApprovalMode.deny_all`;
- `Sandbox.read_only`;
- the designated repository as `cwd`;
- non-ephemeral storage under the worker's `CODEX_HOME`;
- the restrictions in the repository `AGENTS.md`.

The turn returns structured output containing Markdown for the card and a
Boolean readiness decision. A ready plan moves to
`awaiting_implementation_approval`; a plan with blocking questions moves to
`awaiting_feedback`.

## Process and dependency isolation

Run this worker in its own virtual environment and Linux account. Install only
`requirements-agent.txt`; do not install the production `requirements.txt`
into the worker environment. The Codex SDK currently needs a newer Pydantic
than the production API pins, and there is no reason to couple those processes.

The Python SDK package is pinned. Its published wheel installs the matching
Codex CLI runtime, and the worker communicates with that local runtime over
stdio. The worker opens no listening port.

The Linux account needs:

- read access to the worker Python source and planning checkout;
- write access only to its private `CODEX_HOME` and normal temporary storage;
- read access to a worker-specific database configuration;
- no sudo, production application credentials, Firebase credentials, Android
  signing material, or production checkout write access.

## Authentication

For a ChatGPT Plus account, perform one device-code login under the same Linux
account and `CODEX_HOME` used by the service. Codex caches and refreshes that
session. Do not place browser cookies, access tokens, or device codes in the
RemiHub database or environment file.

Authentication is an operator setup step. The normal worker never starts a
login flow and never displays credentials through the RemiHub API.

## Configuration

The planning executor is selected with:

```text
REMIHUB_AGENT_EXECUTOR=codex-planning
REMIHUB_AGENT_REPOSITORY=/absolute/path/to/readable/clean/checkout
```

Optional settings:

| Setting | Default | Purpose |
| --- | ---: | --- |
| `REMIHUB_CODEX_MODEL` | SDK/account default | Explicit model override |
| `REMIHUB_CODEX_RETRY_SECONDS` | 900 | Delay after usage/overload blocking |
| `REMIHUB_AGENT_HEARTBEAT_SECONDS` | 30 | Lease renewal interval |

`REMIHUB_AGENT_HEARTBEAT_SECONDS` must be shorter than
`REMIHUB_AGENT_LEASE_SECONDS`.

## Failure behavior

- Usage limits, overload, and retry-limit errors block the card temporarily.
- Authentication, invalid output, missing repository, and configuration errors
  fail closed and produce a system message.
- A conflicting saved thread ID fails closed.
- If the lease is lost, a stale process cannot attach a thread, complete a run,
  or overwrite the reclaimed worker's result.
- Implementation and deployment remain disabled even if those runs exist.
