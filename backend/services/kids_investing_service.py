from datetime import date
from decimal import Decimal, InvalidOperation
from uuid import UUID, uuid4

from backend.database.database import get_db_conn, put_db_conn


SHARE_PRECISION = Decimal("0.00000001")


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


def _row_to_dict(cur, row) -> dict | None:
    if not row:
        return None

    return _rows_to_dicts(cur, [row])[0]


def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def _shares(value: Decimal) -> Decimal:
    return value.quantize(SHARE_PRECISION)


def _pct(numerator: Decimal, denominator: Decimal) -> Decimal | None:
    if denominator == 0:
        return None

    return ((numerator / denominator) * Decimal("100")).quantize(Decimal("0.0001"))


def _calculate_shares(total_investment, purchase_price) -> Decimal:
    total_investment_decimal = _to_decimal(total_investment)
    purchase_price_decimal = _to_decimal(purchase_price)

    if total_investment_decimal <= 0:
        raise ValueError("total_investment must be greater than 0")

    if purchase_price_decimal <= 0:
        raise ValueError("purchase_price must be greater than 0")

    return _shares(total_investment_decimal / purchase_price_decimal)


def create_child(
    *,
    name: str,
    birthday=None,
    display_color=None,
    display_order: int = 0,
) -> dict:
    child_id = str(uuid4())

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kids_invest_children (
                    id,
                    name,
                    birthday,
                    display_color,
                    display_order
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id,
                          name,
                          birthday,
                          display_color,
                          display_order,
                          active,
                          created_at,
                          updated_at
                """,
                (child_id, name, birthday, display_color, display_order),
            )
            row = cur.fetchone()

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def get_children(*, active_only: bool = True) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if active_only:
                cur.execute(
                    """
                    SELECT id,
                           name,
                           birthday,
                           display_color,
                           display_order,
                           active,
                           created_at,
                           updated_at
                    FROM kids_invest_children
                    WHERE active = true
                    ORDER BY display_order, name
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT id,
                           name,
                           birthday,
                           display_color,
                           display_order,
                           active,
                           created_at,
                           updated_at
                    FROM kids_invest_children
                    ORDER BY active DESC, display_order, name
                    """
                )

            rows = cur.fetchall()
            return _rows_to_dicts(cur, rows)

    finally:
        put_db_conn(conn)


def update_child(child_id: str, **fields) -> dict:
    allowed = {"name", "birthday", "display_color", "display_order", "active"}

    updates = []
    values = []

    for key, value in fields.items():
        if key in allowed:
            updates.append(f"{key} = %s")
            values.append(value)

    if not updates:
        raise ValueError("No child fields supplied for update")

    updates.append("updated_at = now()")
    values.append(child_id)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE kids_invest_children
                SET {", ".join(updates)}
                WHERE id = %s
                RETURNING id,
                          name,
                          birthday,
                          display_color,
                          display_order,
                          active,
                          created_at,
                          updated_at
                """,
                tuple(values),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Child not found: {child_id}")

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def create_account(
    *,
    child_id: str,
    account_label: str,
    account_type: str = "trump_account",
    custodian: str | None = None,
    notes: str | None = None,
) -> dict:
    account_id = str(uuid4())

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kids_invest_accounts (
                    id,
                    child_id,
                    account_label,
                    account_type,
                    custodian,
                    notes
                )
                VALUES (%s, %s, %s, %s, %s, %s)
                RETURNING id,
                          child_id,
                          account_label,
                          account_type,
                          custodian,
                          notes,
                          active,
                          created_at,
                          updated_at
                """,
                (
                    account_id,
                    child_id,
                    account_label,
                    account_type,
                    custodian,
                    notes,
                ),
            )
            row = cur.fetchone()

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def get_accounts(child_id: str | None = None) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if child_id:
                cur.execute(
                    """
                    SELECT id,
                           child_id,
                           account_label,
                           account_type,
                           custodian,
                           notes,
                           active,
                           created_at,
                           updated_at
                    FROM kids_invest_accounts
                    WHERE child_id = %s
                      AND active = true
                    ORDER BY account_label
                    """,
                    (child_id,),
                )
            else:
                cur.execute(
                    """
                    SELECT id,
                           child_id,
                           account_label,
                           account_type,
                           custodian,
                           notes,
                           active,
                           created_at,
                           updated_at
                    FROM kids_invest_accounts
                    WHERE active = true
                    ORDER BY account_label
                    """
                )

            rows = cur.fetchall()
            return _rows_to_dicts(cur, rows)

    finally:
        put_db_conn(conn)


def _get_account_child_id(account_id: str) -> str:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT child_id
                FROM kids_invest_accounts
                WHERE id = %s
                  AND active = true
                """,
                (account_id,),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Active account not found: {account_id}")

            return str(row[0])

    finally:
        put_db_conn(conn)


def _get_lot_for_update(lot_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       child_id,
                       account_id,
                       ticker,
                       shares,
                       purchase_price,
                       purchase_date,
                       contribution_amount,
                       notes,
                       active,
                       created_at,
                       updated_at
                FROM kids_invest_lots
                WHERE id = %s
                """,
                (lot_id,),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Lot not found: {lot_id}")

            return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def create_lot(
    *,
    account_id: str,
    ticker: str,
    purchase_date,
    total_investment,
    purchase_price,
    notes: str | None = None,
) -> dict:
    lot_id = str(uuid4())
    child_id = _get_account_child_id(account_id)
    ticker = ticker.strip().upper()

    contribution_amount = _money(_to_decimal(total_investment))
    purchase_price_decimal = _money(_to_decimal(purchase_price))
    calculated_shares = _calculate_shares(
        contribution_amount,
        purchase_price_decimal,
    )

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kids_invest_lots (
                    id,
                    child_id,
                    account_id,
                    ticker,
                    shares,
                    purchase_price,
                    purchase_date,
                    contribution_amount,
                    notes
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING id,
                          child_id,
                          account_id,
                          ticker,
                          shares,
                          purchase_price,
                          purchase_date,
                          contribution_amount,
                          contribution_amount AS total_investment,
                          notes,
                          active,
                          created_at,
                          updated_at
                """,
                (
                    lot_id,
                    child_id,
                    account_id,
                    ticker,
                    calculated_shares,
                    purchase_price_decimal,
                    purchase_date,
                    contribution_amount,
                    notes,
                ),
            )
            row = cur.fetchone()

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def update_lot(lot_id: str, **fields) -> dict:
    existing = _get_lot_for_update(lot_id)

    allowed = {
        "ticker",
        "purchase_price",
        "purchase_date",
        "total_investment",
        "notes",
    }

    ignored_keys = set(fields) - allowed
    if ignored_keys:
        raise ValueError(f"Unsupported lot update field(s): {', '.join(sorted(ignored_keys))}")

    if not fields:
        raise ValueError("No lot fields supplied for update")

    ticker = fields.get("ticker", existing["ticker"])
    ticker = ticker.strip().upper()

    purchase_date = fields.get("purchase_date", existing["purchase_date"])

    purchase_price = _money(
        _to_decimal(fields.get("purchase_price", existing["purchase_price"]))
    )

    contribution_amount = _money(
        _to_decimal(fields.get("total_investment", existing["contribution_amount"]))
    )

    calculated_shares = _calculate_shares(
        contribution_amount,
        purchase_price,
    )

    notes = fields.get("notes", existing.get("notes"))

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kids_invest_lots
                SET ticker = %s,
                    shares = %s,
                    purchase_price = %s,
                    purchase_date = %s,
                    contribution_amount = %s,
                    notes = %s,
                    updated_at = now()
                WHERE id = %s
                RETURNING id,
                          child_id,
                          account_id,
                          ticker,
                          shares,
                          purchase_price,
                          purchase_date,
                          contribution_amount,
                          contribution_amount AS total_investment,
                          notes,
                          active,
                          created_at,
                          updated_at
                """,
                (
                    ticker,
                    calculated_shares,
                    purchase_price,
                    purchase_date,
                    contribution_amount,
                    notes,
                    lot_id,
                ),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Lot not found: {lot_id}")

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def delete_lot(lot_id: str) -> dict:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE kids_invest_lots
                SET active = false,
                    updated_at = now()
                WHERE id = %s
                RETURNING id,
                          child_id,
                          account_id,
                          ticker,
                          shares,
                          purchase_price,
                          purchase_date,
                          contribution_amount,
                          contribution_amount AS total_investment,
                          notes,
                          active,
                          created_at,
                          updated_at
                """,
                (lot_id,),
            )
            row = cur.fetchone()

            if not row:
                raise ValueError(f"Lot not found: {lot_id}")

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def upsert_price(
    *,
    ticker: str,
    price_date: date,
    close_price,
    source: str = "manual",
) -> dict:
    ticker = ticker.strip().upper()

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO kids_invest_prices (
                    ticker,
                    price_date,
                    close_price,
                    source
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (ticker, price_date)
                DO UPDATE SET close_price = EXCLUDED.close_price,
                              source = EXCLUDED.source,
                              created_at = now()
                RETURNING ticker,
                          price_date,
                          close_price,
                          source,
                          created_at
                """,
                (ticker, price_date, close_price, source),
            )
            row = cur.fetchone()

        conn.commit()
        return _row_to_dict(cur, row)

    finally:
        put_db_conn(conn)


def get_unique_active_tickers() -> list[str]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT DISTINCT ticker
                FROM kids_invest_lots
                WHERE active = true
                ORDER BY ticker
                """
            )
            return [row[0] for row in cur.fetchall()]

    finally:
        put_db_conn(conn)


