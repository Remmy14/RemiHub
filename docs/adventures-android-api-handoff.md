# Adventures V1 backend API and Android handoff

This is the maintained backend contract for the later Android card. Android must inspect the **deployed** `/openapi.json` and deployed backend commit before implementing Retrofit models; this document describes the current source candidate and is not proof of deployment. Source: `backend/routers/adventures.py`, `backend/models/adventures_models.py`, `backend/services/adventures_service.py`, `backend/services/adventures_media.py`, and `backend/database/migrations/0023_adventures_foundation.{up,down}.sql`. FastAPI generates OpenAPI at `/openapi.json`; there is no checked-in generated snapshot. Backend tests: `tests/test_adventures.py`, opt-in migration-backed `tests/test_adventures_integration.py`, and the existing `tests/` suite.

## Authentication and conventions

All 50 routes below require an active RemiHub Firebase ID token in `Authorization: Bearer <id-token>`. The router is registered in the protected list in `backend/main.py` and every endpoint additionally uses `require_current_principal`, including image bytes. Ownership comes exclusively from the principal's `remihub_users.id`; no request may supply a user ID. Foreign or absent resources respond 404, avoiding owner disclosure. JSON success responses have `{"success":true,"data":...}`. DELETE resource operations return 204 with no body except clearing the cover, which returns the updated trip. A malformed body/query/path is 422, invalid date relationships or media content is 400, referenced campground, checklist precondition, or concurrent integrity conflict is 409, missing timezone/media configuration is 503, missing/invalid credentials is 401, and inactive/unapproved users receive 403. Errors use FastAPI's `{"detail":"..."}` shape, such as `{"detail":"Campground is referenced by trips"}`. The API has no `/v1` prefix; existing backend routers use bare feature prefixes.

List envelopes have `data: {items: [...], total: integer, limit: integer, offset: integer}`. `limit` defaults to 50 and is capped at 100 for trips, campgrounds, memories and photos; activities default to 100 and cap at 200; template items default to 200 and cap at 500. `offset` is zero based. Photo IDs are UUIDs. All timestamps are ISO 8601 with timezone. All nullable fields are JSON `null` when absent. PATCH permits partial objects; omit a field to preserve it, send `null` to clear a nullable field. Ties in `display_order` are stable by ID (and album upload time); PATCH the order number to reorder. Clients must not assume contiguous order values.

## Real routes

