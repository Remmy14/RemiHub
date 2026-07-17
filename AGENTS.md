# RemiHub agent guidance

## Repository map

- `backend/`: FastAPI application, routers, services, tasks, and database code.
- `backend/database/migrations/`: ordered reversible PostgreSQL migrations.
- `tests/`: Python `unittest` suite.
- `frontend-web/`: web frontend when present in the checkout.
- Android source and signing may live in a separate checkout. Do not assume it
  is available unless the task explicitly provides it.

Read the nearest more-specific `AGENTS.md` if a subdirectory adds one.

## Safety boundaries

- During planning, inspect only. Do not modify files or run state-changing
  commands.
- Work only in the agent's assigned Git checkout or worktree. Never edit the
  live `/opt/remihub` production checkout.
- Never read or expose secrets from `/opt/remihub-agent/config`, systemd
  environment files, Firebase credentials, signing material, or database
  passwords.
- Never connect to or mutate the production database. QA database work must be
  explicitly requested and use the supplied QA configuration.
- RemiHub, not Codex, owns migrations, builds, signing, releases, service
  restarts, deployment, and rollback.
- Do not use destructive Git commands. Preserve unrelated user changes.
- Use LF line endings for repository text files.

## Validation

The primary backend checks are:

```bash
.venv/bin/python -m compileall -q backend tests
.venv/bin/python -m unittest discover -s tests -v
```

If the assigned environment uses a different virtual environment, use its
Python executable without changing dependency files merely to make imports
work. Add focused tests for changed behavior and report checks that could not
be run.

## Planning output

A plan should identify affected files or components, schema and API effects,
test strategy, security implications, rollout order, and rollback concerns.
Ask only questions that materially change implementation. Do not claim that a
build, migration, restart, deployment, or live validation occurred unless the
corresponding RemiHub-controlled step actually reports success.
