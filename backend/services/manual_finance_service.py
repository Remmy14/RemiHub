"""Current manual finance accounts; historical snapshots are never rewritten here."""

from decimal import Decimal
from uuid import UUID, uuid4

from psycopg2 import errorcodes

from backend.database.database import get_db_conn, put_db_conn
from backend.models.finance_models import ManualAccountCreate, ManualAccountPatch


class AccountConflict(Exception):
    pass


class AccountNotFound(Exception):
    pass


class AccountInvalid(Exception):
    pass


SELECT_FIELDS = """
    id, label, classification, asset_category, current_value,
    as_of_date, updated_at, include_in_net_worth, notes, active, valuation_key
"""

# Provider identities already used by the monthly worker. New manual accounts
# cannot claim them without the protected fallback's valuation_key.
RESERVED_NAMES = frozenset({
    "home", "primary residence", "primary residence valuation",
    "2025 kia telluride", "2026 ram 2500 big horn", "ram 2500",
})


def _account(cursor, row):
    fields = [description[0] for description in cursor.description]
    record = dict(zip(fields, row))
    return {
        "id": str(record["id"]),
        "name": record["label"],
        "classification": record["classification"],
        "asset_category": record["asset_category"],
        "current_balance": format(Decimal(str(record["current_value"])), ".2f"),
        "as_of_date": record["as_of_date"].isoformat(),
        "updated_at": record["updated_at"].isoformat(),
        "include_in_net_worth": record["include_in_net_worth"],
        "notes": record["notes"],
        "active": record["active"],
    }


def _conflict(exc):
    if getattr(exc, "pgcode", None) == errorcodes.UNIQUE_VIOLATION:
        raise AccountConflict("An account with this name or valuation key already exists") from exc
    raise exc


def list_accounts(*, include_archived: bool = False) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {SELECT_FIELDS} FROM public.finance_manual_assets "
                "WHERE (%s OR active) ORDER BY lower(label), id",
                (include_archived,),
            )
            return [_account(cur, row) for row in cur.fetchall()]
    finally:
        put_db_conn(conn)


def get_account(account_id: UUID) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT {SELECT_FIELDS} FROM public.finance_manual_assets WHERE id = %s",
                (str(account_id),),
            )
            row = cur.fetchone()
            if row is None:
                raise AccountNotFound("Manual account not found")
            return _account(cur, row)
    finally:
        put_db_conn(conn)


def create_account(request: ManualAccountCreate) -> dict:
    if request.name.casefold() in RESERVED_NAMES:
        raise AccountConflict("This name is reserved for an automated valuation fallback")
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO public.finance_manual_assets
                    (id, label, asset_category, current_value, as_of_date,
                     include_in_net_worth, notes, active, created_at, updated_at)
                    VALUES (%s, %s, %s, %s, %s, %s, %s, true, now(), now())
                    RETURNING {SELECT_FIELDS}""",
                (str(uuid4()), request.name, request.asset_category,
                 Decimal(request.current_balance), request.as_of_date,
                 request.include_in_net_worth, request.notes),
            )
            result = _account(cur, cur.fetchone())
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        _conflict(exc)
    finally:
        put_db_conn(conn)


def update_account(account_id: UUID, request: ManualAccountPatch) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT label, asset_category, valuation_key FROM public.finance_manual_assets "
                "WHERE id = %s FOR UPDATE",
                (str(account_id),),
            )
            stored = cur.fetchone()
            if stored is None:
                raise AccountNotFound("Manual account not found")
            old_name, old_category, valuation_key = stored
            values = request.model_dump(exclude_unset=True)
            category = values.get("asset_category", old_category)
            classification = values.pop("classification", None)
            if classification == "liability" and "asset_category" not in values:
                category = "liability"
                values["asset_category"] = category
            if classification == "asset" and category == "liability":
                raise AccountInvalid("an asset classification requires a non-liability asset_category")
            if (classification == "liability" and category != "liability") or (
                classification == "asset" and category == "liability"
            ):
                raise AccountInvalid("classification and asset_category are contradictory")
            if valuation_key and category != old_category:
                raise AccountConflict("A valuation fallback's category cannot be changed")
            name = values.get("name", old_name)
            if name.casefold() in RESERVED_NAMES and name.casefold() != old_name.casefold():
                raise AccountConflict("This name is reserved for an automated valuation fallback")
            if "name" in values:
                values["label"] = values.pop("name")
            if "current_balance" in values:
                values["current_value"] = Decimal(values.pop("current_balance"))
            if values:
                columns = ", ".join(f"{field} = %s" for field in values)
                cur.execute(
                    f"UPDATE public.finance_manual_assets SET {columns}, updated_at = now() "
                    f"WHERE id = %s RETURNING {SELECT_FIELDS}",
                    (*values.values(), str(account_id)),
                )
            else:
                cur.execute(
                    f"SELECT {SELECT_FIELDS} FROM public.finance_manual_assets WHERE id = %s",
                    (str(account_id),),
                )
            result = _account(cur, cur.fetchone())
        conn.commit()
        return result
    except Exception as exc:
        conn.rollback()
        _conflict(exc)
    finally:
        put_db_conn(conn)