| Method | Path | Data / behavior |
| --- | --- | --- |
| GET | `/adventures/dashboard?year=2026&upcoming_limit=20` | `Dashboard`: nullable `next_trip`, all `active`, bounded `upcoming`, `summary` |
| GET | `/adventures/calendar?month=2026-11` | Compact `CalendarEntry[]`; alternatively `start_date=YYYY-MM-DD&end_date=YYYY-MM-DD`, inclusive, at most 367 days |
| GET, POST | `/adventures/campgrounds` | Paged campgrounds / create campground |
| GET, PATCH, DELETE | `/adventures/campgrounds/{campground_id}` | Get/edit/delete; delete conflicts while any trip references it |
| GET | `/adventures/campgrounds/{campground_id}/visits` | Paged completed, noncancelled visit trips with each trip's nullable rating; overall average and recorded score count |
| GET, POST | `/adventures/trips` | Paged trips / create trip; GET filters `kind=all|upcoming|active|historical|cancelled|undated`, optional arrival `year`, `limit`, `offset` |
| GET, PATCH, DELETE | `/adventures/trips/{trip_id}` | Get/edit/delete trip; delete removes relational children and queues photo file cleanup |
| PATCH | `/adventures/trips/{trip_id}/notes` | Edit general notes via `{"notes":"..."}` or clear with `null` |
| GET, POST | `/adventures/trips/{trip_id}/activities` | Paged date/order itinerary / create |
| GET, PATCH, DELETE | `/adventures/trips/{trip_id}/activities/{activity_id}` | Read/edit date/time/order/completion or delete |
| GET | `/adventures/checklist-template` | One implicit master template per owner; `categories` and paged `items` |
| POST | `/adventures/checklist-template/items` | Add template item |
| GET, PATCH, DELETE | `/adventures/checklist-template/items/{item_id}` | Read/edit/order/delete template item |
| GET | `/adventures/trips/{trip_id}/checklist` | Snapshot, `instantiated`, counts, percent; before copying, empty and uninstantiated |
| POST | `/adventures/trips/{trip_id}/checklist/instantiate` | Copy once, atomically; subsequent requests return existing list unchanged, even if empty |
| POST | `/adventures/trips/{trip_id}/checklist/reset` | Deliberately uncheck all current trip items |
| POST | `/adventures/trips/{trip_id}/checklist/items` | Add item after instantiation |
| GET, PATCH, DELETE | `/adventures/trips/{trip_id}/checklist/items/{item_id}` | Read/edit text/category/order/notes/completion or delete |
| GET, POST | `/adventures/trips/{trip_id}/photos` | Paged album / multipart upload |
| GET, PATCH, DELETE | `/adventures/trips/{trip_id}/photos/{photo_id}` | Metadata / caption, favorite, order / delete |
| GET | `/adventures/trips/{trip_id}/photos/{photo_id}/thumbnail` | Authenticated JPEG, max 480×480; never a public static URL |
| GET | `/adventures/trips/{trip_id}/photos/{photo_id}/original` | Authenticated original-resolution normalized image, inline |
| GET | `/adventures/trips/{trip_id}/photos/{photo_id}/download` | Same image as attachment |
| PUT | `/adventures/trips/{trip_id}/cover/{photo_id}` | Select a photo from this trip as cover; returns trip |
| DELETE | `/adventures/trips/{trip_id}/cover` | Clear cover; returns trip |
| GET, PUT | `/adventures/trips/{trip_id}/journal` | Nullable journal read / partial upsert; all fields optional |
| GET, POST | `/adventures/trips/{trip_id}/memories` | Paged memories / create |
| GET, PATCH, DELETE | `/adventures/trips/{trip_id}/memories/{memory_id}` | Read/edit/delete; `photo_ids` replace links on PATCH when supplied |
| GET, PUT | `/adventures/trips/{trip_id}/rating` | Nullable per-trip rating read / partial upsert |

## Fields, state, and calendar rules

JSON date fields for trips, activities, and memories accept exact `YYYY-MM-DD` strings or null. Timestamp strings and numeric epoch values fail request validation with HTTP 422; they are never converted to calendar dates.

Trip create requires `title` (1–200 characters). Optional: `description`, `destination`, `campground_id`, paired `arrival_date` and `departure_date`, `intent`, `notes`, `campsite_number`, `confirmation_number`, `reservation_status`, `check_in_time`, `check_out_time`, `electricity`, `water_hookup`, `sewer_hookup`, `site_length_feet`, `access_notes`, `booking_website`, and `reservation_notes`. Check-in/out are local wall-clock times without timezone; no arrival/departure instant is inferred. Electricity is `50A`, `30A`, `20A`, `none`, or `unknown` (default). Water/sewer are nullable booleans. No reservation or campground is required. `intent` is stored as `Considering` (default), `Planned`, `Booked`, or `Cancelled`. Response `state` is `In Progress` while a noncancelled trip's inclusive date window contains today and `Completed` after departure, otherwise it is the stored intent. `intent` always remains available when state changes. Cancelled trips remain `Cancelled` regardless of dates. Undated trips retain their planning state. State transitions are edits to stored intent; there is no separate manual completion flag.

