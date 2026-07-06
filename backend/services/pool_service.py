# Python Imports
from collections import defaultdict
from datetime import datetime, time, timedelta
from statistics import median
from typing import Any

# 3rd Party Imports

# Local Imports
from backend.database.database import get_db_conn, put_db_conn
from backend.scripts import pool_watch_meta


DASHBOARD_GRAPH_HOURS = 72
DASHBOARD_LOOKBACK_DAYS = 21
FETCH_BUFFER_HOURS = 1
RANGE_CHANGE_HOURS = (12, 24, 48, 72)
TREND_WINDOW_HOURS = 3
TREND_WARMING_THRESHOLD_F_PER_HOUR = 0.25
TREND_COOLING_THRESHOLD_F_PER_HOUR = -0.25
DEFAULT_PEAK_TIME = time(hour=18, minute=0)


def get_latest_pool_temp():
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, inlet_temp_f, outdoor_air_temp_f, set_temp_f
                FROM pool_temperature_log
                ORDER BY timestamp DESC
                LIMIT 1
            """)
            row = cur.fetchone()
            if row:
                return _row_to_reading(row)

            return None
    finally:
        put_db_conn(conn)


def get_pool_temps_in_range(start_time: datetime, end_time: datetime) -> list[dict]:
    conn = get_db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT timestamp, inlet_temp_f, outdoor_air_temp_f, set_temp_f
                FROM pool_temperature_log
                WHERE timestamp BETWEEN %s AND %s
                ORDER BY timestamp ASC
            """, (start_time, end_time))
            rows = cur.fetchall()
            return [_row_to_reading(row) for row in rows]
    finally:
        put_db_conn(conn)


def get_pool_dashboard() -> dict | None:
    """Return the current pool dashboard data plus calculated summary metrics."""
    end_time = datetime.now()
    start_time = end_time - timedelta(days=DASHBOARD_LOOKBACK_DAYS)
    all_readings = get_pool_temps_in_range(start_time, end_time)

    if not all_readings:
        return None

    all_readings = _sort_readings(all_readings)
    latest = all_readings[-1]
    latest_timestamp = _parse_timestamp(latest["timestamp"])

    graph_start = latest_timestamp - timedelta(
        hours=DASHBOARD_GRAPH_HOURS + FETCH_BUFFER_HOURS
    )
    graph_readings = [
        reading
        for reading in all_readings
        if _parse_timestamp(reading["timestamp"]) >= graph_start
    ]

    trend = _calculate_trend(graph_readings)

    return {
        "latest": latest,
        "readings": [
            reading
            for reading in graph_readings
            if _parse_timestamp(reading["timestamp"])
            >= latest_timestamp - timedelta(hours=DASHBOARD_GRAPH_HOURS)
        ],
        "summary": {
            "rangeChanges": _calculate_range_changes(graph_readings),
            "trend": trend,
            "predictedPeak": _calculate_predicted_peak(all_readings, latest, trend),
        },
    }


def get_pool_mode() -> dict:
    summer_mode = pool_watch_meta.get_summer_mode()
    return {
        'summerMode': summer_mode
    }


def set_pool_mode(summer_mode: bool) -> dict:
    return pool_watch_meta.set_summer_mode(summer_mode)


def _row_to_reading(row: tuple[Any, Any, Any, Any]) -> dict:
    return {
        "timestamp": row[0].isoformat(),
        "inletTemp": _safe_float(row[1]),
        "airTemp": _safe_float(row[2]),
        "setTemp": _safe_float(row[3]),
    }


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    return float(value)


def _round_optional(value: float | None, digits: int = 1) -> float | None:
    if value is None:
        return None
    return round(value, digits)


def _parse_timestamp(timestamp: str | datetime) -> datetime:
    if isinstance(timestamp, datetime):
        return timestamp
    return datetime.fromisoformat(timestamp)


