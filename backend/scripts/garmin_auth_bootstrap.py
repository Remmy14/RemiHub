from __future__ import annotations

from dotenv import load_dotenv

import getpass
import os
import sys

from backend.config import resolve_environment_file_path
from backend.services.garmin_activity_provider import (
    GARMIN_TOKENSTORE_ENV,
    bootstrap_garmin_tokenstore,
    configured_tokenstore,
)


def resolve_email() -> str:
    email = os.environ.get("GARMIN_EMAIL", "").strip()
    if email:
        return email
    return input("Garmin email: ").strip()


def prompt_mfa() -> str:
    return input("Garmin MFA code: ").strip()


def main() -> int:
    email = resolve_email()
    if not email:
        print("Garmin email is required.", file=sys.stderr)
        return 1
    try:
        tokenstore = configured_tokenstore()
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    password = getpass.getpass("Garmin password: ")
    try:
        bootstrap_garmin_tokenstore(
            email=email,
            password=password,
            tokenstore=tokenstore,
            prompt_mfa=prompt_mfa,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    print("Garmin auth bootstrap complete.")
    print(f"{GARMIN_TOKENSTORE_ENV}: {os.path.abspath(tokenstore)}")
    return 0


if __name__ == "__main__":
    _ENV_PATH = resolve_environment_file_path()
    print(f"Environment variables: {_ENV_PATH}")
    load_dotenv(dotenv_path=_ENV_PATH, override=False)
    raise SystemExit(main())
