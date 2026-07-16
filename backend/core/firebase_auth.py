from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any


_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SERVICE_ACCOUNT_PATH = _PROJECT_ROOT / "config" / "firebase-service-account.json"
_firebase_app = None
_firebase_lock = threading.Lock()


class FirebaseConfigurationError(RuntimeError):
    """Raised when server-side Firebase authentication is not configured."""


def _get_service_account_path() -> Path:
    configured = os.environ.get("FIREBASE_SERVICE_ACCOUNT_FILE")
    return Path(configured) if configured else _DEFAULT_SERVICE_ACCOUNT_PATH


def _check_revoked_tokens() -> bool:
    value = os.environ.get("FIREBASE_CHECK_REVOKED", "true").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _get_firebase_app():
    global _firebase_app

    if _firebase_app is not None:
        return _firebase_app

    with _firebase_lock:
        if _firebase_app is not None:
            return _firebase_app

        try:
            import firebase_admin
            from firebase_admin import credentials
        except ImportError as exc:
            raise FirebaseConfigurationError(
                "firebase-admin is not installed"
            ) from exc

        try:
            _firebase_app = firebase_admin.get_app()
            return _firebase_app
        except ValueError:
            pass

        service_account_path = _get_service_account_path()
        if not service_account_path.is_file():
            raise FirebaseConfigurationError(
                f"Firebase service account file not found: {service_account_path}"
            )

        credential = credentials.Certificate(str(service_account_path))
        _firebase_app = firebase_admin.initialize_app(credential)
        return _firebase_app


def verify_firebase_id_token(id_token: str) -> dict[str, Any]:
    if not id_token:
        raise ValueError("Firebase ID token cannot be blank")

    try:
        from firebase_admin import auth as firebase_auth
    except ImportError as exc:
        raise FirebaseConfigurationError(
            "firebase-admin is not installed"
        ) from exc

    return firebase_auth.verify_id_token(
        id_token,
        app=_get_firebase_app(),
        check_revoked=_check_revoked_tokens(),
    )
