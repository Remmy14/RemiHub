# Authentication foundation rollout

This patch is the backward-compatible first phase of RemiHub authentication.
It adds Firebase ID-token verification and database migration support, but it
does not switch the current Android app to authenticated requests.

## Safety model

`REMIHUB_AUTH_MODE` controls the API boundary:

| Mode | Missing bearer token | Valid bearer token | Invalid bearer token |
| --- | --- | --- | --- |
| `disabled` | Allowed | Ignored | Ignored |
| `transition` | Allowed | Authenticated | Rejected |
| `required` | Rejected | Authenticated | Rejected |

The default is `transition`. Keep that setting throughout this patch. The
`/auth/me` endpoint is intentionally strict in every mode so it can be used to
test authentication without enforcing it across the rest of the API.

Only FastAPI routers are behind the new dependency. Static web assets,
`/favicon.ico`, and the OpenAPI documentation remain public.

## Configuration

Add these values to the permission-restricted systemd environment file. The
production path used in this rollout is
`/opt/remihub-agent/config/remihub.env`:

```dotenv
REMIHUB_CONFIG_FILE=/opt/remihub-agent/config/application.ini
REMIHUB_DATABASE_CONFIG=/opt/remihub-agent/config/prod-app.ini
REMIHUB_ENV_FILE=/opt/remihub-agent/config/remihub.env
REMIHUB_AUTH_MODE=transition
REMIHUB_ADMIN_EMAILS=your-google-account@example.com
FIREBASE_SERVICE_ACCOUNT_FILE=/opt/remihub-agent/config/firebase-service-account.json
FIREBASE_CHECK_REVOKED=true
```

`REMIHUB_CONFIG_FILE` points legacy application components at their protected
non-database INI configuration. The protected copy must not contain a
`[Database]` section. `REMIHUB_ENV_FILE` gives API entry points and manual
scripts the same protected dotenv path used by systemd.

`REMIHUB_DATABASE_CONFIG` lets the running service use a permission-restricted
application credential outside the source checkout. It does not affect the
migration runner when that command is given its separate `--config` path.
`FIREBASE_SERVICE_ACCOUNT_FILE` is shared by authentication and the push
notification worker, so neither subsystem depends on a credential in the
source checkout.

`REMIHUB_ADMIN_EMAILS` is a comma-separated bootstrap allowlist. A verified
Firebase identity on that list is enrolled as the first RemiHub administrator.
Other valid Firebase identities receive `403` until they have an active row in
`public.remihub_users`. Do not put service-account JSON or bearer tokens in the
repository.

## QA checklist

Use a QA database restored from a recent production backup. Store its
credentials in a separate, permission-restricted INI file with the same
`[Database]` structure as the production configuration.

From a clean source checkout on the server:

```bash
cd /opt/remihub
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m compileall -q backend tests
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m backend.database.migration_runner status --config /path/to/config.qa.ini
.venv/bin/python -m backend.database.migration_runner upgrade --config /path/to/config.qa.ini
.venv/bin/python -m backend.database.migration_runner status --config /path/to/config.qa.ini
```

Verify in QA:

1. `0001_auth_foundation` and `0002_remove_uv_alert_prototype` report
   `applied`. Migration `0002` intentionally removes the abandoned UV alert
   proof-of-concept tables and their data.
2. Existing API calls without an `Authorization` header still work in
   `transition` mode.
3. `/auth/me` returns `401` without a bearer token.
4. A valid Firebase ID token for the allowlisted email returns the new user
   record from `/auth/me`.
5. A malformed, expired, or unenrolled token is rejected.
6. Device registration without a bearer token remains compatible and leaves
   `user_id` null. Authenticated registration associates the device with its
   user.

## Production deployment gate

Do not make migration execution part of service startup yet. Keep it as an
explicit approved deployment step:

1. Back up the production database and verify the backup completed.
2. Install the added dependency in `/opt/remihub/.venv`.
3. Deploy the reviewed source with `REMIHUB_AUTH_MODE=transition`.
4. Run `status`, `upgrade`, and `status` against the production configuration.
5. Restart with `sudo systemctl restart remihub`.
6. Check `sudo systemctl status remihub --no-pager` and recent journal logs.
7. Call `GET /app-update/latest?platform=android` without a bearer token. A
   normal application response (`200` or `404`) confirms transition mode did
   not turn the request into `401`.
8. Confirm `/auth/me` still returns `401` without a bearer token.

Do not set `REMIHUB_AUTH_MODE=required` until the Android and web clients send
Firebase bearer tokens and their authenticated release has been verified.

## Rollback

The safest application rollback is to restore the previous source and
dependencies, restart RemiHub, and leave both database migrations applied.
The old application will ignore the additive authentication schema, and the UV
alert prototype was unused.

Only downgrade if there is a specific reason to recreate the removed UV table
structures. Stop RemiHub first, retain a verified backup, restore the old
application code, and then run:

```bash
.venv/bin/python -m backend.database.migration_runner downgrade --steps 1
```

This reverses only migration `0002`. It recreates empty UV alert tables; it
cannot restore the prototype data deleted by the upgrade. Reversing migration
`0001` as well would remove user associations and notification data introduced
by the authentication foundation and should not be part of a routine rollback.
