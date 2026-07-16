from __future__ import annotations

import os
from typing import Any


class UserNotAuthorizedError(RuntimeError):
    """Raised when a valid Firebase identity is not allowed into RemiHub."""


class InactiveUserError(RuntimeError):
    """Raised when a previously enrolled RemiHub user has been disabled."""


def _admin_email_allowlist() -> set[str]:
    raw = os.environ.get("REMIHUB_ADMIN_EMAILS", "")
    return {
        email.strip().lower()
        for email in raw.split(",")
        if email.strip()
    }


def _identity_from_token(decoded_token: dict[str, Any]) -> tuple[str, str, str | None]:
    firebase_uid = str(decoded_token.get("uid") or decoded_token.get("sub") or "").strip()
    email = str(decoded_token.get("email") or "").strip().lower()
    display_name_raw = decoded_token.get("name")
    display_name = str(display_name_raw).strip() if display_name_raw else None
    display_name = display_name or None

    if not firebase_uid:
        raise UserNotAuthorizedError("Firebase token has no user identifier")
    if not email or decoded_token.get("email_verified") is not True:
        raise UserNotAuthorizedError("A verified email address is required")

    return firebase_uid, email, display_name


def _row_to_user(row) -> dict[str, Any]:
    return {
        "id": row[0],
        "firebase_uid": row[1],
        "email": row[2],
        "display_name": row[3],
        "role": row[4],
        "is_active": row[5],
        "last_login_at": row[6],
        "created_at": row[7],
        "updated_at": row[8],
    }


def resolve_authenticated_user(
    decoded_token: dict[str, Any],
    conn=None,
) -> dict[str, Any]:
    firebase_uid, email, display_name = _identity_from_token(decoded_token)
    owns_connection = conn is None

    if owns_connection:
        # Imported lazily so authentication helpers can be tested and inspected
        # without opening the application database during module import.
        from backend.database.database import get_db_conn

        conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT
                    id,
                    firebase_uid,
                    email,
                    display_name,
                    role,
                    is_active,
                    last_login_at,
                    created_at,
                    updated_at
                FROM public.remihub_users
                WHERE firebase_uid = %s
                LIMIT 1;
                """,
                (firebase_uid,),
            )
            row = cur.fetchone()

            if row is None:
                if email not in _admin_email_allowlist():
                    raise UserNotAuthorizedError(
                        "Firebase user is not enrolled in RemiHub"
                    )

                cur.execute(
                    """
                    SELECT firebase_uid
                    FROM public.remihub_users
                    WHERE lower(email) = %s
                    LIMIT 1;
                    """,
                    (email,),
                )
                email_owner = cur.fetchone()
                if email_owner is not None and email_owner[0] != firebase_uid:
                    raise UserNotAuthorizedError(
                        "Email address is already assigned to another Firebase identity"
                    )

                cur.execute(
                    """
                    INSERT INTO public.remihub_users (
                        firebase_uid,
                        email,
                        display_name,
                        role,
                        is_active,
                        last_login_at
                    )
                    VALUES (%s, %s, %s, 'admin', TRUE, CURRENT_TIMESTAMP)
                    RETURNING
                        id,
                        firebase_uid,
                        email,
                        display_name,
                        role,
                        is_active,
                        last_login_at,
                        created_at,
                        updated_at;
                    """,
                    (firebase_uid, email, display_name),
                )
                row = cur.fetchone()
            else:
                if row[5] is not True:
                    raise InactiveUserError("RemiHub user is inactive")

                if email != row[2].lower():
                    cur.execute(
                        """
                        SELECT id
                        FROM public.remihub_users
                        WHERE lower(email) = %s
                          AND id <> %s
                        LIMIT 1;
                        """,
                        (email, row[0]),
                    )
                    if cur.fetchone() is not None:
                        raise UserNotAuthorizedError(
                            "Email address is already assigned to another Firebase identity"
                        )

                cur.execute(
                    """
                    UPDATE public.remihub_users
                    SET email = %s,
                        display_name = %s,
                        last_login_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND (
                          email IS DISTINCT FROM %s
                          OR display_name IS DISTINCT FROM %s
                          OR last_login_at IS NULL
                          OR last_login_at < CURRENT_TIMESTAMP - INTERVAL '15 minutes'
                      )
                    RETURNING
                        id,
                        firebase_uid,
                        email,
                        display_name,
                        role,
                        is_active,
                        last_login_at,
                        created_at,
                        updated_at;
                    """,
                    (email, display_name, row[0], email, display_name),
                )
                updated_row = cur.fetchone()
                if updated_row is not None:
                    row = updated_row

        conn.commit()
        return _row_to_user(row)
    except Exception:
        conn.rollback()
        raise
    finally:
        if owns_connection:
            from backend.database.database import put_db_conn

            put_db_conn(conn)