def _sort_readings(readings: list[dict]) -> list[dict]:
    return sorted(readings, key=lambda reading: _parse_timestamp(reading["timestamp"]))


def _calculate_range_changes(readings: list[dict]) -> dict[str, float | None]:
    readings = _sort_readings(readings)
    if not readings:
        return {f"{hours}h": None for hours in RANGE_CHANGE_HOURS}

    latest = readings[-1]
    latest_timestamp = _parse_timestamp(latest["timestamp"])
    latest_temp = _safe_float(latest.get("inletTemp"))

    if latest_temp is None:
        return {f"{hours}h": None for hours in RANGE_CHANGE_HOURS}

    changes: dict[str, float | None] = {}
    for hours in RANGE_CHANGE_HOURS:
        target_timestamp = latest_timestamp - timedelta(hours=hours)
        baseline_temp = _interpolated_value_at(
            readings=readings,
            target_timestamp=target_timestamp,
            value_key="inletTemp",
        )
        change = None if baseline_temp is None else latest_temp - baseline_temp
        changes[f"{hours}h"] = _round_optional(change)

    return changes


def _calculate_trend(readings: list[dict]) -> dict:
    readings = _sort_readings(readings)
    if not readings:
        return {
            "degreesPerHour": None,
            "label": "Unknown",
            "windowHours": TREND_WINDOW_HOURS,
            "sampleCount": 0,
        }

    latest_timestamp = _parse_timestamp(readings[-1]["timestamp"])
    trend_start = latest_timestamp - timedelta(hours=TREND_WINDOW_HOURS)
    trend_readings = [
        reading
        for reading in readings
        if _parse_timestamp(reading["timestamp"]) >= trend_start
    ]

    slope = _linear_regression_slope(trend_readings, value_key="inletTemp")
    label = _trend_label(slope)

    return {
        "degreesPerHour": _round_optional(slope, digits=2),
        "label": label,
        "windowHours": TREND_WINDOW_HOURS,
        "sampleCount": len(
            [reading for reading in trend_readings if reading.get("inletTemp") is not None]
        ),
    }


def _calculate_predicted_peak(
    all_readings: list[dict],
    latest: dict,
    trend: dict,
) -> dict:
    latest_timestamp = _parse_timestamp(latest["timestamp"])
    latest_temp = _safe_float(latest.get("inletTemp"))

    if latest_temp is None:
        return {
            "temp": None,
            "time": None,
            "label": "Unknown",
            "peakReached": False,
            "confidence": "low",
        }

    todays_readings = [
        reading
        for reading in all_readings
        if _parse_timestamp(reading["timestamp"]).date() == latest_timestamp.date()
        and reading.get("inletTemp") is not None
    ]

    if not todays_readings:
        return {
            "temp": _round_optional(latest_temp),
            "time": latest_timestamp.isoformat(),
            "label": "Predicted Peak",
            "peakReached": False,
            "confidence": "low",
        }

    todays_peak = max(todays_readings, key=lambda reading: reading["inletTemp"])
    todays_peak_temp = _safe_float(todays_peak.get("inletTemp")) or latest_temp
    todays_peak_timestamp = _parse_timestamp(todays_peak["timestamp"])

    slope = _safe_float(trend.get("degreesPerHour")) or 0.0
    hours_since_peak = (
        latest_timestamp - todays_peak_timestamp
    ).total_seconds() / 3600

    peak_likely_reached = (
        hours_since_peak >= 1.0
        and latest_temp <= todays_peak_temp - 0.2
        and slope <= 0.0
    ) or (
        latest_timestamp.time() >= time(hour=19, minute=0)
        and slope <= 0.1
    )

    if peak_likely_reached:
        return {
            "temp": _round_optional(todays_peak_temp),
            "time": todays_peak_timestamp.isoformat(),
            "label": "Peak Reached",
            "peakReached": True,
            "confidence": "medium",
        }

    historical_gain = _historical_remaining_gain(
        all_readings=all_readings,
        comparison_timestamp=latest_timestamp,
    )
    default_peak_timestamp = datetime.combine(
        latest_timestamp.date(),
        DEFAULT_PEAK_TIME,
        tzinfo=latest_timestamp.tzinfo,
    )
    remaining_hours = max(
        0.0,
        (default_peak_timestamp - latest_timestamp).total_seconds() / 3600,
    )
    trend_remaining_gain = max(0.0, slope) * remaining_hours

    if historical_gain["sampleCount"] >= 2:
        # Mostly trust the pool's own history, but let today's current trend nudge it.
        remaining_gain = (historical_gain["medianRemainingGain"] * 0.7) + (
            trend_remaining_gain * 0.3
        )
    else:
        remaining_gain = trend_remaining_gain

    remaining_gain = _apply_air_temp_adjustment(remaining_gain, latest)
    remaining_gain = max(0.0, min(remaining_gain, 6.0))

    predicted_temp = max(latest_temp + remaining_gain, todays_peak_temp)
    predicted_time = _predicted_peak_time(
        historical_peak_minutes=historical_gain["medianPeakMinute"],
        latest_timestamp=latest_timestamp,
        fallback_timestamp=default_peak_timestamp,
    )

    confidence = "medium" if historical_gain["sampleCount"] >= 3 else "low"

    return {
        "temp": _round_optional(predicted_temp),
        "time": predicted_time.isoformat(),
        "label": "Predicted Peak",
        "peakReached": False,
        "confidence": confidence,
        "historicalSampleCount": historical_gain["sampleCount"],
    }


