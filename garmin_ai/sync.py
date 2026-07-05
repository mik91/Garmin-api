import time
from datetime import date, datetime, timedelta

from garminconnect import Garmin

from garmin_ai import config, db


def _get(d: dict, *path, default=None):
    """Walk a chain of possibly-missing dict keys, e.g. _get(d, 'a', 'b')."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def login() -> Garmin:
    client = Garmin(
        config.GARMIN_EMAIL,
        config.GARMIN_PASSWORD,
        prompt_mfa=lambda: input("Enter Garmin MFA code: "),
    )
    client.login(config.GARMIN_TOKENSTORE)
    return client


def _with_retry(fn, *args, retries=3, backoff=5, **kwargs):
    for attempt in range(retries):
        try:
            return fn(*args, **kwargs)
        except Exception:
            if attempt == retries - 1:
                raise
            time.sleep(backoff * (attempt + 1))


def _sync_activities(client: Garmin, conn, start: date, end: date):
    activities = _with_retry(client.get_activities_by_date, start.isoformat(), end.isoformat()) or []
    for a in activities:
        row = {
            "activity_id": a.get("activityId"),
            "start_time": a.get("startTimeLocal"),
            "type": _get(a, "activityType", "typeKey"),
            "duration_s": a.get("duration"),
            "distance_m": a.get("distance"),
            "calories": a.get("calories"),
            "avg_hr": a.get("averageHR"),
            "max_hr": a.get("maxHR"),
            "elevation_gain_m": a.get("elevationGain"),
            "raw_json": db.dumps(a),
        }
        if row["activity_id"] is not None:
            db.upsert_activity(conn, row)


def _sync_sleep(client: Garmin, conn, day: date):
    data = _with_retry(client.get_sleep_data, day.isoformat())
    if not data:
        return
    dto = _get(data, "dailySleepDTO", default={}) or {}
    row = {
        "date": day.isoformat(),
        "total_sleep_s": dto.get("sleepTimeSeconds"),
        "deep_s": dto.get("deepSleepSeconds"),
        "light_s": dto.get("lightSleepSeconds"),
        "rem_s": dto.get("remSleepSeconds"),
        "awake_s": dto.get("awakeSleepSeconds"),
        "sleep_score": _get(dto, "sleepScores", "overall", "value"),
        "resting_hr": data.get("restingHeartRate"),
        "raw_json": db.dumps(data),
    }
    db.upsert_sleep(conn, row)


def _sync_daily_stats(client: Garmin, conn, day: date):
    data = _with_retry(client.get_stats, day.isoformat())
    if not data:
        return
    row = {
        "date": day.isoformat(),
        "steps": data.get("totalSteps"),
        "resting_hr": data.get("restingHeartRate"),
        "stress_avg": data.get("averageStressLevel"),
        "body_battery_high": data.get("bodyBatteryHighestValue"),
        "body_battery_low": data.get("bodyBatteryLowestValue"),
        "calories": data.get("totalKilocalories"),
        "raw_json": db.dumps(data),
    }
    db.upsert_daily_stats(conn, row)


def _sync_body_composition(client: Garmin, conn, day: date):
    data = _with_retry(client.get_body_composition, day.isoformat())
    if not data:
        return
    entries = _get(data, "dateWeightList", default=[]) or []
    for e in entries:
        weight_g = e.get("weight")
        row = {
            "date": day.isoformat(),
            "weight_kg": (weight_g / 1000.0) if weight_g else None,
            "bmi": e.get("bmi"),
            "body_fat_pct": e.get("bodyFat"),
            "raw_json": db.dumps(e),
        }
        db.upsert_body_composition(conn, row)


def sync(days: int = 7):
    """Pull the last `days` days of data (or since the last sync, whichever is more recent)."""
    client = login()

    with db.get_connection() as conn:
        last_synced = db.get_last_synced_date(conn)
        today = date.today()
        default_start = today - timedelta(days=days)
        start = max(default_start, date.fromisoformat(last_synced)) if last_synced else default_start

        _sync_activities(client, conn, start, today)

        day = start
        while day <= today:
            _sync_sleep(client, conn, day)
            _sync_daily_stats(client, conn, day)
            _sync_body_composition(client, conn, day)
            day += timedelta(days=1)

        db.set_last_synced_date(conn, today.isoformat())

    return start, today