def _portfolio_rows(child_id: str | None = None) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            params = []
            child_filter = ""

            if child_id:
                child_filter = "AND c.id = %s"
                params.append(child_id)

            cur.execute(
                f"""
                WITH latest_prices AS (
                    SELECT DISTINCT ON (ticker)
                           ticker,
                           price_date,
                           close_price
                    FROM kids_invest_prices
                    ORDER BY ticker, price_date DESC
                )
                SELECT c.id AS child_id,
                       c.name AS child_name,
                       c.display_color,
                       c.display_order,
                       l.id AS lot_id,
                       l.account_id,
                       a.account_label,
                       a.account_type,
                       l.ticker,
                       l.shares,
                       l.purchase_price,
                       l.purchase_date,
                       l.contribution_amount,
                       l.notes,
                       p.price_date AS current_price_date,
                       p.close_price AS current_price
                FROM kids_invest_children c
                LEFT JOIN kids_invest_lots l
                       ON l.child_id = c.id
                      AND l.active = true
                LEFT JOIN kids_invest_accounts a
                       ON a.id = l.account_id
                LEFT JOIN latest_prices p
                       ON p.ticker = l.ticker
                WHERE c.active = true
                  {child_filter}
                ORDER BY c.display_order,
                         c.name,
                         l.ticker,
                         l.purchase_date
                """,
                tuple(params),
            )
            rows = cur.fetchall()
            return _rows_to_dicts(cur, rows)

    finally:
        put_db_conn(conn)