def _interpolated_value_at(
    readings: list[dict],
    target_timestamp: datetime,
    value_key: str,
) -> float | None:
    valid_points = [
        (_parse_timestamp(reading["timestamp"]), _safe_float(reading.get(value_key)))
        for reading in readings
        if reading.get(value_key) is not None
    ]

    if not valid_points:
        return None

    if target_timestamp <= valid_points[0][0]:
        return valid_points[0][1]

    if target_timestamp >= valid_points[-1][0]:
        return valid_points[-1][1]

    for index in range(1, len(valid_points)):
        previous_timestamp, previous_value = valid_points[index - 1]
        next_timestamp, next_value = valid_points[index]

        if previous_timestamp <= target_timestamp <= next_timestamp:
            if previous_value is None or next_value is None:
                return None

            total_seconds = (next_timestamp - previous_timestamp).total_seconds()
            if total_seconds == 0:
                return previous_value

            target_seconds = (target_timestamp - previous_timestamp).total_seconds()
            ratio = target_seconds / total_seconds
            return previous_value + ((next_value - previous_value) * ratio)

    return None


def _linear_regression_slope(readings: list[dict], value_key: str) -> float | None:
    points: list[tuple[float, float]] = []
    valid_readings = [
        reading for reading in _sort_readings(readings) if reading.get(value_key) is not None
    ]

    if len(valid_readings) < 2:
        return None

    start_timestamp = _parse_timestamp(valid_readings[0]["timestamp"])
    for reading in valid_readings:
        timestamp = _parse_timestamp(reading["timestamp"])
        hours_since_start = (timestamp - start_timestamp).total_seconds() / 3600
        points.append((hours_since_start, _safe_float(reading[value_key]) or 0.0))

    count = len(points)
    sum_x = sum(point[0] for point in points)
    sum_y = sum(point[1] for point in points)
    sum_x_squared = sum(point[0] ** 2 for point in points)
    sum_xy = sum(point[0] * point[1] for point in points)

    denominator = (count * sum_x_squared) - (sum_x ** 2)
    if denominator == 0:
        return None

    return ((count * sum_xy) - (sum_x * sum_y)) / denominator


