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
"""


@contextmanager
def get_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()


def upsert_activity(conn, activity: dict):
    conn.execute(
        """
        INSERT INTO activities
            (activity_id, start_time, type, duration_s, distance_m, calories, avg_hr, max_hr, elevation_gain_m, raw_json)
        VALUES (:activity_id, :start_time, :type, :duration_s, :distance_m, :calories, :avg_hr, :max_hr, :elevation_gain_m, :raw_json)
        ON CONFLICT(activity_id) DO UPDATE SET
            start_time=excluded.start_time,
            type=excluded.type,
            duration_s=excluded.duration_s,
            distance_m=excluded.distance_m,
            calories=excluded.calories,
            avg_hr=excluded.avg_hr,
            max_hr=excluded.max_hr,
            elevation_gain_m=excluded.elevation_gain_m,
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
    conn.execute(
        """
        INSERT INTO daily_stats
            (date, steps, resting_hr, stress_avg, body_battery_high, body_battery_low, calories, raw_json)
        VALUES (:date, :steps, :resting_hr, :stress_avg, :body_battery_high, :body_battery_low, :calories, :raw_json)
        ON CONFLICT(date) DO UPDATE SET
            steps=excluded.steps,
            resting_hr=excluded.resting_hr,
            stress_avg=excluded.stress_avg,
            body_battery_high=excluded.body_battery_high,
            body_battery_low=excluded.body_battery_low,
            calories=excluded.calories,
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


def dumps(obj) -> str:
    return json.dumps(obj, default=str)
