# Python Imports
from bs4 import BeautifulSoup
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
from dotenv import load_dotenv
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
import re
import requests
import time
import uuid
from uuid import uuid4

# 3rd Party Imports
from plaid.api import plaid_api
from plaid.api_client import ApiClient
from plaid.configuration import Configuration
from plaid.model.accounts_balance_get_request import AccountsBalanceGetRequest

from cryptography.fernet import Fernet

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.notifications.notifications import insert_notification, Notification
from backend.config import load_application_config, resolve_environment_file_path
from backend.core.runtime_paths import ensure_log_directory

# Initialize config
cfg = load_application_config()
finance_config = cfg.get('Finance', {})


# Create loggers
LOG_DIR = ensure_log_directory()

logger = logging.getLogger("FinanceWorker")
logger.setLevel(logging.INFO)

if not logger.handlers:
    log_handler = RotatingFileHandler(
        LOG_DIR / "finance_worker.log",
        maxBytes=1_000_000,
        backupCount=3,
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s: %(message)s")
    log_handler.setFormatter(formatter)
    logger.addHandler(log_handler)

PLAID_HOSTS = {
    "sandbox": "https://sandbox.plaid.com",
    "production": "https://production.plaid.com",
}

def _get_fernet() -> Fernet:
    key = os.getenv("FINANCE_TOKEN_ENCRYPTION_KEY")
    if not key:
        raise RuntimeError("FINANCE_TOKEN_ENCRYPTION_KEY is not set")
    return Fernet(key.encode())

def encrypt_access_token(access_token: str) -> str:
    return _get_fernet().encrypt(access_token.encode()).decode()

def decrypt_access_token(access_token_encrypted: str) -> str:
    return _get_fernet().decrypt(access_token_encrypted.encode()).decode()

def upsert_plaid_item(
    *,
    label: str,
    item_id: str,
    access_token: str,
    environment: str = "production",
    enabled: bool = True,
) -> dict:
    """
    Store a Plaid Item using an encrypted access token.

    This intentionally accepts the plaintext token only at the boundary,
    encrypts it immediately, and never returns it.
    """
    encrypted_token = encrypt_access_token(access_token)

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM finance_plaid_items
                WHERE item_id = %s
                """,
                (item_id,),
            )
            row = cur.fetchone()

            if row:
                plaid_item_id = row[0]
                cur.execute(
                    """
                    UPDATE finance_plaid_items
                    SET label = %s,
                        access_token_encrypted = %s,
                        environment = %s,
                        enabled = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        label,
                        encrypted_token,
                        environment,
                        enabled,
                        plaid_item_id,
                    ),
                )
            else:
                plaid_item_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO finance_plaid_items (
                        id,
                        label,
                        item_id,
                        access_token_encrypted,
                        environment,
                        enabled,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, now(), now())
                    """,
                    (
                        plaid_item_id,
                        label,
                        item_id,
                        encrypted_token,
                        environment,
                        enabled,
                    ),
                )

        conn.commit()

        return {
            "id": plaid_item_id,
            "label": label,
            "item_id": item_id,
            "environment": environment,
            "enabled": enabled,
        }
    finally:
        put_db_conn(conn)

def get_enabled_plaid_items() -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id,
                       label,
                       item_id,
                       access_token_encrypted,
                       environment,
                       enabled
                FROM finance_plaid_items
                WHERE enabled = true
                ORDER BY label
                """
            )
            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

        return [dict(zip(columns, row)) for row in rows]
    finally:
        put_db_conn(conn)

def mark_plaid_item_success(plaid_item_id: str) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE finance_plaid_items
                SET last_success_at = %s,
                    last_error = NULL,
                    updated_at = now()
                WHERE id = %s
                """,
                (datetime.now(timezone.utc), plaid_item_id),
            )
        conn.commit()
    finally:
        put_db_conn(conn)


def mark_plaid_item_error(plaid_item_id: str, error: str) -> None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE finance_plaid_items
                SET last_error = %s,
                    updated_at = now()
                WHERE id = %s
                """,
                (error[:2000], plaid_item_id),
            )
        conn.commit()
    finally:
        put_db_conn(conn)