Trip dates are local **calendar dates**, never converted from UTC instants. Configure `REMIHUB_ADVENTURES_TIMEZONE` as an IANA timezone for the family's date boundary; the API fails with 503 if absent/invalid. Both dates are set together or both null; departure may equal arrival. `nights = departure − arrival` in days, `calendar_days = nights + 1`, `days_until_arrival = arrival − local today`, `trip_day = local today − arrival + 1` only while active. Null dates yield null derived values. Fri November 20 through Mon November 23, 2026 is three nights and four calendar days. Scheduled future trips have classification `upcoming`, current trips `active`, past trips `historical`, undated trips `undated`, and cancellations `cancelled`. Overlaps are allowed. Calendar queries return every dated trip intersecting the inclusive query period, including cancellations and month/year spans. A period with 1000 or more entries returns 409 and should be narrowed. `historical` list excludes cancellations; cancelled trips have their own filter. Upcoming sorts arrival ascending; historical sorts departure descending.

The dashboard's `next_trip` is the earliest future noncancelled trip, or null. `active` includes all overlaps. `summary` attributes each dated noncancelled trip to its **arrival year**, including a trip ending in the next year: `total_planned` counts those trips; `upcoming` counts arrivals after today; `active` counts trips covering today; `completed` counts departures before today. `planned_nights` sums nights for future/active trips, `completed_nights` for past trips; `distinct_visited_campgrounds` counts distinct nonnull campgrounds on completed trips. Cancelled/undated trips contribute to none of these year counts. `overall_average` for campground visits uses only nonnull overall ratings, and `overall_recorded_count` gives its denominator.

Campgrounds are separate owner-owned records with `name` required and nullable `address`, `city`, `state_province`, `postal_code`, `country`, `website`, `latitude`, `longitude`, `notes`. Similar names are never merged. Trip-specific reservation fields remain on the trip. A campground cannot be deleted while referenced.

Activities require `title`, with nullable `description`, `activity_date`, `activity_time`, `location`, `notes`; `display_order` defaults to 0 and `completed` to false. Unscheduled activities are valid. The master template is initially empty, with suggested categories `Before Departure`, `At Campground`, `Before Heading Home`; categories remain free text for expansion. Items require `text` and `category`, with `display_order` and optional `notes`; trip items add `completed`. Template changes never alter an instantiated trip list. Empty checklist percent is 0, not 100.

Journal fields are nullable `overall_summary`, `favorite_part`, `family_enjoyed`, `went_well`, `could_improve`, `do_differently`, `problems`, `notes`, and `would_visit_again`. Memories require `title`, with nullable `description`, `long_text`, `memory_date` and optional `photo_ids` from the same trip. Deleting a photo removes memory links. Rating fields `overall`, `campsite_quality`, `facilities`, `activities`, and `family_friendliness` independently accept 1–5 or null; `would_return` is `yes`, `no`, `undecided`, or null; `review` is nullable. No absent score is represented as zero.

## Representative wire examples

`POST /adventures/trips`:

```json
{"title":"Autumn campsite","destination":"Hocking Hills","arrival_date":"2026-11-20","departure_date":"2026-11-23","intent":"Booked","electricity":"30A","campsite_number":"B12","confirmation_number":"R-123"}
```

Trip `data` contains those fields plus `id`, nullable other fields, `campground_name`, `cover_photo_id`, `created_at`, `updated_at`, `state`, `classification`, `days_until_arrival`, `nights`, `calendar_days`, and `trip_day`. For local November 16 the derived fragment is:

```json
{"intent":"Booked","state":"Booked","classification":"upcoming","days_until_arrival":4,"nights":3,"calendar_days":4,"trip_day":null,"cover_photo_id":null}
```

`GET /adventures/dashboard?year=2026` has this shape in an empty account:

```json
{"success":true,"data":{"next_trip":null,"active":[],"upcoming":[],"summary":{"year":2026,"total_planned":0,"upcoming":0,"active":0,"completed":0,"planned_nights":0,"completed_nights":0,"distinct_visited_campgrounds":0}}}
```

`POST /adventures/checklist-template/items`:

```json
{"text":"Fill water jugs","category":"Before Departure","display_order":10,"notes":null}
```

`PATCH /adventures/trips/{trip_id}/checklist/items/{item_id}`: `{"completed":true}`. `PUT /adventures/trips/{trip_id}/rating`: `{"overall":5,"facilities":null,"would_return":"yes","review":"Quiet site"}`. `POST /adventures/trips/{trip_id}/memories`: `{"title":"First fish","memory_date":"2026-11-21","photo_ids":["<same-trip-photo-uuid>"]}`.

