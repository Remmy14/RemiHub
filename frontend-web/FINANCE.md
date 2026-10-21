# Finance portal contract and validation

The portal reads published figures from `/finance/summary` and `/finance/history`.
Editable account records are separate from the monthly snapshots; saving an
account does not rebuild or change a published snapshot. The directory never
adds account values to snapshot values. It links configured records to snapshot
items only when both `source` and `source_account_id` match, and links manual
records by their UUID. The backend chooses KBB/Zillow valuations or manual
fallbacks when it creates a snapshot.

## API limitations

- `/finance/accounts` has no live balance, Plaid item connection status, or
  reauthorization state. No user-facing Plaid link, reconnect, or refresh API
  is exposed. The portal shows a snapshot value only for an exact match and
  labels other values unavailable. A future backend contract would need safe
  status fields and explicit operations before these controls can be added.
- `/finance/history` contains monthly totals and status, without historical
  items. Only the latest snapshot has component detail in `/finance/summary`.
  An older snapshot detail view needs a read-only backend endpoint.
- The summary provides a snapshot month, but not a successful-update timestamp.
  KBB/Zillow item responses have no valuation timestamp or `valuation_key`.
  The portal therefore cannot claim an exact refresh time or reliably link a
  provider valuation to its editable manual fallback. A future read contract
  could expose a safe valuation identity and timestamp.
- `/finance/accounts/{id}` cannot clear `display_name` or `display_order` by
  sending `null`; the editor rejects such attempts rather than claiming success.

## Deployment checks owned by RemiHub

No migration, worker change, KBB configuration change, or snapshot rewrite is
required. Before deployment, compare authorized read-only Finance API responses
and representative manual UUIDs, balances, archive flags, historical snapshot
IDs, counts, statuses, and totals in the approved validation environment. Test
anonymous, member, and admin requests in both `transition` and `required` modes.
Confirm other portal modules preserve 401 sign-in handling and show a permission
error on 403 without signing the user out. Roll back with the previous backend
and web release; no financial data rollback is part of this card.