def get_plaid_client(environment: str = "production") -> plaid_api.PlaidApi:
    client_id = os.getenv("PLAID_CLIENT_ID")
    secret = os.getenv("PLAID_SECRET")

    if not client_id:
        raise RuntimeError("PLAID_CLIENT_ID is not set")
    if not secret:
        raise RuntimeError("PLAID_SECRET is not set")

    if environment not in PLAID_HOSTS:
        raise RuntimeError(f"Unsupported Plaid environment: {environment}")

    configuration = Configuration(
        host=PLAID_HOSTS[environment],
        api_key={
            "clientId": client_id,
            "secret": secret,
        },
    )

    return plaid_api.PlaidApi(ApiClient(configuration))

def _safe_str(value) -> str | None:
    if value is None:
        return None
    return str(value)

def _parse_money_text(value: str | None) -> Decimal | None:
    if not value:
        return None

    match = re.search(r"\$?\s*([\d,]+(?:\.\d{1,2})?)", value)
    if not match:
        return None

    try:
        return Decimal(match.group(1).replace(",", ""))
    except (InvalidOperation, ValueError):
        return None

def upsert_finance_account(
    *,
    source: str,
    source_account_id: str,
    institution_label: str | None,
    name: str,
    official_name: str | None,
    mask: str | None,
    account_type: str | None,
    subtype: str | None,
    asset_category: str,
) -> dict:
    """
    Upsert a discovered financial account.

    New accounts default to include_in_net_worth=false.
    Existing include/exclude decisions are preserved.
    """
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, include_in_net_worth, display_name
                FROM finance_accounts
                WHERE source = %s
                  AND source_account_id = %s
                """,
                (source, source_account_id),
            )
            row = cur.fetchone()

            if row:
                account_id = row[0]
                include_in_net_worth = row[1]
                display_name = row[2]

                cur.execute(
                    """
                    UPDATE finance_accounts
                    SET institution_label = %s,
                        name = %s,
                        official_name = %s,
                        mask = %s,
                        type = %s,
                        subtype = %s,
                        asset_category = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        institution_label,
                        name,
                        official_name,
                        mask,
                        account_type,
                        subtype,
                        asset_category,
                        account_id,
                    ),
                )
            else:
                account_id = str(uuid.uuid4())
                include_in_net_worth = False
                display_name = name

                cur.execute(
                    """
                    INSERT INTO finance_accounts (
                        id,
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
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, false, %s, now(), now())
                    """,
                    (
                        account_id,
                        source,
                        source_account_id,
                        institution_label,
                        name,
                        official_name,
                        mask,
                        account_type,
                        subtype,
                        asset_category,
                        name,
                    ),
                )

        conn.commit()

        return {
            "id": account_id,
            "source": source,
            "source_account_id": source_account_id,
            "institution_label": institution_label,
            "name": name,
            "display_name": display_name,
            "official_name": official_name,
            "mask": mask,
            "type": account_type,
            "subtype": subtype,
            "asset_category": asset_category,
            "include_in_net_worth": include_in_net_worth,
        }
    finally:
        put_db_conn(conn)


def infer_asset_category(account_type: str | None, subtype: str | None) -> str:
    account_type = (account_type or "").lower()
    subtype = (subtype or "").lower()

    if account_type == "depository":
        return "cash"
    elif account_type == "investment":
        if subtype in {"401k", "403b", "ira", "roth", "sep ira", "simple ira"}:
            return "retirement"
        if subtype == "hsa":
            return "hsa"
        if subtype == "529":
            return "education"
        return "brokerage"
    elif account_type in ["loan", "credit"]:
        return "liability"

    return "other"

