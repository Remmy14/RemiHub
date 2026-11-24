"""Passive Agent requests. Only send_to_planning creates execution records."""

from psycopg2 import errors

from backend.services import agent_service


COLUMNS = "id, title, prompt, priority, created_by, promoted_card_id, promoted_at, created_at, updated_at, sort_order"
PRIORITY_ORDER = "CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END"


def _item(cur, row):
    return agent_service._row_to_dict(cur, row)


def _validate(title: str, prompt: str, priority: str):
    if len(title) > 160 or len(prompt) > 20000:
        raise ValueError("title or prompt exceeds the agent card character limit")
    agent_service._required_text(title, field="title", maximum=160)
    agent_service._required_text(prompt, field="prompt", maximum=20000)
    if priority not in ("High", "Medium", "Low"):
        raise ValueError("priority must be High, Medium, or Low")


def _not_found(item_id: str):
    return agent_service.AgentCardNotFoundError(f"Backlog item not found: {item_id}")


def _locked_draft(cur, item_id: str):
    cur.execute(f"SELECT {COLUMNS} FROM agent.backlog_items WHERE id = %s FOR UPDATE", (item_id,))
    item = _item(cur, cur.fetchone())
    if item is None:
        raise _not_found(item_id)
    if item["promoted_card_id"] is not None:
        raise agent_service.AgentConflictError("Backlog item has already been submitted")
    return item


def _lock_order(cur):
    cur.execute("INSERT INTO agent.backlog_order_state (singleton) VALUES (true) ON CONFLICT DO NOTHING")
    cur.execute("SELECT revision, initialized FROM agent.backlog_order_state WHERE singleton = true FOR UPDATE")
    revision, initialized = cur.fetchone()
    if not initialized:
        cur.execute("""WITH ranked AS (
            SELECT id, row_number() OVER (
                ORDER BY CASE priority WHEN 'High' THEN 0 WHEN 'Medium' THEN 1 ELSE 2 END,
                         created_at DESC, id DESC
            ) AS position FROM agent.backlog_items WHERE promoted_card_id IS NULL
        )
        UPDATE agent.backlog_items AS item SET sort_order = ranked.position
        FROM ranked WHERE item.id = ranked.id""")
        cur.execute("UPDATE agent.backlog_order_state SET initialized = true WHERE singleton = true")
    return revision


def _bump_order(cur):
    cur.execute("UPDATE agent.backlog_order_state SET revision = revision + 1 WHERE singleton = true")


def create(*, title: str, prompt: str, priority: str, created_by: str):
    _validate(title, prompt, priority)
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_order(cur)
            cur.execute(
                f"""INSERT INTO agent.backlog_items (title, prompt, priority, created_by, sort_order)
                    VALUES (%s, %s, %s, %s,
                      (SELECT COALESCE(MAX(sort_order), 0) + 1 FROM agent.backlog_items WHERE promoted_card_id IS NULL))
                    RETURNING {COLUMNS}""",
                (title, prompt, priority, created_by),
            )
            item = _item(cur, cur.fetchone())
            _bump_order(cur)
        conn.commit()
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def list_active(*, order: str = "custom"):
    if order not in ("custom", "priority", "created_at"):
        raise ValueError("order must be custom, priority, or created_at")
    ordering = (f"{PRIORITY_ORDER}, created_at DESC, id DESC" if order == "priority"
                else "created_at DESC, id DESC" if order == "created_at" else "sort_order, id")
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            revision = _lock_order(cur)
            cur.execute(f"SELECT {COLUMNS} FROM agent.backlog_items WHERE promoted_card_id IS NULL ORDER BY {ordering}")
            result = agent_service._rows_to_dicts(cur, cur.fetchall()), revision
        conn.commit()
        return result
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def get(item_id: str):
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM agent.backlog_items WHERE id = %s", (item_id,))
            item = _item(cur, cur.fetchone())
            if item is None:
                raise _not_found(item_id)
            return item
    finally:
        conn.rollback()
        agent_service.put_db_conn(conn)


def update(item_id: str, *, title: str, prompt: str, priority: str):
    _validate(title, prompt, priority)
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            _locked_draft(cur, item_id)
            cur.execute(
                f"""UPDATE agent.backlog_items SET title = %s, prompt = %s, priority = %s,
                    updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s RETURNING {COLUMNS}""",
                (title, prompt, priority, item_id),
            )
            item = _item(cur, cur.fetchone())
        conn.commit()
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def delete(item_id: str):
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_order(cur)
            _locked_draft(cur, item_id)
            cur.execute("DELETE FROM agent.backlog_items WHERE id = %s", (item_id,))
            _bump_order(cur)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def send_to_planning(item_id: str, *, created_by: str):
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            _lock_order(cur)
            # The row lock serializes clicks and retries. A committed result is
            # visible to the next waiter, including after a lost HTTP response.
            cur.execute(f"SELECT {COLUMNS} FROM agent.backlog_items WHERE id = %s FOR UPDATE", (item_id,))
            item = _item(cur, cur.fetchone())
            if item is None:
                raise _not_found(item_id)
            if item["promoted_card_id"] is not None:
                result = {"backlog_item_id": item_id, "card_id": item["promoted_card_id"]}
            else:
                _validate(item["title"], item["prompt"], item["priority"])
                card_id = agent_service._insert_initial_card(
                    cur, title=item["title"], description=item["prompt"], created_by=created_by,
                )
                cur.execute(
                    """UPDATE agent.backlog_items
                       SET promoted_card_id = %s, promoted_at = CURRENT_TIMESTAMP,
                           updated_at = CURRENT_TIMESTAMP
                       WHERE id = %s""",
                    (card_id, item_id),
                )
                _bump_order(cur)
                result = {"backlog_item_id": item_id, "card_id": card_id}
        conn.commit()
        return result
    except errors.UniqueViolation as exc:
        conn.rollback()
        raise agent_service._unique_violation_error(exc) from exc
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def reorder(item_ids: list[str], *, revision: int):
    if len(item_ids) != len(set(item_ids)):
        raise ValueError("item_ids must not contain duplicates")
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            current_revision = _lock_order(cur)
            if revision != current_revision:
                raise agent_service.AgentConflictError("Backlog order changed; reload and try again")
            cur.execute("SELECT id FROM agent.backlog_items WHERE promoted_card_id IS NULL")
            active_ids = {str(row[0]) for row in cur.fetchall()}
            if set(item_ids) != active_ids:
                raise agent_service.AgentConflictError("Backlog items changed; reload and try again")
            for position, item_id in enumerate(item_ids, start=1):
                cur.execute("UPDATE agent.backlog_items SET sort_order = %s WHERE id = %s", (position, item_id))
            _bump_order(cur)
        conn.commit()
        return current_revision + 1
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)
