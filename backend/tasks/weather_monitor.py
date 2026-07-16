# Python Imports
from datetime import datetime, timezone
import logging
from psycopg2.extras import Json
import requests
import sys
import time
from typing import Any

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
sys.path.append('M:/Q_Drive/Projects/RemiHub/')
from backend.config import load_application_config

AMBIENT_DEVICES_URL = "https://api.ambientweather.net/v1/devices"


class AmbientWeatherError(Exception):
    pass

cfg = load_application_config()
weather_cfg = cfg.get('Weather', {})

api_key = weather_cfg.get('api_key')
application_key = weather_cfg.get('api_app_key')

if not api_key or not application_key:
    raise AmbientWeatherError(
        "Missing AMBIENT_API_KEY or AMBIENT_APPLICATION_KEY environment variable"
    )

logger = logging.getLogger(__name__)

WEATHER_POLL_INTERVAL_SECONDS = 60


def _parse_ambient_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _parse_dateutc(value: int | float | None) -> datetime | None:
    if value is None:
        return None

    return datetime.fromtimestamp(value / 1000, tz=timezone.utc)


def save_weather_reading(data: dict[str, Any]) -> bool:
    observed_at = _parse_dateutc(data.get("dateutc")) or _parse_ambient_datetime(data.get("date"))

    if observed_at is None:
        raise ValueError("Weather reading did not include dateutc or date")

    last_rain_at = _parse_ambient_datetime(data.get("lastRain"))

    outdoor_battery_ok = None
    if data.get("battout") is not None:
        outdoor_battery_ok = data.get("battout") == 1

    query = """
        INSERT INTO weather_readings (
            observed_at,
            temp_f,
            humidity,
            wind_speed_mph,
            wind_gust_mph,
            max_daily_gust_mph,
            wind_dir,
            uv,
            solar_radiation,
            hourly_rain_in,
            daily_rain_in,
            weekly_rain_in,
            monthly_rain_in,
            yearly_rain_in,
            total_rain_in,
            indoor_temp_f,
            indoor_humidity,
            barom_rel_in,
            barom_abs_in,
            feels_like_f,
            dew_point_f,
            outdoor_battery_ok,
            last_rain_at,
            raw_json
        )
        VALUES (
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
        )
        ON CONFLICT (observed_at) DO NOTHING
        RETURNING id;
    """

    values = (
        observed_at,
        data.get("tempf"),
        data.get("humidity"),
        data.get("windspeedmph"),
        data.get("windgustmph"),
        data.get("maxdailygust"),
        data.get("winddir"),
        data.get("uv"),
        data.get("solarradiation"),
        data.get("hourlyrainin"),
        data.get("dailyrainin"),
        data.get("weeklyrainin"),
        data.get("monthlyrainin"),
        data.get("yearlyrainin"),
        data.get("totalrainin"),
        data.get("tempinf"),
        data.get("humidityin"),
        data.get("baromrelin"),
        data.get("baromabsin"),
        data.get("feelsLike"),
        data.get("dewPoint"),
        outdoor_battery_ok,
        last_rain_at,
        Json(data),
    )

    conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute(query, values)
            inserted = cur.fetchone() is not None

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        put_db_conn(conn)

    return inserted


def get_latest_weather_reading() -> dict[str, Any]:
    #api_key = os.getenv("AMBIENT_API_KEY")
    #application_key = os.getenv("AMBIENT_APPLICATION_KEY")

    response = requests.get(
        AMBIENT_DEVICES_URL,
        params={
            "apiKey": api_key,
            "applicationKey": application_key,
        },
        timeout=20,
    )

    response.raise_for_status()
    devices = response.json()

    if not devices:
        raise AmbientWeatherError("No Ambient Weather devices returned")

    latest_data = devices[0].get("lastData")

    if not latest_data:
        raise AmbientWeatherError("Ambient Weather device did not include lastData")

    return latest_data

def weather_polling_loop():
    while True:
        try:
            data = get_latest_weather_reading()
            inserted = save_weather_reading(data)

            if inserted:
                logger.info("Saved new weather reading: %s", data.get("date"))
            else:
                logger.debug("Weather reading already exists: %s", data.get("date"))

        except AmbientWeatherError:
            logger.exception("Ambient Weather API error")

        except Exception:
            logger.exception("Unexpected error in weather polling loop")

        time.sleep(WEATHER_POLL_INTERVAL_SECONDS)

def run_weather_monitor():
    logger.info("Starting weather polling loop")
    weather_polling_loop()


if __name__ == "__main__":
    run_weather_monitor()
