"""Passive Agent requests. Only send_to_planning creates execution records."""

from psycopg2 import errors

from backend.services import agent_service


COLUMNS = "id, title, prompt, priority, created_by, promoted_card_id, promoted_at, created_at, updated_at"
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


def create(*, title: str, prompt: str, priority: str, created_by: str):
    _validate(title, prompt, priority)
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"""INSERT INTO agent.backlog_items (title, prompt, priority, created_by)
                    VALUES (%s, %s, %s, %s) RETURNING {COLUMNS}""",
                (title, prompt, priority, created_by),
            )
            item = _item(cur, cur.fetchone())
        conn.commit()
        return item
    except Exception:
        conn.rollback()
        raise
    finally:
        agent_service.put_db_conn(conn)


def list_active(*, order: str = "priority"):
    if order not in ("priority", "created_at"):
        raise ValueError("order must be priority or created_at")
    ordering = f"{PRIORITY_ORDER}, created_at DESC, id DESC" if order == "priority" else "created_at DESC, id DESC"
    conn = agent_service.get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(f"SELECT {COLUMNS} FROM agent.backlog_items WHERE promoted_card_id IS NULL ORDER BY {ordering}")
            return agent_service._rows_to_dicts(cur, cur.fetchall())
    finally:
        conn.rollback()
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
            _locked_draft(cur, item_id)
            cur.execute("DELETE FROM agent.backlog_items WHERE id = %s", (item_id,))
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