def discover_plaid_accounts() -> list[dict]:
    """
    Pull balances/accounts from every enabled Plaid Item and upsert them into finance_accounts.

    This does not create a monthly snapshot.
    This only discovers/configures accounts.
    """
    discovered_accounts = []
    plaid_items = get_enabled_plaid_items()

    for item in plaid_items:
        plaid_item_db_id = item["id"]
        label = item["label"]
        environment = item["environment"] or "production"

        try:
            client = get_plaid_client(environment)
            access_token = decrypt_access_token(item["access_token_encrypted"])

            response = client.accounts_balance_get(
                AccountsBalanceGetRequest(access_token=access_token)
            )
            response_dict = response.to_dict()

            for account in response_dict.get("accounts", []):
                balances = account.get("balances") or {}

                account_type = _safe_str(account.get("type"))
                subtype = _safe_str(account.get("subtype"))

                stored = upsert_finance_account(
                    source="plaid",
                    source_account_id=account["account_id"],
                    institution_label=label,
                    name=account.get("name") or "Unknown account",
                    official_name=account.get("official_name"),
                    mask=account.get("mask"),
                    account_type=account_type,
                    subtype=subtype,
                    asset_category=infer_asset_category(account_type, subtype),
                )

                stored["balance_current"] = balances.get("current")
                stored["balance_available"] = balances.get("available")
                stored["currency"] = (
                    balances.get("iso_currency_code")
                    or balances.get("unofficial_currency_code")
                )

                discovered_accounts.append(stored)

            mark_plaid_item_success(plaid_item_db_id)

        except Exception as exc:
            mark_plaid_item_error(plaid_item_db_id, str(exc))
            print(f"ERROR discovering Plaid accounts for {label}: {exc}")

    return discovered_accounts

