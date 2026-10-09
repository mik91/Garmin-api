import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta

from garminconnect import Garmin

from garmin_ai import calendar as training_calendar
from garmin_ai import config, db


@contextmanager
def _sync_guard(timeout_seconds: int = 600):
    """Serialize syncs across Streamlit sessions and separate processes."""
    lock_path = f"{config.DB_PATH}.sync-lock"
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    lock_conn = sqlite3.connect(lock_path, timeout=timeout_seconds)
    try:
        lock_conn.execute(f"PRAGMA busy_timeout = {timeout_seconds * 1000}")
        lock_conn.execute(
            "CREATE TABLE IF NOT EXISTS sync_guard (id INTEGER PRIMARY KEY)"
        )
        lock_conn.commit()
        lock_conn.execute("BEGIN EXCLUSIVE")
        yield
    finally:
        lock_conn.rollback()
        lock_conn.close()


def _today_sync_covers(conn, days: int, today: date) -> bool:
    state = db.get_sync_state(conn)
    try:
        synced_days = int(state.get("last_sync_days", "0"))
    except (TypeError, ValueError):
        synced_days = 0
    return state.get("last_synced_date") == today.isoformat() and synced_days >= days


def friendly_error(error: Exception) -> str:
    """Turn nested Garmin/network exceptions into useful UI guidance."""
    messages = []
    current = error
    seen = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        messages.append(str(current))
        current = current.__cause__ or current.__context__

    detail = " ".join(messages).lower()
    if "winerror 10013" in detail or "forbidden by its access permissions" in detail:
        return (
            "Network access to Garmin was blocked by the app runner or Windows. "
            "Launch the dashboard with start.ps1 outside a restricted terminal; "
            "if it still fails, allow Python through Windows Firewall."
        )
    if any(
        phrase in detail
        for phrase in (
            "failed to establish a new connection",
            "name or service not known",
            "getaddrinfo failed",
            "connection timed out",
            "connecttimeouterror",
        )
    ):
        return "Garmin Connect could not be reached. Check your internet connection and try again."
    return str(error)


def _get(d: dict, *path, default=None):
    """Walk a chain of possibly-missing dict keys, e.g. _get(d, 'a', 'b')."""
    cur = d
    for key in path:
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur if cur is not None else default


def login(prompt_mfa=None) -> Garmin:
    client = Garmin(
        config.GARMIN_EMAIL,
        config.GARMIN_PASSWORD,
        prompt_mfa=prompt_mfa or (lambda: input("Enter Garmin MFA code: ")),
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


def _optional_call(fn, *args, **kwargs):
    """Return optional Garmin metrics without aborting the core daily sync."""
    try:
        return fn(*args, **kwargs)
    except Exception:
        return None


def _primary_device_record(mapping) -> dict:
    if not isinstance(mapping, dict):
        return {}
    records = [value for value in mapping.values() if isinstance(value, dict)]
    return next(
        (record for record in records if record.get("primaryTrainingDevice")),
        records[0] if records else {},
    )


def _status_label(value) -> str | None:
    if not value:
        return None
    parts = str(value).split("_")
    if parts and parts[-1].isdigit():
        parts.pop()
    return " ".join(parts).title()


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
            "moving_duration_s": a.get("movingDuration"),
            "avg_power": a.get("avgPower"),
            "normalized_power": a.get("normPower"),
            "aerobic_te": a.get("aerobicTrainingEffect"),
            "anaerobic_te": a.get("anaerobicTrainingEffect"),
            "raw_json": db.dumps(a),
        }
        if row["activity_id"] is not None:
            db.upsert_activity(conn, row)


def _sync_sleep(client: Garmin, conn, day: date):
    data = _with_retry(client.get_sleep_data, day.isoformat())
    if not data:
        return None
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
    return data


