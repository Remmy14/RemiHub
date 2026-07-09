from datetime import date, datetime
from decimal import Decimal
from io import StringIO
import csv
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import time
import pandas as pd
from datetime import date
from decimal import Decimal

import yfinance as yf

import requests

from backend.services import kids_investing_service

BASE_DIR = Path(__file__).resolve().parent.parent.parent
LOG_DIR = BASE_DIR / "backend" / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logger = logging.getLogger("KidsInvestingWorker")
logger.setLevel(logging.INFO)

if not logger.handlers:
    handler = RotatingFileHandler(LOG_DIR / "kids_investing_worker.log", maxBytes=1_000_000, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)



def fetch_yfinance_latest_price(ticker: str) -> dict:
    symbol = ticker.strip().upper()

    df = yf.download(
        symbol,
        period="7d",
        interval="1d",
        progress=False,
        auto_adjust=False,
        threads=False,
    )

    # yfinance may return columns like:
    #   Close
    # or:
    #   ("Close", "SPYM")
    #
    # Normalize it to flat columns: Close, High, Low, Open, Volume, etc.
    if isinstance(df.columns, pd.MultiIndex):
        if symbol in df.columns.get_level_values(-1):
            df = df.xs(symbol, axis=1, level=-1)
        else:
            df.columns = df.columns.get_level_values(0)

    if "Close" not in df.columns:
        raise RuntimeError(f"{symbol}: Close column not found in yfinance response: {list(df.columns)}")

    if df.empty:
        raise RuntimeError(f"{symbol}: no price data returned from yfinance")

    # Drop rows where Close is missing, then use the latest trading day.
    df = df.dropna(subset=["Close"])

    if df.empty:
        raise RuntimeError(f"{symbol}: no close price returned from yfinance")

    latest_row = df.iloc[-1]
    latest_date = df.index[-1].date()
    close_price = Decimal(str(latest_row["Close"]))

    return {
        "ticker": symbol,
        "price_date": latest_date,
        "close_price": close_price,
        "source": "yfinance",
    }

def fetch_stooq_latest_close(ticker: str) -> tuple[date, Decimal] | None:
    # Stooq uses lowercase symbols and .us suffix for US-listed tickers.
    symbol = f"{ticker.lower()}.us"
    url = f"https://stooq.com/q/l/?s={symbol}&f=sd2t2ohlcv&h&e=csv"

    response = requests.get(url, timeout=20)
    response.raise_for_status()

    reader = csv.DictReader(StringIO(response.text))
    row = next(reader, None)
    if not row or row.get("Close") in {None, "N/D"} or row.get("Date") in {None, "N/D"}:
        return None

    return date.fromisoformat(row["Date"]), Decimal(row["Close"])


def refresh_prices_and_snapshot() -> dict:
    tickers = kids_investing_service.get_unique_active_tickers()
    updated = []
    errors = []

    for ticker in tickers:
        try:
            latest = fetch_yfinance_latest_price(ticker)
            if not latest:
                errors.append(f"{ticker}: no price returned")
                continue

            price_date = latest['price_date']
            close_price = latest['close_price']
            kids_investing_service.upsert_price(
                ticker=ticker,
                price_date=price_date,
                close_price=close_price,
                source=latest['source'],
            )
            updated.append({"ticker": ticker, "price_date": price_date.isoformat(), "close_price": float(close_price)})
        except Exception as exc:
            logger.exception("Failed to refresh price for %s", ticker)
            errors.append(f"{ticker}: {exc}")

    snapshot = kids_investing_service.create_daily_snapshots()
    return {"success": not errors, "updated": updated, "errors": errors, "snapshot": snapshot}


def run_kids_investing_worker():
    logger.info("Starting Kids Investing Worker")

    while True:
        try:
            now = datetime.now()
            # Run after market close, but keep it simple/idempotent: once per day snapshots upsert.
            if now.hour >= 16:
                result = refresh_prices_and_snapshot()
                logger.info(
                    "Kids investing refresh complete: updated=%s errors=%s snapshot=%s",
                    len(result["updated"]),
                    len(result["errors"]),
                    result["snapshot"],
                )
                time.sleep(60 * 60 * 23)
            else:
                time.sleep(60 * 5)
        except Exception as exc:
            logger.exception("Error running kids investing worker: %s", exc)
            time.sleep(60 * 30)


if __name__ == "__main__":
    print(refresh_prices_and_snapshot())