def upsert_manual_asset(
    *,
    label: str,
    asset_category: str,
    current_value: float,
    as_of_date: str,
    include_in_net_worth: bool = True,
    notes: str | None = None,
) -> dict:
    """
    Upsert a manually-maintained asset by label.

    Used for things Plaid cannot currently connect:
    - HSA
    - T. Rowe Price 401k
    - home value
    - vehicle values
    """
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM finance_manual_assets
                WHERE label = %s
                """,
                (label,),
            )
            row = cur.fetchone()

            if row:
                asset_id = row[0]
                cur.execute(
                    """
                    UPDATE finance_manual_assets
                    SET asset_category = %s,
                        current_value = %s,
                        as_of_date = %s,
                        include_in_net_worth = %s,
                        notes = %s,
                        updated_at = now()
                    WHERE id = %s
                    """,
                    (
                        asset_category,
                        current_value,
                        as_of_date,
                        include_in_net_worth,
                        notes,
                        asset_id,
                    ),
                )
            else:
                asset_id = str(uuid.uuid4())
                cur.execute(
                    """
                    INSERT INTO finance_manual_assets (
                        id,
                        label,
                        asset_category,
                        current_value,
                        as_of_date,
                        include_in_net_worth,
                        notes,
                        created_at,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, now(), now())
                    """,
                    (
                        asset_id,
                        label,
                        asset_category,
                        current_value,
                        as_of_date,
                        include_in_net_worth,
                        notes,
                    ),
                )

        conn.commit()

        return {
            "id": asset_id,
            "label": label,
            "asset_category": asset_category,
            "current_value": current_value,
            "as_of_date": as_of_date,
            "include_in_net_worth": include_in_net_worth,
            "notes": notes,
        }
    finally:
        put_db_conn(conn)


def get_manual_assets(*, included_only: bool = True) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if included_only:
                cur.execute(
                    """
                    SELECT id,
                           label,
                           asset_category,
                           current_value,
                           as_of_date,
                           include_in_net_worth,
                           notes,
                           updated_at,
                           valuation_key
                    FROM finance_manual_assets
                    WHERE include_in_net_worth = true
                    ORDER BY asset_category, label
                    """
                )
            else:
                cur.execute(
                    """
                    SELECT id,
                           label,
                           asset_category,
                           current_value,
                           as_of_date,
                           include_in_net_worth,
                           notes,
                           updated_at,
                           valuation_key
                    FROM finance_manual_assets
                    ORDER BY asset_category, label
                    """
                )

            rows = cur.fetchall()
            columns = [desc[0] for desc in cur.description]

        return [dict(zip(columns, row)) for row in rows]
    finally:
        put_db_conn(conn)

def _scrape_zillow_price_text(zillow_url: str) -> str | None:
    """
    Use a real headless browser to load Zillow's rendered page.

    If Zillow blocks the request, shows a captcha, or the expected selector
    does not appear, return None so the manual Home value remains the fallback.
    """
    browser = None

    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=True)

            context = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/126.0.0.0 Safari/537.36"
                ),
                viewport={"width": 1366, "height": 900},
                locale="en-US",
                timezone_id="America/New_York",
            )

            page = context.new_page()

            page.goto(
                zillow_url,
                wait_until="domcontentloaded",
                timeout=30_000,
            )

            try:
                page.wait_for_load_state("networkidle", timeout=10_000)
            except PlaywrightTimeoutError:
                # Zillow may keep network connections open. Not fatal.
                pass

            body_text = page.locator("body").inner_text(timeout=5_000).lower()

            if "access to this page has been denied" in body_text or "captcha" in body_text:
                logger.warning("Zillow appears to have blocked the browser scrape.")
                return None

            price_locator = page.locator('[data-testid="price"]').first
            price_locator.wait_for(state="visible", timeout=15_000)

            price_text = price_locator.inner_text(timeout=5_000).strip()

            context.close()
            browser.close()

            return price_text

    except Exception as exc:
        logger.warning("Failed to scrape Zillow rendered page: %s", exc)
        try:
            if browser:
                browser.close()
        except Exception:
            pass

        return None

def get_zillow_home_values() -> list[dict]:
    """
    Pull the current Zillow displayed home value.

    If Zillow cannot be reached or parsed, return [] so the manual Home asset
    remains the fallback value for valuation_key=home_primary.
    """
    zillow_url = finance_config.get("zillow_url")

    if not zillow_url:
        logger.info("No Zillow URL configured. Using manual home value fallback.")
        return []

    price_text = _scrape_zillow_price_text(zillow_url)
    zestimate_value = _parse_money_text(price_text)

    if zestimate_value is None:
        logger.warning(
            "Could not parse Zillow home value. price_text=%r. Using manual fallback.",
            price_text,
        )
        return []

    return [
        {
            "valuation_key": "home_primary",
            "source_account_id": "home_primary",
            "label": "Home",
            "asset_category": "home",
            "value": zestimate_value,
            "confidence": "zillow",
            "raw_payload": {
                "provider": "zillow",
                "selector": '[data-testid="price"]',
                "price_text": price_text,
                "scraped_at": datetime.now(timezone.utc).isoformat(),
                "url_configured": True,
            },
        }
    ]

def _extract_kbb_private_party_value_from_widget(html: str) -> tuple[Decimal | None, dict]:
    """
    Parse KBB's embedded price advisor widget HTML/SVG.

    Expected SVG text order:
    - Private Party Range
    - $8,745 - $9,870
    - Private Party Value
    - $9,320
    """
    soup = BeautifulSoup(html, "html.parser")

    text_values = [
        node.get_text(" ", strip=True)
        for node in soup.find_all("text")
        if node.get_text(" ", strip=True)
    ]

    private_party_range_text = None
    private_party_value_text = None

    for index, text_value in enumerate(text_values):
        normalized = text_value.strip().lower()

        if normalized == "private party range" and index + 1 < len(text_values):
            private_party_range_text = text_values[index + 1]

        if normalized == "private party value" and index + 1 < len(text_values):
            private_party_value_text = text_values[index + 1]
            break

    parsed_value = _parse_money_text(private_party_value_text)

    return parsed_value, {
        "parse_mode": "private_party_value",
        "private_party_range_text": private_party_range_text,
        "private_party_value_text": private_party_value_text,
        "svg_text_values": text_values,
    }

def _fetch_kbb_widget_html(*, vehicle_label: str, kbb_widget_url: str,) -> str | None:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (X11; Linux x86_64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/126.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/svg+xml,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    try:
        response = requests.get(
            kbb_widget_url,
            headers=headers,
            timeout=20,
        )
        response.raise_for_status()
        return response.text
    except requests.RequestException as exc:
        logger.warning(
            "Failed to fetch KBB widget for %s: %s",
            vehicle_label,
            exc,
        )
        return None

def get_kbb_vehicle_values() -> list[dict]:
    """
    Pull current KBB private party vehicle values from the configured
    KBB price advisor widget URLs.

    If a vehicle cannot be fetched or parsed, no valuation item is returned
    for that vehicle, so its manual asset remains the fallback.
    """
    vehicle_configs = [
        {
            "valuation_key": "vehicle_telluride",
            "label": "2025 Kia Telluride",
            "url": finance_config.get("vehicle_kia_url"),
        },
        {
            "valuation_key": "vehicle_camry",
            "label": "2014 Toyota Camry",
            "url": finance_config.get("vehicle_camry_url"),
        },
    ]

    valuation_items = []

    for vehicle in vehicle_configs:
        valuation_key = vehicle["valuation_key"]
        label = vehicle["label"]
        kbb_widget_url = vehicle.get("url")

        if not kbb_widget_url:
            logger.info(
                "No KBB widget URL configured for %s. Using manual fallback.",
                label,
            )
            continue

        widget_html = _fetch_kbb_widget_html(
            vehicle_label=label,
            kbb_widget_url=kbb_widget_url,
        )

        if not widget_html:
            logger.warning(
                "No KBB widget HTML returned for %s. Using manual fallback.",
                label,
            )
            continue

        parsed_value, parse_metadata = _extract_kbb_private_party_value_from_widget(
            widget_html
        )

        if parsed_value is None:
            logger.warning(
                "Could not parse KBB private party value for %s. metadata=%s. Using manual fallback.",
                label,
                parse_metadata,
            )
            continue

        valuation_items.append(
            {
                "valuation_key": valuation_key,
                "source_account_id": valuation_key,
                "label": label,
                "asset_category": "vehicle",
                "value": parsed_value,
                "confidence": "kbb",
                "raw_payload": {
                    "provider": "kbb",
                    "valuation_type": "private_party",
                    "scraped_at": datetime.now(timezone.utc).isoformat(),
                    "url_configured": True,
                    **parse_metadata,
                },
            }
        )

    return valuation_items

def _to_decimal(value) -> Decimal:
    if value is None:
        return Decimal("0")

    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"))


def get_snapshot_month(target_date: date | None = None) -> date:
    target_date = target_date or date.today()
    return target_date.replace(day=1)


def _snapshot_totals(items: list[dict]) -> tuple[Decimal, Decimal, Decimal]:
    total_assets = Decimal("0")
    total_liabilities = Decimal("0")

    for item in items:
        value = _to_decimal(item["value"])
        category = (item.get("asset_category") or "").lower()

        if category == "liability":
            total_liabilities += abs(value)
        else:
            total_assets += value

    net_worth = total_assets - total_liabilities

    return (
        _money(total_assets),
        _money(total_liabilities),
        _money(net_worth),
    )


def _get_existing_snapshot_id(snapshot_month: date) -> str | None:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id
                FROM finance_snapshots
                WHERE snapshot_month = %s
                """,
                (snapshot_month,),
            )
            row = cur.fetchone()
            return row[0] if row else None
    finally:
        put_db_conn(conn)


