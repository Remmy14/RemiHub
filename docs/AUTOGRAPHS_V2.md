# Autographs V2 backend contract and operator rollout

All routes are under `/autographs` and require the existing authenticated application principal. The backend retains the five installed-Android routes: `POST /add`, `GET /all`, `POST /uploadImage`, `GET /image/{filename}`, and `DELETE /{autograph_id}`. Their request and response JSON remains unchanged. `GET /all` returns only the 2026 helmet. The legacy `driver_name` and `image_path` fields remain non-null strings; `image_path: ""` means no photo. Legacy creation uses the 2026 helmet and accepts `driver_name`. The original helmet has `uses_legacy_images: true`; its four bundled Android drawables need no server upload.

## V2 routes

- `GET /helmets`: helmets ordered by descending `year`, then ID.
- `POST /helmets`: `{year, display_name, description?}`; returns helmet with UUID `id`, `uses_legacy_images`, and `created_at` (201). A duplicate year returns 409. New helmets are empty and have no images.
- `GET /helmets/{helmet_id}`: helmet details (404 if absent).
- `GET /helmets/{helmet_id}/autographs`: individual signatures ordered by descending `created_at`, then ID.
- `POST /helmets/{helmet_id}/autographs`: `{driver_id | driver_name, helmet_view, x_percent, y_percent, image_path?, date_signed?, region?, notes?}`; exactly one driver selector is required. The view is one of `front`, `right`, `back`, `left`; coordinates are finite numbers in `[0,1]`. `date_signed` is an ISO calendar date or null. The returned row includes `id`, `helmet_id`, `driver_id`, canonical `driver_name`, `image_path` as a non-null string, all signature fields, and `created_at`. `image_path` must refer to an existing uploaded `/autographs/image/{uuid}.{jpg|png|webp}` or be omitted/null/empty. Multiple signatures by one driver are allowed. Unknown helmet/driver returns 404; validation errors return 422.
- `GET /helmets/{helmet_id}/stats`: `{total_signatures, unique_signers}`.
- `GET /stats`: `{total_helmets, total_signatures, unique_signers}`.
- `GET /drivers?search=...`: alphabetic driver list, with optional case and whitespace normalized substring search.
- `GET /drivers/{driver_id}`: canonical identity.
- `PATCH /drivers/{driver_id}`: `{display_name}`; conflicting normalized name returns 409.
- `POST /drivers/{target_driver_id}/merge`: `{source_driver_id}`; every source autograph moves to the target and the source identity is removed. Missing driver returns 404.
- `GET /drivers/{driver_id}/history`: all signatures ordered by descending helmet year, then descending creation time and autograph ID. Each item includes helmet UUID/year/name, autograph and driver UUIDs, view, coordinates, `image_url` (null without a photo), optional signing date, region, and notes. Select `helmet_id` and `id` to navigate and highlight.
- `GET /helmets/{helmet_id}/images`: available view-image metadata. An empty list is valid.
- `PUT /helmets/{helmet_id}/images/{view}`: multipart `file` upload or replacement (JPEG, PNG, WebP; 5 MB). Returns metadata including authenticated `image_url`. Returns 409 if that helmet view has any signatures; replacement cannot silently invalidate coordinates.
- `GET /helmets/{helmet_id}/images/{view}`: metadata and URL (404 if unavailable). Fetch image bytes through the existing authenticated `/image/{filename}` route. A new Android image loader must send the Firebase ID token when requesting image bytes.

Timestamps are ISO 8601 in FastAPI JSON. Nullable V2 fields are JSON `null`; the legacy fields required by the installed Gson model stay non-null. Helmet and driver IDs are UUIDs; existing autograph IDs stay integers.

## Operator sequence

The verified baseline is 31 rows, 30 names after case and whitespace normalization, 29 nonblank photo references, two blank photo references, and no invalid coordinates. The owner confirmed every existing row belongs to the 2026 helmet. Creation timestamps are not signing dates.

1. Back up the database and autograph media under the operator recovery procedure. Run `python -m backend.scripts.verify_autographs_v2 --config <QA-config> --phase before` with the approved read-only QA account and media mount. Keep its `legacy_columns_sha256`. Run the same inspection as the effective backend service account to confirm media read and write permissions. The script checks each exact referenced filename; the count of 29 files alone is insufficient.
2. Apply `0024_autographs_v2.up.sql` through RemiHub's operator-controlled migration runner on QA. Its schema is additive and leaves old Android/API behavior valid. The automatic deployment validator forbids data-changing up migrations.
3. Run `python -m backend.scripts.autographs_v2_backfill --config <QA-config>` as one operator-controlled PostgreSQL transaction on QA. It is guarded for the confirmed 31-row baseline and aborts on conflicting data. Reexecution before V2 writes is safe. It also installs a synchronous insert trigger that associates writes from the old backend with the 2026 helmet and a normalized driver during the cutover; V2 writes pass through unchanged. After installing the trigger, the same transaction adds and validates a database check requiring both associations on every row, including rows changed by `UPDATE`. Run the audit with `--phase after --expected-sha <before digest>` and verify 31 original-helmet rows, 30 linked driver identities, zero missing links, a validated association constraint, unchanged legacy-column digest, and exact photo integrity. Test a new empty 2027 helmet, cross-year signing, history, statistics, image upload/replace guard, deletion, and the installed Android calls against QA.
4. Independent review and QA approve the exact candidate. Repeat the backup, pre-audit, schema migration, backfill, and post-audit in production **before** application promotion. RemiHub owns these operations and the subsequent deployment approval. If any audit fails, stop and report discrepancies; do not infer data or continue promotion.

Do not automatically downgrade after V2 writes. An application rollback can keep the additive schema and old `driver_name`/`image_path` columns. Schema/data rollback needs operator review and a verified backup because dropping the V2 columns would discard new relationships. Before any operator-approved schema downgrade after backfill, remove the compatibility trigger and function explicitly; the automatic down migration is intended only for an unused schema.
