# Python Imports
from decimal import Decimal, InvalidOperation
from uuid import UUID

# 3rd Party Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn

def _serialize_value(value):
    if value is None:
        return None

    if isinstance(value, UUID):
        return str(value)

    if isinstance(value, Decimal):
        return float(value)

    if hasattr(value, "isoformat"):
        return value.isoformat()

    return value


def _rows_to_dicts(cur, rows) -> list[dict]:
    columns = [desc[0] for desc in cur.description]
    return [
        {
            column: _serialize_value(value)
            for column, value in zip(columns, row)
        }
        for row in rows
    ]

def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def get_latest_finance_snapshot() -> dict | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       snapshot_month,
                       status,
                       total_assets,
                       total_liabilities,
                       net_worth,
                       notes,
                       created_at
                FROM finance_snapshots
                ORDER BY snapshot_month DESC
                LIMIT 1
                """
            )
            row = cur.fetchone()

            if not row:
                return None

            columns = [desc[0] for desc in cur.description]
            return {
                column: _serialize_value(value)
                for column, value in zip(columns, row)
            }
    finally:
        put_db_conn(conn)


def get_finance_snapshot_items(snapshot_id: str) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT source,
                       source_account_id,
                       label,
                       institution_label,
                       asset_category,
                       value,
                       confidence
                FROM finance_snapshot_items
                WHERE snapshot_id = %s
                  AND include_in_net_worth = true
                ORDER BY asset_category, institution_label, label
                """,
                (snapshot_id,),
            )
            rows = cur.fetchall()
            return _rows_to_dicts(cur, rows)
    finally:
        put_db_conn(conn)


def get_finance_summary() -> dict | None:
    snapshot = get_latest_finance_snapshot()

    if not snapshot:
        return None

    snapshot_id = snapshot["id"]
    items = get_finance_snapshot_items(snapshot_id)

    category_totals: dict[str, Decimal] = {}

    for item in items:
        category = item["asset_category"]
        category_totals.setdefault(category, Decimal("0"))
        category_totals[category] += _to_decimal(item["value"])

    categories = [
        {
            "asset_category": category,
            "value": float(_money(value)),
        }
        for category, value in sorted(category_totals.items())
    ]

    return {
        "snapshot_id": snapshot["id"],
        "snapshot_month": snapshot["snapshot_month"],
        "status": snapshot["status"],
        "total_assets": snapshot["total_assets"],
        "total_liabilities": snapshot["total_liabilities"],
        "net_worth": snapshot["net_worth"],
        "notes": snapshot["notes"],
        "items": items,
        "categories": categories,
    }


def get_finance_history(limit: int = 24) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id AS snapshot_id,
                       snapshot_month,
                       total_assets,
                       total_liabilities,
                       net_worth,
                       status
                FROM finance_snapshots
                ORDER BY snapshot_month DESC
                LIMIT %s
                """,
                (limit,),
            )
            rows = cur.fetchall()
            history = _rows_to_dicts(cur, rows)

        return list(reversed(history))
    finally:
        put_db_conn(conn)


def get_finance_accounts() -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       source,
                       source_account_id,
                       institution_label,
                       name,
                       official_name,
                       mask,
                       type,
                       subtype,
                       asset_category,
                       include_in_net_worth,
                       display_name,
                       display_order,
                       updated_at
                FROM finance_accounts
                ORDER BY institution_label, display_order, name
                """
            )
            rows = cur.fetchall()
            return _rows_to_dicts(cur, rows)
    finally:
        put_db_conn(conn)


def update_finance_account(
    *,
    account_id: str,
    include_in_net_worth: bool | None = None,
    asset_category: str | None = None,
    display_name: str | None = None,
    display_order: int | None = None,
) -> dict:
    updates = []
    values = []

    if include_in_net_worth is not None:
        updates.append("include_in_net_worth = %s")
        values.append(include_in_net_worth)

    if asset_category is not None:
        updates.append("asset_category = %s")
        values.append(asset_category)

    if display_name is not None:
        updates.append("display_name = %s")
        values.append(display_name)

    if display_order is not None:
        updates.append("display_order = %s")
        values.append(display_order)

    if not updates:
        raise ValueError("No account fields supplied for update")

    updates.append("updated_at = now()")
    values.append(account_id)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE finance_accounts
                SET {", ".join(updates)}
                WHERE id = %s
                RETURNING id,
                          source,
                          source_account_id,
                          institution_label,
                          name,
                          official_name,
                          mask,
                          type,
                          subtype,
                          asset_category,
                          include_in_net_worth,
                          display_name,
                          display_order,
                          updated_at
                """,
                tuple(values),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Finance account not found: {account_id}")

            conn.commit()

            columns = [desc[0] for desc in cur.description]
            return {
                column: _serialize_value(value)
                for column, value in zip(columns, row)
            }
    finally:
        put_db_conn(conn)