def collect_plaid_snapshot_items() -> tuple[list[dict], list[str]]:
    """
    Pull current Plaid balances, upsert discovered accounts, and return only
    accounts explicitly included in net worth.

    New Plaid accounts discovered here still default to excluded because
    upsert_finance_account() preserves that behavior.
    """
    snapshot_items = []
    errors = []

    plaid_items = get_enabled_plaid_items()

    for item in plaid_items:
        plaid_item_db_id = item["id"]
        institution_label = item["label"]
        environment = item["environment"] or "production"

        try:
            client = get_plaid_client(environment)
            access_token = decrypt_access_token(item["access_token_encrypted"])

            response = client.accounts_balance_get(
                AccountsBalanceGetRequest(access_token=access_token)
            )
            response_dict = response.to_dict()

            for account in response_dict.get("accounts", []):
                balances = account.get("balances") or {}

                account_type = _safe_str(account.get("type"))
                subtype = _safe_str(account.get("subtype"))

                stored_account = upsert_finance_account(
                    source="plaid",
                    source_account_id=account["account_id"],
                    institution_label=institution_label,
                    name=account.get("name") or "Unknown account",
                    official_name=account.get("official_name"),
                    mask=account.get("mask"),
                    account_type=account_type,
                    subtype=subtype,
                    asset_category=infer_asset_category(account_type, subtype),
                )

                if not stored_account["include_in_net_worth"]:
                    continue

                display_label = (
                    stored_account.get("display_name")
                    or stored_account.get("name")
                    or account.get("name")
                    or "Unknown account"
                )

                snapshot_items.append(
                    {
                        "source": "plaid",
                        "source_account_id": account["account_id"],
                        "label": display_label,
                        "institution_label": institution_label,
                        "asset_category": stored_account["asset_category"],
                        "value": _money(_to_decimal(balances.get("current"))),
                        "include_in_net_worth": True,
                        "confidence": "plaid",
                        "raw_payload": {
                            "account": account,
                            "balances": balances,
                        },
                    }
                )

            mark_plaid_item_success(plaid_item_db_id)

        except Exception as exc:
            error = f"{institution_label}: {exc}"
            errors.append(error)
            mark_plaid_item_error(plaid_item_db_id, error)

    return snapshot_items, errors