def _enrich_lot(row: dict) -> dict | None:
    if not row.get("lot_id"):
        return None

    shares = _to_decimal(row["shares"])
    purchase_price = _to_decimal(row["purchase_price"])
    invested = _to_decimal(row.get("contribution_amount"))
    current_price = _to_decimal(row.get("current_price"))

    current_value = shares * current_price if current_price else Decimal("0")
    gain_loss = current_value - invested

    return {
        "id": row["lot_id"],
        "child_id": row["child_id"],
        "account_id": row.get("account_id"),
        "account_label": row.get("account_label"),
        "account_type": row.get("account_type"),
        "ticker": row["ticker"],
        "shares": float(shares),
        "purchase_price": float(_money(purchase_price)),
        "purchase_date": row["purchase_date"],
        "contribution_amount": float(_money(invested)),
        "total_investment": float(_money(invested)),
        "current_price": row.get("current_price"),
        "current_price_date": row.get("current_price_date"),
        "current_value": float(_money(current_value)),
        "gain_loss": float(_money(gain_loss)),
        "gain_loss_percent": float(_pct(gain_loss, invested)) if invested else None,
        "notes": row.get("notes"),
    }


def _summarize_lots(lots: list[dict]) -> dict:
    invested = sum(
        (_to_decimal(lot["total_investment"]) for lot in lots),
        Decimal("0"),
    )
    current_value = sum(
        (_to_decimal(lot["current_value"]) for lot in lots),
        Decimal("0"),
    )
    gain_loss = current_value - invested

    holdings_by_ticker: dict[str, dict] = {}

    for lot in lots:
        ticker = lot["ticker"]
        bucket = holdings_by_ticker.setdefault(
            ticker,
            {
                "ticker": ticker,
                "shares": Decimal("0"),
                "invested": Decimal("0"),
                "current_value": Decimal("0"),
                "current_price": lot.get("current_price"),
                "current_price_date": lot.get("current_price_date"),
            },
        )

        bucket["shares"] += _to_decimal(lot["shares"])
        bucket["invested"] += _to_decimal(lot["total_investment"])
        bucket["current_value"] += _to_decimal(lot["current_value"])

        if lot.get("current_price") is not None:
            bucket["current_price"] = lot.get("current_price")
            bucket["current_price_date"] = lot.get("current_price_date")

    holdings = []

    for holding in holdings_by_ticker.values():
        holding_gain = holding["current_value"] - holding["invested"]

        holdings.append(
            {
                "ticker": holding["ticker"],
                "shares": float(holding["shares"]),
                "total_shares": float(holding["shares"]),
                "invested": float(_money(holding["invested"])),
                "total_invested": float(_money(holding["invested"])),
                "current_price": holding.get("current_price"),
                "current_price_date": holding.get("current_price_date"),
                "current_value": float(_money(holding["current_value"])),
                "gain_loss": float(_money(holding_gain)),
                "gain_loss_percent": (
                    float(_pct(holding_gain, holding["invested"]))
                    if holding["invested"]
                    else None
                ),
            }
        )

    holdings.sort(key=lambda h: h["current_value"], reverse=True)

    return {
        "total_invested": float(_money(invested)),
        "current_value": float(_money(current_value)),
        "gain_loss": float(_money(gain_loss)),
        "gain_loss_percent": float(_pct(gain_loss, invested)) if invested else None,
        "holdings": holdings,
    }