## Private photos and operations

The upload route rejects a multipart request with HTTP 413 as soon as its declared or streamed body exceeds `REMIHUB_ADVENTURES_MAX_PHOTO_BYTES + 65536` bytes. This limit is enforced while the request body is ingested, before FastAPI can spool an unbounded file to temporary storage. The extra 64 KiB covers multipart framing and caption; the photo itself remains subject to the smaller decoded-file limit below. The same bound applies to chunked requests and inaccurate `Content-Length` claims.

Upload uses `multipart/form-data` at `POST /adventures/trips/{trip_id}/photos`: required part `file`, optional text part `caption` (up to 4000 characters). JPEG, PNG and single-frame WebP are supported; the declared MIME must match decoded content. Default input and normalized-output limit is 15 MiB, configurable with `REMIHUB_ADVENTURES_MAX_PHOTO_BYTES` (1 KiB–100 MiB). Images over 40 million pixels or animated images are rejected. Pillow decodes and re-encodes original resolution, transposes orientation, strips EXIF and ancillary payloads, and generates a protected JPEG thumbnail. Downloaded bytes are therefore normalized originals, not byte-for-byte copies. Metadata includes `id`, `trip_id`, `original_filename`, `content_type`, `byte_size`, `uploaded_at`, `updated_at`, `caption`, `favorite`, `display_order`, `is_cover`, `thumbnail_url`, and `original_url`; it never includes filesystem paths or storage keys. Use the shared Android OkHttp Firebase interceptor for multipart **and** image GETs. Direct unauthenticated image loading will fail. Cover deletion clears the trip cover reference.

Operator prerequisite: set `REMIHUB_ADVENTURES_MEDIA_ROOT` to an existing, writable, persistent **absolute directory outside Git, the release tree, and `/tmp`**. The service creates private `originals/` and `thumbnails/` children and checks write/traversal permissions on each; file publication failures return 503. Include the root in backups together with the PostgreSQL database and test restore/recovery. Do not mount this root through `/static`, `/assets`, or a public web server. Do not enable production until the directory, service permissions, capacity, backup, and `REMIHUB_ADVENTURES_TIMEZONE` are verified. Each upload commits a cleanup intent **before** publishing bytes. A per-key PostgreSQL advisory lock protects active uploads; original and thumbnail directory entries are synced before the final photo insert and intent removal commit together. Process exits release the lock, leaving a durable intent that the bounded reconciler uses to remove unreferenced originals, thumbnails, and partial files. Reconciliation syncs directory removals before clearing the intent. Photo and trip deletion also queue cleanup in the metadata transaction. `backend/main.py` starts an in-process retry loop on startup and every five minutes; deletion also attempts immediate cleanup. Failures remain queued for retry, and an operator can invoke `backend.services.adventures_media.reconcile_cleanup()` for recovery. Retain media backups through database rollback windows. The `down` migration is destructive and cannot restore deleted trips or photos.

Migration-backed HTTP tests are opt-in and require an operator-provisioned disposable QA database with migration 0023 already applied: set `REMIHUB_ADVENTURES_QA_TESTS=1` and `REMIHUB_ADVENTURES_QA_DATABASE_URL`. They do not apply migrations. They create and remove their own QA users and use a disposable media directory. They must never be pointed at production.

## Release and deferred work

Implementation workers do not apply migrations, deploy, or assert a deployed commit. RemiHub must perform independent review, QA migration/up/down safety assessment, authentication and owner-isolation checks, actual `/openapi.json` comparison, storage/backup validation, and production approval. Record the exact approved and deployed backend commit in the card after deployment; this source candidate has no deployed-commit claim. The Android card starts only from that deployed contract. V1 excludes campground maps, live weather, expenses, routes/fuel stops, third-party booking/geocoding, advanced analytics, wishlist, automated notifications, family sharing/accounts, maintenance integration, video, and AI summaries.