def collect_manual_snapshot_items(
    *,
    excluded_valuation_keys: set[str] | None = None,
) -> list[dict]:
    excluded_valuation_keys = excluded_valuation_keys or set()
    items = []

    for asset in get_manual_assets(included_only=True):
        valuation_key = asset.get("valuation_key")

        if valuation_key and valuation_key in excluded_valuation_keys:
            logger.info(
                "Skipping manual asset %s because valuation_key %s was provided by live valuation.",
                asset["label"],
                valuation_key,
            )
            continue

        items.append(
            {
                "source": "manual",
                "source_account_id": str(asset["id"]),
                "valuation_key": valuation_key,
                "label": asset["label"],
                "institution_label": None,
                "asset_category": asset["asset_category"],
                "value": _money(_to_decimal(asset["current_value"])),
                "include_in_net_worth": True,
                "confidence": "manual",
                "raw_payload": {
                    "as_of_date": str(asset["as_of_date"]),
                    "notes": asset.get("notes"),
                    "updated_at": str(asset.get("updated_at")),
                    "valuation_key": valuation_key,
                },
            }
        )

    return items


def collect_stubbed_valuation_items() -> list[dict]:
    """
    Zillow/KBB are currently stubs. If/when they return values, this will include them.
    Manual home/vehicle assets are used as fallback when no live valuation exists.
    """
    items = []

    for value in get_zillow_home_values():
        valuation_key = value.get("valuation_key")

        items.append(
            {
                "source": "zillow",
                "source_account_id": value.get("source_account_id") or valuation_key,
                "valuation_key": valuation_key,
                "label": value["label"],
                "institution_label": None,
                "asset_category": value.get("asset_category", "home"),
                "value": _money(_to_decimal(value["value"])),
                "include_in_net_worth": True,
                "confidence": value.get("confidence", "estimate"),
                "raw_payload": value.get("raw_payload", {}),
            }
        )

    for value in get_kbb_vehicle_values():
        valuation_key = value.get("valuation_key")

        items.append(
            {
                "source": "kbb",
                "source_account_id": value.get("source_account_id") or valuation_key,
                "valuation_key": valuation_key,
                "label": value["label"],
                "institution_label": None,
                "asset_category": value.get("asset_category", "vehicle"),
                "value": _money(_to_decimal(value["value"])),
                "include_in_net_worth": True,
                "confidence": value.get("confidence", "estimate"),
                "raw_payload": value.get("raw_payload", {}),
            }
        )

    return items


