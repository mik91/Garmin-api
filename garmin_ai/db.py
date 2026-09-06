import json
import os
import sqlite3
from contextlib import contextmanager

from garmin_ai.config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    start_time TEXT,
    type TEXT,
    duration_s REAL,
    distance_m REAL,
    calories REAL,
    avg_hr REAL,
    max_hr REAL,
    elevation_gain_m REAL,
    moving_duration_s REAL,
    avg_power REAL,
    normalized_power REAL,
    aerobic_te REAL,
    anaerobic_te REAL,
    splits_json TEXT,
    exercise_sets_json TEXT,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS sleep (
    date TEXT PRIMARY KEY,
    total_sleep_s REAL,
    deep_s REAL,
    light_s REAL,
    rem_s REAL,
    awake_s REAL,
    sleep_score REAL,
    resting_hr REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS daily_stats (
    date TEXT PRIMARY KEY,
    steps INTEGER,
    resting_hr REAL,
    stress_avg REAL,
    body_battery_high REAL,
    body_battery_low REAL,
    calories REAL,
    hrv_overnight REAL,
    training_readiness REAL,
    training_status TEXT,
    vo2_max REAL,
    recovery_time_hours REAL,
    moderate_intensity_min REAL,
    vigorous_intensity_min REAL,
    avg_respiration REAL,
    avg_spo2 REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS body_composition (
    date TEXT PRIMARY KEY,
    weight_kg REAL,
    bmi REAL,
    body_fat_pct REAL,
    raw_json TEXT
);

CREATE TABLE IF NOT EXISTS sync_state (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS goals (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS scheduled_workouts (
    scheduled_workout_id TEXT PRIMARY KEY,
    workout_id TEXT,
    date TEXT NOT NULL,
    name TEXT,
    sport TEXT,
    duration_s REAL,
    distance_m REAL,
    associated_activity_id INTEGER,
    raw_json TEXT,
    last_seen_at TEXT
);

CREATE TABLE IF NOT EXISTS adherence_overrides (
    scheduled_workout_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    matched_activity_id INTEGER,
    updated_at TEXT NOT NULL,
    FOREIGN KEY (scheduled_workout_id) REFERENCES scheduled_workouts(scheduled_workout_id)
);
"""


ACTIVITY_COLUMNS = {
    "moving_duration_s": "REAL",
    "avg_power": "REAL",
    "normalized_power": "REAL",
    "aerobic_te": "REAL",
    "anaerobic_te": "REAL",
    "splits_json": "TEXT",
    "exercise_sets_json": "TEXT",
}

DAILY_COLUMNS = {
    "hrv_overnight": "REAL",
    "training_readiness": "REAL",
    "training_status": "TEXT",
    "vo2_max": "REAL",
    "recovery_time_hours": "REAL",
    "moderate_intensity_min": "REAL",
    "vigorous_intensity_min": "REAL",
    "avg_respiration": "REAL",
    "avg_spo2": "REAL",
}


def _ensure_columns(conn, table: str, columns: dict[str, str]):
    existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
    for name, column_type in columns.items():
        if name not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {column_type}")


@contextmanager
def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        _ensure_columns(conn, "activities", ACTIVITY_COLUMNS)
        _ensure_columns(conn, "daily_stats", DAILY_COLUMNS)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_activity(conn, activity: dict):
    # Keep the upsert compatible with callers (and cached payloads) created
    # before the optional activity metrics were added to the schema.
    activity = {**dict.fromkeys(ACTIVITY_COLUMNS), **activity}
    conn.execute(
        """
        INSERT INTO activities
            (activity_id, start_time, type, duration_s, distance_m, calories, avg_hr, max_hr,
             elevation_gain_m, moving_duration_s, avg_power, normalized_power, aerobic_te,
             anaerobic_te, splits_json, exercise_sets_json, raw_json)
        VALUES (:activity_id, :start_time, :type, :duration_s, :distance_m, :calories, :avg_hr,
                :max_hr, :elevation_gain_m, :moving_duration_s, :avg_power, :normalized_power,
                :aerobic_te, :anaerobic_te, :splits_json, :exercise_sets_json, :raw_json)
        ON CONFLICT(activity_id) DO UPDATE SET
            start_time=excluded.start_time,
            type=excluded.type,
            duration_s=excluded.duration_s,
            distance_m=excluded.distance_m,
            calories=excluded.calories,
            avg_hr=excluded.avg_hr,
            max_hr=excluded.max_hr,
            elevation_gain_m=excluded.elevation_gain_m,
            moving_duration_s=COALESCE(excluded.moving_duration_s, activities.moving_duration_s),
            avg_power=COALESCE(excluded.avg_power, activities.avg_power),
            normalized_power=COALESCE(excluded.normalized_power, activities.normalized_power),
            aerobic_te=COALESCE(excluded.aerobic_te, activities.aerobic_te),
            anaerobic_te=COALESCE(excluded.anaerobic_te, activities.anaerobic_te),
            splits_json=COALESCE(excluded.splits_json, activities.splits_json),
            exercise_sets_json=COALESCE(excluded.exercise_sets_json, activities.exercise_sets_json),
            raw_json=excluded.raw_json
        """,
        activity,
    )


def upsert_sleep(conn, row: dict):
    conn.execute(
        """
        INSERT INTO sleep
            (date, total_sleep_s, deep_s, light_s, rem_s, awake_s, sleep_score, resting_hr, raw_json)
        VALUES (:date, :total_sleep_s, :deep_s, :light_s, :rem_s, :awake_s, :sleep_score, :resting_hr, :raw_json)
        ON CONFLICT(date) DO UPDATE SET
            total_sleep_s=excluded.total_sleep_s,
            deep_s=excluded.deep_s,
            light_s=excluded.light_s,
            rem_s=excluded.rem_s,
            awake_s=excluded.awake_s,
            sleep_score=excluded.sleep_score,
            resting_hr=excluded.resting_hr,
            raw_json=excluded.raw_json
        """,
        row,
    )


def upsert_daily_stats(conn, row: dict):
    # Garmin does not return every extended metric for every day. Missing
    # values should be stored as NULL instead of becoming SQL binding errors.
    row = {**dict.fromkeys(DAILY_COLUMNS), **row}
    conn.execute(
        """
        INSERT INTO daily_stats
            (date, steps, resting_hr, stress_avg, body_battery_high, body_battery_low, calories,
             hrv_overnight, training_readiness, training_status, vo2_max, recovery_time_hours,
             moderate_intensity_min, vigorous_intensity_min, avg_respiration, avg_spo2, raw_json)
        VALUES (:date, :steps, :resting_hr, :stress_avg, :body_battery_high, :body_battery_low,
                :calories, :hrv_overnight, :training_readiness, :training_status, :vo2_max,
                :recovery_time_hours, :moderate_intensity_min, :vigorous_intensity_min,
                :avg_respiration, :avg_spo2, :raw_json)
        ON CONFLICT(date) DO UPDATE SET
            steps=excluded.steps,
            resting_hr=excluded.resting_hr,
            stress_avg=excluded.stress_avg,
            body_battery_high=excluded.body_battery_high,
            body_battery_low=excluded.body_battery_low,
            calories=excluded.calories,
            hrv_overnight=COALESCE(excluded.hrv_overnight, daily_stats.hrv_overnight),
            training_readiness=COALESCE(excluded.training_readiness, daily_stats.training_readiness),
            training_status=COALESCE(excluded.training_status, daily_stats.training_status),
            vo2_max=COALESCE(excluded.vo2_max, daily_stats.vo2_max),
            recovery_time_hours=COALESCE(excluded.recovery_time_hours, daily_stats.recovery_time_hours),
            moderate_intensity_min=COALESCE(excluded.moderate_intensity_min, daily_stats.moderate_intensity_min),
            vigorous_intensity_min=COALESCE(excluded.vigorous_intensity_min, daily_stats.vigorous_intensity_min),
            avg_respiration=COALESCE(excluded.avg_respiration, daily_stats.avg_respiration),
            avg_spo2=COALESCE(excluded.avg_spo2, daily_stats.avg_spo2),
            raw_json=excluded.raw_json
        """,
        row,
    )


def upsert_body_composition(conn, row: dict):
    conn.execute(
        """
        INSERT INTO body_composition
            (date, weight_kg, bmi, body_fat_pct, raw_json)
        VALUES (:date, :weight_kg, :bmi, :body_fat_pct, :raw_json)
        ON CONFLICT(date) DO UPDATE SET
            weight_kg=excluded.weight_kg,
            bmi=excluded.bmi,
            body_fat_pct=excluded.body_fat_pct,
            raw_json=excluded.raw_json
        """,
        row,
    )


def get_last_synced_date(conn):
    cur = conn.execute("SELECT value FROM sync_state WHERE key = 'last_synced_date'")
    row = cur.fetchone()
    return row["value"] if row else None


def get_sync_state(conn) -> dict:
    cur = conn.execute("SELECT key, value FROM sync_state")
    return {row["key"]: row["value"] for row in cur.fetchall()}


def set_last_synced_date(conn, date_str: str):
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES ('last_synced_date', ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (date_str,),
    )


def set_sync_state(conn, key: str, value: str):
    conn.execute(
        """
        INSERT INTO sync_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        (key, value),
    )


def get_goals(conn) -> dict:
    return {row["key"]: row["value"] for row in conn.execute("SELECT key, value FROM goals")}


def set_goals(conn, goals: dict):
    conn.execute("DELETE FROM goals")
    conn.executemany(
        """
        INSERT INTO goals (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value
        """,
        [(key, str(value)) for key, value in goals.items()],
    )


def replace_scheduled_workouts(conn, start_date: str, end_date: str, rows: list[dict]):
    """Replace the cached Garmin calendar entries for a completely fetched window."""
    conn.execute(
        "DELETE FROM scheduled_workouts WHERE date BETWEEN ? AND ?",
        (start_date, end_date),
    )
    conn.executemany(
        """
        INSERT INTO scheduled_workouts
            (scheduled_workout_id, workout_id, date, name, sport, duration_s,
             distance_m, associated_activity_id, raw_json, last_seen_at)
        VALUES
            (:scheduled_workout_id, :workout_id, :date, :name, :sport, :duration_s,
             :distance_m, :associated_activity_id, :raw_json, :last_seen_at)
        ON CONFLICT(scheduled_workout_id) DO UPDATE SET
            workout_id=excluded.workout_id,
            date=excluded.date,
            name=excluded.name,
            sport=excluded.sport,
            duration_s=excluded.duration_s,
            distance_m=excluded.distance_m,
            associated_activity_id=excluded.associated_activity_id,
            raw_json=excluded.raw_json,
            last_seen_at=excluded.last_seen_at
        """,
        rows,
    )


def set_adherence_override(
    conn,
    scheduled_workout_id: str,
    status: str,
    matched_activity_id: int | None = None,
):
    conn.execute(
        """
        INSERT INTO adherence_overrides
            (scheduled_workout_id, status, matched_activity_id, updated_at)
        VALUES (?, ?, ?, datetime('now'))
        ON CONFLICT(scheduled_workout_id) DO UPDATE SET
            status=excluded.status,
            matched_activity_id=excluded.matched_activity_id,
            updated_at=excluded.updated_at
        """,
        (scheduled_workout_id, status, matched_activity_id),
    )


def clear_adherence_override(conn, scheduled_workout_id: str):
    conn.execute(
        "DELETE FROM adherence_overrides WHERE scheduled_workout_id = ?",
        (scheduled_workout_id,),
    )


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
