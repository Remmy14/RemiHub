# RemiHub database migrations

The migration runner records applied files in `public.schema_migrations` and
uses a PostgreSQL advisory lock so only one RemiHub migration process can run
at a time.

From the repository root:

```bash
.venv/bin/python -m backend.database.migration_runner status
.venv/bin/python -m backend.database.migration_runner upgrade
.venv/bin/python -m backend.database.migration_runner downgrade --steps 1
```

By default, the runner reads `config/config.ini`. Set
`REMIHUB_DATABASE_URL` or pass `--config /path/to/config.ini` to target a
different database.

Migration files use this naming convention:

```text
NNNN_short_name.up.sql
NNNN_short_name.down.sql
```

Rules:

- Never edit an applied `up` file. Add a new numbered migration instead.
- Each version must have an `up` file. A `down` file is strongly recommended.
- Keep each file transactional. Do not use statements such as `CREATE INDEX
  CONCURRENTLY` that cannot run inside the runner's transaction.
- Treat `down` migrations as an explicit recovery tool. They can discard data.
- Destructive migrations require explicit approval and a verified backup. State
  clearly when a `down` migration cannot restore deleted data.
- Run and verify migrations against QA before production.
- Migration `0003_agent_workflow_foundation.down.sql` drops all agent cards and
  history. Treat that downgrade as destructive even before the feature is in
  regular use.

The first `status` invocation creates the empty migration-history table if it
does not already exist.