def create_monthly_finance_snapshot(
    *,
    target_date: date | None = None,
    force: bool = False,
) -> dict:
    """
    Create one monthly finance snapshot.

    If force=False and a snapshot already exists for the month, this skips.
    If force=True, it replaces that month's snapshot and snapshot items.
    """
    snapshot_month = get_snapshot_month(target_date)
    existing_snapshot_id = _get_existing_snapshot_id(snapshot_month)

    if existing_snapshot_id and not force:
        return {
            "success": True,
            "skipped": True,
            "snapshot_id": existing_snapshot_id,
            "snapshot_month": snapshot_month.isoformat(),
            "message": "Snapshot already exists for this month.",
        }

    # Gather the items from various sources
    plaid_items, plaid_errors = collect_plaid_snapshot_items()
    valuation_items = collect_stubbed_valuation_items()

    # Gather the keys for the manual items
    overridden_valuation_keys = {
        item["valuation_key"]
        for item in valuation_items
        if item.get("valuation_key")
    }

    manual_items = collect_manual_snapshot_items(
        excluded_valuation_keys=overridden_valuation_keys
    )

    # Aggregate
    snapshot_items = plaid_items + manual_items + valuation_items
    total_assets, total_liabilities, net_worth = _snapshot_totals(snapshot_items)

    status = "success" if not plaid_errors else "partial_success"
    notes = None if not plaid_errors else "\n".join(plaid_errors)

    snapshot_id = str(uuid4())

    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            if existing_snapshot_id:
                cur.execute(
                    """
                    DELETE FROM finance_snapshots
                    WHERE id = %s
                    """,
                    (existing_snapshot_id,),
                )

            cur.execute(
                """
                INSERT INTO finance_snapshots (
                    id,
                    snapshot_month,
                    status,
                    total_assets,
                    total_liabilities,
                    net_worth,
                    notes,
                    created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                """,
                (
                    snapshot_id,
                    snapshot_month,
                    status,
                    total_assets,
                    total_liabilities,
                    net_worth,
                    notes,
                ),
            )

            for item in snapshot_items:
                cur.execute(
                    """
                    INSERT INTO finance_snapshot_items (
                        id,
                        snapshot_id,
                        source,
                        source_account_id,
                        valuation_key,
                        label,
                        institution_label,
                        asset_category,
                        value,
                        include_in_net_worth,
                        confidence,
                        raw_payload,
                        created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, now())
                    """,
                    (
                        str(uuid4()),
                        snapshot_id,
                        item["source"],
                        item.get("source_account_id"),
                        item.get("valuation_key"),
                        item["label"],
                        item.get("institution_label"),
                        item["asset_category"],
                        item["value"],
                        item["include_in_net_worth"],
                        item["confidence"],
                        json.dumps(item.get("raw_payload", {}), default=str),
                    ),
                )

        conn.commit()

    finally:
        put_db_conn(conn)

    return {
        "success": True,
        "skipped": False,
        "snapshot_id": snapshot_id,
        "snapshot_month": snapshot_month.isoformat(),
        "status": status,
        "item_count": len(snapshot_items),
        "plaid_item_count": len(plaid_items),
        "manual_item_count": len(manual_items),
        "valuation_item_count": len(valuation_items),
        "total_assets": float(total_assets),
        "total_liabilities": float(total_liabilities),
        "net_worth": float(net_worth),
        "errors": plaid_errors,
    }

