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
| List active drafts | `GET /agent/backlog` | `order=priority` (default) or `order=created_at` | Array of items |
| Create | `POST /agent/backlog` | `{title, prompt, priority?}` | Item; HTTP 201 |
| Retrieve | `GET /agent/backlog/{id}` | — | Item, including its complete prompt |
| Update | `PUT /agent/backlog/{id}` | `{title, prompt, priority?}` | Updated item |
| Delete draft | `DELETE /agent/backlog/{id}` | — | HTTP 204 |
| Send to Planning | `POST /agent/backlog/{id}/send-to-planning` | Empty body | `{backlog_item_id, card_id}` |

An item has `id`, `title`, `prompt`, `priority` (`High`, `Medium`, `Low`),
`created_by`, `created_at`, `updated_at`, `promoted_card_id`, and `promoted_at`.
Priority defaults to `Medium`. Title and prompt are required, nonblank, and
limited to 160 and 20,000 characters respectively. The prompt is stored and
returned verbatim, including line breaks. Listing includes only unsubmitted
items. Retrieval of a known submitted item remains available for historical
linkage. A repeated promotion returns the original `card_id`, allowing a client
to recover after losing an HTTP response. Draft writes never create a card or
planning run; only the promotion endpoint does.

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
