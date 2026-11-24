# Agent Backlog API

All endpoints require an administrator Firebase ID token in the ordinary
`Authorization: Bearer <token>` header. They return the existing RemiHub JSON
envelope `{ "success": true, "data": ... }`, except DELETE, which returns 204.
Authentication failures are 401 or 403. Invalid UUIDs or request shapes return
422; blank text, invalid ordering, and invalid priorities return 400 or 422;
missing items return 404; an already submitted item cannot be edited or deleted
(409). A promotion blocked by the existing single-open-card constraint is 409
and leaves the draft active.

| Operation | Endpoint | Body / query | Successful data |
| --- | --- | --- | --- |
| List active drafts | `GET /agent/backlog` | `order=custom` (default), `order=priority`, or `order=created_at` | Array of items and top-level `revision` |
| Reorder active drafts | `PUT /agent/backlog/order` | `{revision, item_ids}` containing every active item ID exactly once | Updated top-level `revision` |
| Create | `POST /agent/backlog` | `{title, prompt, priority?}` | Item; HTTP 201 |
| Retrieve | `GET /agent/backlog/{id}` | — | Item, including its complete prompt |
| Update | `PUT /agent/backlog/{id}` | `{title, prompt, priority?}` | Updated item |
| Delete draft | `DELETE /agent/backlog/{id}` | — | HTTP 204 |
| Send to Planning | `POST /agent/backlog/{id}/send-to-planning` | Empty body | `{backlog_item_id, card_id}` |

An item has `id`, `title`, `prompt`, `priority` (`High`, `Medium`, `Low`), `sort_order`,
`created_by`, `created_at`, `updated_at`, `promoted_card_id`, and `promoted_at`.
Priority defaults to `Medium`. Title and prompt are required, nonblank, and
limited to 160 and 20,000 characters respectively. The prompt is stored and
returned verbatim, including line breaks. Listing includes only unsubmitted
items. Retrieval of a known submitted item remains available for historical
linkage. A repeated promotion returns the original `card_id`, allowing a client
to recover after losing an HTTP response. Draft writes never create a card or
planning run; only the promotion endpoint does.

Custom order is shared across administrators. Existing drafts are assigned positions
in the former default display order (Priority, then newest first) on first backlog
access after migration `0029`. New drafts append at the bottom. Reorder requests
must include the revision from a list response; a stale revision or changed active
item set returns 409, so the client can reload before retrying. Reordering changes
only `sort_order`; it never changes priority or creates an Agent card or run.

The `0029` migration creates `agent.backlog_order_state`. Before enabling the new
API, RemiHub must grant the application role SELECT, INSERT, and UPDATE on that
table, plus UPDATE on `agent.backlog_items` if it does not already have it. This
grant is a RemiHub deployment step; migration SQL cannot contain role grants.
Rolling back `0029` removes custom positions, so export them before rollback if
they need to be retained.

## Deployment database privileges

The automatic migration policy prohibits role grants inside migration SQL.
After migration `0028` is applied in QA, RemiHub must grant the QA application
role access under the owning role and verify it before testing the API:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.backlog_items TO remihub_qa_app;
```

Repeat under separate production approval after applying the migration there:

```sql
GRANT SELECT, INSERT, UPDATE, DELETE ON agent.backlog_items TO remihub_app;
```

The existing `agent` schema usage grant remains in place. Verify the runtime
role's table privileges in QA before enabling the portal; without this operator
step backlog requests will fail with a database permission error. These grants
are not applied by this card or by its migration.

## Android follow-up

Add these routes to the feature's existing Retrofit `AgentService`, using the
shared Firebase-token client. Provide a Backlog list, full prompt editor,
priority selector, delete confirmation, and Send to Planning confirmation.
After promotion, navigate to `card_id` in the existing card view. Keep the
button disabled during a request and treat a repeated promotion response as
success. No Android source is changed by this card.