def get_overview() -> dict:
    rows = _portfolio_rows()
    children: dict[str, dict] = {}

    for row in rows:
        child = children.setdefault(
            row["child_id"],
            {
                "id": row["child_id"],
                "child_id": row["child_id"],
                "name": row["child_name"],
                "display_color": row.get("display_color"),
                "display_order": row.get("display_order"),
                "lots": [],
            },
        )

        lot = _enrich_lot(row)

        if lot:
            child["lots"].append(lot)

    child_cards = []
    all_lots = []

    for child in children.values():
        summary = _summarize_lots(child["lots"])
        all_lots.extend(child["lots"])

        child_cards.append(
            {
                key: value
                for key, value in child.items()
                if key != "lots"
            }
            | summary
        )

    child_cards.sort(key=lambda c: (c.get("display_order") or 0, c["name"]))

    totals = _summarize_lots(all_lots)

    return {
        "children": child_cards,
        "totals": totals,
        **totals,
    }


def get_child_detail(child_id: str) -> dict:
    rows = _portfolio_rows(child_id)

    if not rows:
        raise ValueError(f"Child not found: {child_id}")

    first = rows[0]
    lots = [lot for row in rows if (lot := _enrich_lot(row))]
    summary = _summarize_lots(lots)

    child = {
        "id": first["child_id"],
        "name": first["child_name"],
        "display_color": first.get("display_color"),
    }

    return {
        "id": first["child_id"],
        "child_id": first["child_id"],
        "name": first["child_name"],
        "display_color": first.get("display_color"),
        "child": child,
        **summary,
        "lots": lots,
        "accounts": get_accounts(child_id),
    }


def create_daily_snapshots(*, snapshot_date: date | None = None) -> dict:
    snapshot_date = snapshot_date or date.today()
    overview = get_overview()

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            for child in overview["children"]:
                gain_pct = child.get("gain_loss_percent")

                cur.execute(
                    """
                    INSERT INTO kids_invest_daily_snapshots (
                        id,
                        snapshot_date,
                        child_id,
                        total_invested,
                        current_value,
                        gain_loss,
                        gain_loss_percent
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s)
                    ON CONFLICT (snapshot_date, child_id)
                    DO UPDATE SET total_invested = EXCLUDED.total_invested,
                                  current_value = EXCLUDED.current_value,
                                  gain_loss = EXCLUDED.gain_loss,
                                  gain_loss_percent = EXCLUDED.gain_loss_percent,
                                  created_at = now()
                    """,
                    (
                        str(uuid4()),
                        snapshot_date,
                        child["id"],
                        child["total_invested"],
                        child["current_value"],
                        child["gain_loss"],
                        gain_pct,
                    ),
                )

        conn.commit()

        return {
            "success": True,
            "snapshot_date": snapshot_date.isoformat(),
            "child_count": len(overview["children"]),
        }

    finally:
        put_db_conn(conn)


def get_history(*, child_id: str | None = None, limit: int = 365) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if child_id and child_id != "all":
                cur.execute(
                    """
                    SELECT s.snapshot_date,
                           s.child_id,
                           c.name AS child_name,
                           s.total_invested,
                           s.current_value,
                           s.gain_loss,
                           s.gain_loss_percent
                    FROM kids_invest_daily_snapshots s
                    JOIN kids_invest_children c
                         ON c.id = s.child_id
                    WHERE s.child_id = %s
                    ORDER BY s.snapshot_date DESC
                    LIMIT %s
                    """,
                    (child_id, limit),
                )
            else:
                cur.execute(
                    """
                    SELECT s.snapshot_date,
                           NULL AS child_id,
                           'All Kids' AS child_name,
                           SUM(s.total_invested) AS total_invested,
                           SUM(s.current_value) AS current_value,
                           SUM(s.gain_loss) AS gain_loss,
                           CASE WHEN SUM(s.total_invested) = 0 THEN NULL
                                ELSE (SUM(s.gain_loss) / SUM(s.total_invested)) * 100
                           END AS gain_loss_percent
                    FROM kids_invest_daily_snapshots s
                    GROUP BY s.snapshot_date
                    ORDER BY s.snapshot_date DESC
                    LIMIT %s
                    """,
                    (limit,),
                )

            rows = cur.fetchall()
            return list(reversed(_rows_to_dicts(cur, rows)))

    finally:
        put_db_conn(conn)