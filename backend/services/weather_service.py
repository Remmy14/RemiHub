from datetime import datetime

from backend.database.database import get_db_conn, put_db_conn


def _weather_row_to_dict(row):
    return {
        "timestamp": row[0].isoformat(),
        "tempF": row[1],
        "humidity": row[2],
        "windSpeedMph": row[3],
        "windGustMph": row[4],
        "maxDailyGustMph": row[5],
        "windDir": row[6],
        "uv": row[7],
        "solarRadiation": row[8],
        "hourlyRainIn": row[9],
        "dailyRainIn": row[10],
        "weeklyRainIn": row[11],
        "monthlyRainIn": row[12],
        "yearlyRainIn": row[13],
        "totalRainIn": row[14],
        "indoorTempF": row[15],
        "indoorHumidity": row[16],
        "baromRelIn": row[17],
        "baromAbsIn": row[18],
        "feelsLikeF": row[19],
        "dewPointF": row[20],
        "outdoorBatteryOk": row[21],
        "lastRainAt": row[22].isoformat() if row[22] else None,
    }


def get_latest_weather_reading():
    conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
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
                    last_rain_at
                FROM weather_readings
                ORDER BY observed_at DESC
                LIMIT 1
            """)
            row = cur.fetchone()

            if row:
                return _weather_row_to_dict(row)

            return None

    finally:
        put_db_conn(conn)


def get_weather_readings_in_range(start_time: datetime, end_time: datetime) -> list[dict]:
    conn = get_db_conn()

    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
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
                    last_rain_at
                FROM weather_readings
                WHERE observed_at BETWEEN %s AND %s
                ORDER BY observed_at ASC
            """, (start_time, end_time))

            rows = cur.fetchall()
            return [_weather_row_to_dict(row) for row in rows]

    finally:
        put_db_conn(conn)