def _trend_label(slope: float | None) -> str:
    if slope is None:
        return "Unknown"
    if slope >= TREND_WARMING_THRESHOLD_F_PER_HOUR:
        return "Warming"
    if slope <= TREND_COOLING_THRESHOLD_F_PER_HOUR:
        return "Cooling"
    return "Stable"


def _historical_remaining_gain(
    all_readings: list[dict],
    comparison_timestamp: datetime,
) -> dict:
    readings_by_date: dict[Any, list[dict]] = defaultdict(list)
    for reading in _sort_readings(all_readings):
        timestamp = _parse_timestamp(reading["timestamp"])
        if timestamp.date() == comparison_timestamp.date():
            continue
        if reading.get("inletTemp") is None:
            continue
        readings_by_date[timestamp.date()].append(reading)

    remaining_gains: list[float] = []
    peak_minutes: list[int] = []

    for day_readings in readings_by_date.values():
        comparison_reading = _nearest_reading_by_time_of_day(
            readings=day_readings,
            target_timestamp=comparison_timestamp,
            tolerance_minutes=45,
        )
        if comparison_reading is None:
            continue

        comparison_reading_timestamp = _parse_timestamp(comparison_reading["timestamp"])
        future_readings = [
            reading
            for reading in day_readings
            if _parse_timestamp(reading["timestamp"]) >= comparison_reading_timestamp
        ]
        if not future_readings:
            continue

        comparison_temp = _safe_float(comparison_reading.get("inletTemp"))
        peak_reading = max(future_readings, key=lambda reading: reading["inletTemp"])
        peak_temp = _safe_float(peak_reading.get("inletTemp"))

        if comparison_temp is None or peak_temp is None:
            continue

        remaining_gains.append(max(0.0, peak_temp - comparison_temp))
        peak_timestamp = _parse_timestamp(peak_reading["timestamp"])
        peak_minutes.append((peak_timestamp.hour * 60) + peak_timestamp.minute)

    return {
        "medianRemainingGain": median(remaining_gains) if remaining_gains else 0.0,
        "medianPeakMinute": int(median(peak_minutes)) if peak_minutes else None,
        "sampleCount": len(remaining_gains),
    }


def _nearest_reading_by_time_of_day(
    readings: list[dict],
    target_timestamp: datetime,
    tolerance_minutes: int,
) -> dict | None:
    target_minutes = (target_timestamp.hour * 60) + target_timestamp.minute
    nearest_reading = None
    nearest_delta = None

    for reading in readings:
        timestamp = _parse_timestamp(reading["timestamp"])
        reading_minutes = (timestamp.hour * 60) + timestamp.minute
        delta = abs(reading_minutes - target_minutes)

        if nearest_delta is None or delta < nearest_delta:
            nearest_delta = delta
            nearest_reading = reading

    if nearest_delta is None or nearest_delta > tolerance_minutes:
        return None

    return nearest_reading


def _apply_air_temp_adjustment(remaining_gain: float, latest: dict) -> float:
    air_temp = _safe_float(latest.get("airTemp"))
    water_temp = _safe_float(latest.get("inletTemp"))

    if air_temp is None or water_temp is None or remaining_gain <= 0:
        return remaining_gain

    air_delta = air_temp - water_temp
    if air_delta >= 10:
        return remaining_gain * 1.15
    if air_delta <= -5:
        return remaining_gain * 0.75

    return remaining_gain


def _predicted_peak_time(
    historical_peak_minutes: int | None,
    latest_timestamp: datetime,
    fallback_timestamp: datetime,
) -> datetime:
    if historical_peak_minutes is not None:
        predicted_time = datetime.combine(
            latest_timestamp.date(),
            time(
                hour=historical_peak_minutes // 60,
                minute=historical_peak_minutes % 60,
            ),
            tzinfo=latest_timestamp.tzinfo,
        )
        if predicted_time >= latest_timestamp:
            return predicted_time

    if fallback_timestamp >= latest_timestamp:
        return fallback_timestamp

    return latest_timestamp