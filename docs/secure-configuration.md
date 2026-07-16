# Secure configuration migration

RemiHub supports three independent protected configuration files:

| Environment variable | Contents | Production path |
| --- | --- | --- |
| `REMIHUB_CONFIG_FILE` | Legacy application settings, without `[Database]` | `/opt/remihub-agent/config/application.ini` |
| `REMIHUB_DATABASE_CONFIG` | Least-privilege runtime database login | `/opt/remihub-agent/config/prod-app.ini` |
| `REMIHUB_ENV_FILE` | Application environment variables and service secrets | `/opt/remihub-agent/config/remihub.env` |

The paths are intentionally independent. Setting `REMIHUB_CONFIG_FILE` never
changes the database login. This prevents a legacy application configuration
from silently replacing the restricted `remihub_app` credential.

The following runtime components use `REMIHUB_CONFIG_FILE`:

- automatic TV-provider login;
- finance worker;
- Plex download monitor;
- pool monitor;
- RH Storage fallback configuration;
- weather monitor.

The FastAPI and Flask entry points, the finance command-line entry point, and
the Spotify authorization setup script use `REMIHUB_ENV_FILE`. Systemd still
loads the protected dotenv file before Python starts; the Python calls use
`override=False` and cannot replace values already supplied by the service
manager.

File-based workers create their log directory automatically. Set the optional
`REMIHUB_LOG_DIR` environment variable to place runtime logs outside the source
checkout; when it is unset, the existing `backend/logs` location is retained.
Static assets and the generated web bundle are allowed to be absent while a
clean worktree imports the API, but deployment validation must confirm both
directories are populated before release.

## Migration safety

Create `application.ini` as a protected copy of the current application
configuration with its entire `[Database]` section removed. Keep the database
credential only in `prod-app.ini` and the migration credential only in
`prod-migrator.ini`.

Before removing the old files from the mounted source checkout:

1. Confirm the protected application INI has every non-database section and
   key present in the old INI.
2. Confirm the protected dotenv file has every active key present in the old
   dotenv file.
3. Run compilation, unit tests, configuration probes, and an API smoke test.
4. Restart RemiHub and inspect the journal for startup or worker errors.
5. Move the old files to a root-only backup outside the checkout first. Delete
   those backups only after an observation period.

Never print configuration values during the comparison. Compare section names,
key names, permissions, and file hashes only.