def _sync_daily_stats(client: Garmin, conn, day: date, sleep_data: dict | None = None):
    data = _with_retry(client.get_stats, day.isoformat())
    if not data:
        return

    day_text = day.isoformat()
    readiness = _optional_call(client.get_morning_training_readiness, day_text) or {}
    training = _optional_call(client.get_training_status, day_text) or {}
    status_map = (
        (training.get("mostRecentTrainingStatus") or {}).get("latestTrainingStatusData")
        or {}
    )
    status = _primary_device_record(status_map)
    vo2 = (training.get("mostRecentVO2Max") or {}).get("generic") or {}
    sleep_data = sleep_data or {}
    recovery_minutes = readiness.get("recoveryTime")
    if readiness.get("recoveryTimeChangePhrase") == "REACHED_ZERO":
        recovery_minutes = 0

    row = {
        "date": day_text,
        "steps": data.get("totalSteps"),
        "resting_hr": data.get("restingHeartRate"),
        "stress_avg": data.get("averageStressLevel"),
        "body_battery_high": data.get("bodyBatteryHighestValue"),
        "body_battery_low": data.get("bodyBatteryLowestValue"),
        "calories": data.get("totalKilocalories"),
        "hrv_overnight": sleep_data.get("avgOvernightHrv"),
        "training_readiness": readiness.get("score"),
        "training_status": _status_label(
            status.get("trainingStatusFeedbackPhrase") or readiness.get("level")
        ),
        "vo2_max": vo2.get("vo2MaxPreciseValue") or vo2.get("vo2MaxValue"),
        "recovery_time_hours": (
            float(recovery_minutes) / 60 if recovery_minutes is not None else None
        ),
        "moderate_intensity_min": data.get("moderateIntensityMinutes"),
        "vigorous_intensity_min": data.get("vigorousIntensityMinutes"),
        "avg_respiration": data.get("avgWakingRespirationValue"),
        "avg_spo2": data.get("averageSpo2"),
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


def _calendar_months(start: date, end: date):
    current = start.replace(day=1)
    while current <= end:
        yield current.year, current.month
        current = (
            current.replace(year=current.year + 1, month=1)
            if current.month == 12
            else current.replace(month=current.month + 1)
        )


def _sync_scheduled_workouts(client: Garmin, conn, start: date, end: date):
    """Cache a complete calendar window without erasing it after partial failures."""
    seen_at = datetime.now().isoformat(timespec="seconds")
    rows = []
    for year, month in _calendar_months(start, end):
        payload = _with_retry(client.get_scheduled_workouts, year, month)
        for item in training_calendar.scheduled_items(payload):
            row = training_calendar.normalize_scheduled_workout(item, seen_at)
            if start.isoformat() <= row["date"] <= end.isoformat():
                rows.append(row)
    db.replace_scheduled_workouts(conn, start.isoformat(), end.isoformat(), rows)
    db.set_sync_state(conn, "scheduled_workouts_last_synced_at", seen_at)
    db.set_sync_state(conn, "scheduled_workouts_error", "")


def _sync_race_predictions(client: Garmin, conn, end: date):
    """Store Garmin's daily model history so predictions remain auditable over time."""
    from garmin_ai import race_prediction

    fetched_at = datetime.now().isoformat(timespec="seconds")
    start = end - timedelta(days=365)
    payload = _with_retry(
        client.get_race_predictions,
        start.isoformat(),
        end.isoformat(),
        "daily",
    )
    items = payload if isinstance(payload, list) else [payload]
    stored = 0
    for item in items:
        if not isinstance(item, dict):
            continue
        row = race_prediction.normalize_garmin_prediction(item, fetched_at)
        if row and any(row[key] is not None for key in (
            "time_5k_s", "time_10k_s", "time_half_s", "time_marathon_s"
        )):
            db.upsert_race_prediction(conn, row)
            stored += 1
    if not stored:
        raise ValueError("Garmin returned no usable race predictions.")
    db.set_sync_state(conn, "race_predictions_last_synced_at", fetched_at)
    db.set_sync_state(conn, "race_predictions_error", "")


def sync(days: int = 7, reuse_today: bool = False, prompt_mfa=None):
    """Pull the last `days` days of data."""
    with _sync_guard():
        today = date.today()
        start = today - timedelta(days=days - 1)

        if reuse_today:
            with db.get_connection() as conn:
                if _today_sync_covers(conn, days, today):
                    return start, today

        client = login(prompt_mfa=prompt_mfa) if prompt_mfa is not None else login()
        with db.get_connection() as conn:
            _sync_activities(client, conn, start, today)

            day = start
            while day <= today:
                sleep_data = _sync_sleep(client, conn, day)
                _sync_daily_stats(client, conn, day, sleep_data)
                _sync_body_composition(client, conn, day)
                day += timedelta(days=1)

            calendar_start = min(start, today - timedelta(days=14))
            calendar_end = today + timedelta(days=42)
            try:
                _sync_scheduled_workouts(client, conn, calendar_start, calendar_end)
            except Exception as error:
                db.set_sync_state(conn, "scheduled_workouts_error", friendly_error(error))
            try:
                _sync_race_predictions(client, conn, today)
            except Exception as error:
                db.set_sync_state(conn, "race_predictions_error", friendly_error(error))

            db.set_last_synced_date(conn, today.isoformat())
            db.set_sync_state(conn, "last_synced_at", datetime.now().isoformat(timespec="seconds"))
            db.set_sync_state(conn, "last_sync_days", str(days))

    return start, today