def send_finance_snapshot_notification(result: dict) -> None:
    snapshot_month = result.get("snapshot_month")
    status = result.get("status")
    net_worth = result.get("net_worth")
    errors = result.get("errors") or []

    try:
        net_worth_text = f"${float(net_worth):,.2f}"
    except (TypeError, ValueError):
        net_worth_text = "unknown"

    if status == "partial_success":
        title = "Monthly Finance Snapshot Created with Warnings"
        body = (
            f"New snapshot for {snapshot_month} is available. "
            f"Net worth: {net_worth_text}. "
            f"{len(errors)} account update issue(s) occurred."
        )
    else:
        title = "New Monthly Finance Snapshot Available"
        body = (
            f"New snapshot for {snapshot_month} is available. "
            f"Net worth: {net_worth_text}."
        )

    notice = Notification(
        title=title,
        body=body,
        module="Finance",
    )

    logger.info("Sending finance notification: %s - %s", notice.title, notice.body)
    insert_notification(notice)

def preview_monthly_finance_snapshot_items() -> str:
    plaid_items, plaid_errors = collect_plaid_snapshot_items()
    valuation_items = collect_stubbed_valuation_items()

    overridden_valuation_keys = {
        item["valuation_key"]
        for item in valuation_items
        if item.get("valuation_key")
    }

    manual_items = collect_manual_snapshot_items(
        excluded_valuation_keys=overridden_valuation_keys
    )

    snapshot_items = plaid_items + manual_items + valuation_items
    total_assets, total_liabilities, net_worth = _snapshot_totals(snapshot_items)
    result = {
        "item_count": len(snapshot_items),
        "plaid_item_count": len(plaid_items),
        "manual_item_count": len(manual_items),
        "valuation_item_count": len(valuation_items),
        "total_assets": float(total_assets),
        "total_liabilities": float(total_liabilities),
        "net_worth": float(net_worth),
        "errors": plaid_errors,
        "items": snapshot_items,
    }
    return json.dumps(result, indent=2, default=str)

def run_finance_worker():
    """
    Long-running finance worker.

    Runs inside the RemiHub backend process. It periodically ensures that the
    current month has a finance snapshot.

    This is intentionally idempotent because create_monthly_finance_snapshot()
    skips when force=False and the snapshot already exists for the month.
    """
    logger.info("Starting Finance Worker")

    while True:
        try:
            now = datetime.now()
            logger.info("Checking finance snapshot status for %s", now.date())

            result = create_monthly_finance_snapshot(force=False)

            if result.get("skipped"):
                logger.info(
                    "Finance snapshot skipped: %s",
                    result.get("message", "snapshot already exists"),
                )
            else:
                logger.info(
                    "Finance snapshot created: month=%s status=%s net_worth=%s items=%s",
                    result.get("snapshot_month"),
                    result.get("status"),
                    result.get("net_worth"),
                    result.get("item_count"),
                )

                for error in result.get("errors") or []:
                    logger.warning("Finance snapshot partial error: %s", error)

                # Send a notification to the front end alerting them of the new snapshot
                try:
                    send_finance_snapshot_notification(result)
                except Exception as exc:
                    logger.exception("Failed to send finance snapshot notification: %s", exc)

        except Exception as exc:
            logger.exception("Error running finance worker: %s", exc)

        # Safe to check periodically because create_monthly_finance_snapshot()
        # is idempotent for the current month when force=False.
        time.sleep(60 * 60 * 6)


if __name__ == "__main__":
    load_dotenv(dotenv_path=resolve_environment_file_path(), override=False)
    print(preview_monthly_finance_snapshot_items())

