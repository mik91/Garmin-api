"""Data and actions for the main local interface, reusing the existing engines."""
import json
import logging
from contextlib import closing
import sqlite3
import threading
from datetime import date
from pathlib import Path

import pandas as pd

from garmin_ai import analysis, ai_insights, calendar, config, insights_engine, race_prediction, web_extras

log = logging.getLogger(__name__)
sync_lock = threading.Lock()
sync_status = {"running": False, "message": "", "finished": 0}


def records(frame):
    return json.loads(frame.to_json(orient="records", date_format="iso"))


def read_history(db_path=None):
    with closing(sqlite3.connect(Path(db_path or config.DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        frames = {name: pd.read_sql_query(f"SELECT * FROM {name}", conn) for name in
                  ("activities", "daily_stats", "sleep", "body_composition", "scheduled_workouts",
                   "adherence_overrides", "race_predictions", "goals", "sync_state")}
    for name, frame in frames.items():
        column = "start_time" if name == "activities" else "date"
        if column in frame:
            frame[column] = pd.to_datetime(frame[column])
            frames[name] = frame.sort_values(column)
    frames["activities"] = analysis.ensure_activity_categories(frames["activities"])
    return frames


def coach_races(db_path=None):
    """The coach profile's races (oldest first) and the season phase from the next A race.
    Empty when the profile has no races or they cannot be read."""
    from garmin_ai.coaching import history, review
    try:
        with closing(sqlite3.connect(Path(db_path or config.DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT content FROM coach_profile WHERE section = 'races'").fetchone()
            races = review.parse_races(row["content"] if row else None)
            season = review.season_phase(races, date.today().isoformat(), history.rules(conn)) if races else None
    except (sqlite3.Error, ValueError) as error:
        log.warning("Could not read the profile races: %s", error)
        return [], None
    return races, season


def latest_review(week=None, db_path=None):
    """The stored automatic weekly review (newest, or for one week), or None."""
    from garmin_ai.coaching import weekly
    try:
        with closing(sqlite3.connect(Path(db_path or config.DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
            conn.row_factory = sqlite3.Row
            return weekly.latest(conn, week)
    except sqlite3.Error as error:
        log.warning("Could not read the weekly review: %s", error)
        return None


def weekly_card(db_path=None):
    """The latest review's card, for payloads the app already loads (dashboard, plan)."""
    from garmin_ai.coaching import weekly
    return weekly.card_summary(latest_review(db_path=db_path))


def review_payload(query):
    """GET /api/review[?week=YYYY-MM-DD]: the latest automatic weekly review, or review null."""
    from garmin_ai.coaching import weekly
    week = query.get("week", [None])[0]
    if week:
        week = weekly.week_start_of(week)
    try:
        schedule = weekly.Schedule.parse(config.GARMIN_WEEKLY_REVIEW)
    except ValueError:
        schedule = None
    return {"review": latest_review(week), "week": week,
            "schedule": schedule.describe() if schedule else None,
            "timezone": config.GARMIN_TIMEZONE or None}


def bounds(query):
    start = pd.Timestamp(query.get("start", [date.today().replace(day=1).isoformat()])[0])
    end = pd.Timestamp(query.get("end", [date.today().isoformat()])[0])
    if pd.isna(start) or pd.isna(end) or start.tzinfo or end.tzinfo or start > end:
        raise ValueError("Choose a valid date range.")
    return start, end


def window(frame, start, end, column="date"):
    return frame[frame[column].between(start, end+pd.Timedelta(days=1)-pd.Timedelta(microseconds=1))].copy()


def payload(query, db_path=None):
    history = read_history(db_path)
    start, end = bounds(query)
    activity = history["activities"]
    selected = window(activity, start, end, "start_time")
    raw = selected.raw_json.map(lambda value: json.loads(value or "{}"))
    selected["name"] = raw.map(lambda value: value.get("activityName"))
    selected["location"] = raw.map(lambda value: value.get("locationName"))
    selected["training_load"] = raw.map(lambda value: value.get("activityTrainingLoad"))
    selected["hr_zones"] = raw.map(lambda value: [value.get(f"hrTimeInZone_{i}") for i in range(1, 6)])
    selected["split_summaries"] = raw.map(lambda value: value.get("splitSummaries", []))
    focus = web_extras.running_focus(selected, start, end)
    # Do not expose unneeded device/account fields in the browser.
    selected = selected.drop(columns=["raw_json"])
    report = insights_engine.generate_insights(history["daily_stats"], history["sleep"], activity,
                                               history["body_composition"], start.date(), end.date())
    plans = calendar.build_adherence(history["scheduled_workouts"], activity, history["adherence_overrides"])
    calendar_activity = activity[["activity_id", "start_time", "type", "duration_s", "distance_m"]].copy()
    calendar_activity["name"] = activity.raw_json.map(lambda value: json.loads(value or "{}").get("activityName"))
    try:
        ai = {"configured": ai_insights.is_configured(), "provider": ai_insights.provider_name(),
              "model": ai_insights.configured_model()}
    except ValueError:
        ai = {"configured": False, "provider": config.AI_PROVIDER, "model": ""}
    races, season = coach_races(db_path)
    quality = web_extras.data_quality_notes(
        window(history["daily_stats"], start, end), window(history["sleep"], start, end), selected,
        window(history["body_composition"], start, end),
        dict(history["sync_state"][["key", "value"]].itertuples(index=False, name=None)), start, end)
    return {"activities": records(selected), "running_focus": focus, "data_quality": quality,
            **{name: records(window(history[name], start, end).drop(columns=["raw_json"], errors="ignore"))
               for name in ("daily_stats", "sleep", "body_composition")},
            "calendar": records(plans), "calendar_activities": records(calendar_activity),
            "race_history": records(history["race_predictions"].drop(columns=["raw_json"], errors="ignore")),
            "race_model": race_prediction.build_model(activity, history["race_predictions"]),
            "insights": report,
            "goals": dict(history["goals"][["key", "value"]].itertuples(index=False, name=None)),
            "races": races, "season": season,
            "weekly_review": weekly_card(db_path),
            "sync_state": dict(history["sync_state"][["key", "value"]].itertuples(index=False, name=None)),
            "ai": ai, "hosted": config.HOSTED}


def save_context(body):
    values = {key: str(body.get(key, "")).strip() for key in ("primary_goal", "target_event", "constraints")}
    if any(len(value) > 4000 for value in values.values()):
        raise ValueError("Context is too long.")
    analysis.save_goals(values)
    return {"message": "Context saved."}


def save_adherence(body):
    schedule_id = str(body.get("scheduled_workout_id", ""))
    status = body.get("status")
    if status not in ("automatic", "completed", "missed", "planned", "skipped"):
        raise ValueError("Choose a valid status.")
    scheduled = analysis.load_scheduled_workouts()
    if schedule_id not in scheduled.scheduled_workout_id.astype(str).tolist():
        raise ValueError("Scheduled workout not found.")
    activity_id = body.get("matched_activity_id")
    if activity_id is not None:
        activity_id = int(activity_id)
        if activity_id not in analysis.load_activities().activity_id.tolist():
            raise ValueError("Activity not found.")
    if status == "automatic":
        analysis.clear_adherence_override(schedule_id)
    else:
        analysis.save_adherence_override(schedule_id, status, activity_id)
    return {"message": "Workout status saved."}


def current_sync_status():
    with sync_lock:
        return dict(sync_status)


def start_sync(days):
    """Start a background sync unless one is already running. Returns the status."""
    with sync_lock:
        if sync_status["running"]:
            return dict(sync_status)
        sync_status.update(running=True, message="Syncing Garmin history…")
    def run():
        from garmin_ai import sync
        def needs_mfa():
            raise ValueError("Garmin needs you to sign in again. Open Connections and sign in to Garmin, then sync.")
        try:
            sync.sync(days=days, prompt_mfa=needs_mfa)
            message = "Sync complete. History refreshed."
        except Exception as error:
            log.warning("Sync failed", exc_info=True)
            message = "Sync failed: " + sync.friendly_error(error)
        with sync_lock:
            sync_status.update(running=False, message=message, finished=sync_status["finished"] + 1)
    threading.Thread(target=run, daemon=True).start()
    return {"running": True, "message": "Sync started."}


def begin_sync(body):
    days = body.get("days", 7)
    if not isinstance(days, int) or isinstance(days, bool) or not 1 <= days <= 365:
        raise ValueError("Sync days must be between 1 and 365.")
    return start_sync(days)


def auto_sync_loop(check_seconds=60):
    """Sync in the background whenever the last attempt is older than the configured interval.

    Settings are re-read on every check so changes from the Connections page apply live."""
    import time
    from datetime import datetime
    from garmin_ai import db
    last_attempt = 0.0
    while True:
        interval_min, days = config.GARMIN_AUTO_SYNC_INTERVAL_MIN, config.GARMIN_AUTO_SYNC_DAYS
        try:
            if interval_min <= 0:
                raise LookupError("auto-sync is off")
            with db.get_connection() as conn:
                last = db.get_sync_state(conn).get("last_synced_at")
            age = (datetime.now() - datetime.fromisoformat(last)).total_seconds() if last else float("inf")
            due = age >= interval_min * 60 and time.time() - last_attempt >= interval_min * 60
            if due and _can_sync():
                last_attempt = time.time()
                start_sync(days)
        except LookupError:
            pass
        except Exception:
            log.exception("Auto-sync check failed")
        time.sleep(check_seconds)


def _can_sync() -> bool:
    return bool(config.GARMIN_EMAIL or Path(config.GARMIN_TOKENSTORE).exists() or config.HEVY_API_KEY)


def _local_now():
    """Now in GARMIN_TIMEZONE (naive), or the server's local time when it is unset or unknown."""
    from datetime import datetime
    if config.GARMIN_TIMEZONE:
        try:
            from zoneinfo import ZoneInfo
            return datetime.now(ZoneInfo(config.GARMIN_TIMEZONE)).replace(tzinfo=None)
        except Exception:
            log.warning("Unknown GARMIN_TIMEZONE %r; using the server's local time", config.GARMIN_TIMEZONE)
    return datetime.now()


def _sync_and_wait(timeout_s=1200, poll_s=5):
    """Run a sync now (or wait for the one already running) so the review sees today's data."""
    import time
    with sync_lock:
        before = sync_status["finished"]
    start_sync(config.GARMIN_AUTO_SYNC_DAYS)
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with sync_lock:
            if not sync_status["running"] and sync_status["finished"] > before:
                return sync_status["message"]
        time.sleep(poll_s)
    return "Sync still running; reviewed the data already synced."


def run_weekly_review_if_due(now=None, sync=True):
    """Store the review for the week of the latest scheduled time unless it exists already.
    Returns the new coach_log entry, or None when nothing was due."""
    from garmin_ai import db
    from garmin_ai.coaching import weekly
    schedule = weekly.Schedule.parse(config.GARMIN_WEEKLY_REVIEW)
    if schedule is None:
        return None
    moment = schedule.last_run_before(now or _local_now())
    monday = weekly.monday_of(moment.date()).isoformat()
    with db.get_connection() as conn:
        if weekly.has_review(conn, monday):
            return None
    if sync and _can_sync():
        log.info("Weekly review for %s: %s", monday, _sync_and_wait())
    with db.get_connection() as conn:
        synced_at = db.get_sync_state(conn).get("last_synced_at")
        entry = weekly.run(conn, monday, today=moment.date(), synced_at=synced_at)
    log.info("Weekly review stored for %s: %s", monday, entry["summary"])
    return entry


def weekly_review_loop(check_seconds=300, first_delay_s=90, retry_s=3600):
    """Check every few minutes whether the weekly review is due. Waits first so the server
    (and the startup sync) can settle; a failed week is retried after retry_s."""
    import time
    time.sleep(first_delay_s)
    failed_at = 0.0
    while True:
        if time.time() - failed_at >= retry_s:
            try:
                run_weekly_review_if_due()
            except Exception:
                failed_at = time.time()
                log.exception("Weekly review failed")
        time.sleep(check_seconds)


def start_auto_sync():
    """Launch the scheduler threads (sync and weekly review); returns a description, or None
    when both are off."""
    from garmin_ai.coaching import weekly
    threading.Thread(target=auto_sync_loop, daemon=True).start()
    notes = []
    interval = config.GARMIN_AUTO_SYNC_INTERVAL_MIN
    if interval > 0:
        notes.append(f"Auto-sync every {interval} min ({config.GARMIN_AUTO_SYNC_DAYS} days)")
    try:
        schedule = weekly.Schedule.parse(config.GARMIN_WEEKLY_REVIEW)
    except ValueError as error:
        log.warning("%s Weekly review is off.", error)
        schedule = None
    if schedule:
        threading.Thread(target=weekly_review_loop, daemon=True).start()
        zone = config.GARMIN_TIMEZONE or "server time"
        notes.append(f"weekly review {schedule.describe()} ({zone})")
    return "; ".join(notes) or None


def activity_raw(query):
    """Stored Garmin payload for one activity, loaded only when the user opens it."""
    try:
        activity_id = int(query.get("id", [""])[0])
    except ValueError:
        raise ValueError("Invalid activity id.")
    with closing(sqlite3.connect(Path(config.DB_PATH).resolve().as_uri()+"?mode=ro", uri=True)) as conn:
        row = conn.execute("SELECT raw_json FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
    if row is None:
        raise ValueError("Activity not found.")
    return json.loads(row[0] or "{}")


SPLIT_FIELDS = {
    "lap": "lapIndex", "distance_m": "distance", "duration_s": "duration", "moving_s": "movingDuration",
    "avg_hr": "averageHR", "max_hr": "maxHR", "gain_m": "elevationGain", "loss_m": "elevationLoss",
    "speed_ms": "averageMovingSpeed", "run_cadence": "averageRunCadence", "power": "averagePower",
    "swim_cadence": "averageSwimCadence", "swolf": "averageSWOLF", "strokes": "totalNumberOfStrokes",
    "stride_cm": "strideLength", "gct_ms": "groundContactTime", "intensity": "intensityType",
}


def activity_splits(body):
    """Per-lap splits for one activity. Downloaded from Garmin on first request, then cached."""
    activity_id = body.get("id")
    if not isinstance(activity_id, int) or isinstance(activity_id, bool):
        raise ValueError("Invalid activity id.")
    from garmin_ai import db, sync
    with db.get_connection() as conn:
        row = conn.execute("SELECT splits_json FROM activities WHERE activity_id = ?", (activity_id,)).fetchone()
        if row is None:
            raise ValueError("Activity not found.")
        stored = row["splits_json"]
    if stored:
        data = json.loads(stored)
    else:
        def needs_mfa():
            raise ValueError("Garmin needs you to sign in again. Open Connections and sign in to Garmin, then retry.")
        try:
            data = sync.login(prompt_mfa=needs_mfa).get_activity_splits(activity_id) or {}
        except ValueError:
            raise
        except Exception as error:
            raise ValueError("Could not download splits: " + sync.friendly_error(error))
        with db.get_connection() as conn:
            conn.execute("UPDATE activities SET splits_json = ? WHERE activity_id = ?", (db.dumps(data), activity_id))
            conn.commit()
    laps = data.get("lapDTOs") if isinstance(data, dict) else None
    return {"laps": [{key: lap.get(source) for key, source in SPLIT_FIELDS.items()} for lap in laps or []]}


def chat_summary(body):
    """The aggregated, summarized data sent with an AI request (and previewed in the UI)."""
    query = {"start": [str(body.get("start", ""))], "end": [str(body.get("end", ""))]}
    dashboard = payload(query)
    summary = {"insight_engine": dashboard["insights"], "personal_goals": dashboard["goals"],
               "race_prediction_model": dashboard["race_model"],
               "recent_daily_trend": dashboard["daily_stats"][-14:], "recent_sleep_trend": dashboard["sleep"][-14:]}
    target = body.get("target", {})
    if isinstance(target, dict):
        summary["user_target_scenario_not_prediction"] = {key: str(target.get(key, ""))[:100] for key in ("distance", "finish_time", "weeks")}
    return summary


def ai_preview(body):
    return {"summary": chat_summary(body)}


def chat(body):
    if not ai_insights.is_configured():
        raise ValueError("Configure an AI provider in .env to use AI insights.")
    messages = body.get("messages")
    if not isinstance(messages, list) or not 1 <= len(messages) <= 30:
        raise ValueError("Send between 1 and 30 chat messages.")
    for message in messages:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant") or not isinstance(message.get("content"), str) or len(message["content"]) > 8000:
            raise ValueError("Invalid chat message.")
    return {"answer": ai_insights.chat(chat_summary(body), messages)}
