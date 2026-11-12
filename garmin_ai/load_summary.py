"""Weekly training load from the local activity history (Monday-Sunday weeks).

One consistent definition, so the coach doesn't write its own SQL each time:
- quality session = Garmin's primary training-effect label is TEMPO, LACTATE_THRESHOLD,
  VO2MAX, ANAEROBIC_CAPACITY or SPEED (Garmin sets it from the session's HR/pace profile).
- hours use elapsed duration; distance is what the watch recorded.
- strength also counts Hevy workouts that have no matching Garmin activity.
"""
import json
import sqlite3
from datetime import date, datetime, timedelta

from garmin_ai.analysis import activity_category

QUALITY_LABELS = ("TEMPO", "LACTATE_THRESHOLD", "VO2MAX", "ANAEROBIC_CAPACITY", "SPEED")
SPORTS = ("running", "cycling", "swimming", "strength", "other")


def sport_of(activity_type) -> str:
    if "strength" in str(activity_type or "").lower():
        return "strength"
    return activity_category(activity_type).lower()


def _empty_week(start: date) -> dict:
    return {
        "week_start": start.isoformat(), "week_end": (start + timedelta(days=6)).isoformat(),
        "running": {"distance_km": 0.0, "runs": 0, "longest_run_km": 0.0, "hours": 0.0, "quality_sessions": 0},
        "hours_by_sport": dict.fromkeys(SPORTS, 0.0),
        "distance_km_by_sport": {"running": 0.0, "cycling": 0.0, "swimming": 0.0},
        "sessions_by_sport": dict.fromkeys(SPORTS, 0),
        "quality_sessions": 0, "quality_by_label": {},
        "hr_zone_4_5_minutes": 0.0, "training_load": 0.0,
    }


def _round_week(week: dict) -> dict:
    run = week["running"]
    for key in ("distance_km", "longest_run_km", "hours"):
        run[key] = round(run[key], 1 if key != "hours" else 2)
    week["hours_by_sport"] = {k: round(v, 2) for k, v in week["hours_by_sport"].items()}
    week["distance_km_by_sport"] = {k: round(v, 2) for k, v in week["distance_km_by_sport"].items()}
    week["total_hours"] = round(sum(week["hours_by_sport"].values()), 2)
    week["hr_zone_4_5_minutes"] = round(week["hr_zone_4_5_minutes"])
    week["training_load"] = round(week["training_load"])
    return week


def _pct(new: float, old: float):
    return round((new / old - 1) * 100) if old else None


def weekly_load(conn, weeks: int = 12, today: date | None = None) -> dict:
    """The last `weeks` complete weeks (oldest first) plus the current week so far."""
    weeks = max(1, min(int(weeks), 52))
    today = today or date.today()
    current = today - timedelta(days=today.weekday())
    first = current - timedelta(days=7 * weeks)
    table = {first + timedelta(days=7 * i): _empty_week(first + timedelta(days=7 * i)) for i in range(weeks + 1)}

    rows = conn.execute(
        "SELECT substr(start_time, 1, 10), type, duration_s, distance_m, raw_json FROM activities "
        "WHERE substr(start_time, 1, 10) BETWEEN ? AND ?", (first.isoformat(), today.isoformat())).fetchall()
    for day, activity_type, duration_s, distance_m, raw in rows:
        day = date.fromisoformat(day)
        week = table[day - timedelta(days=day.weekday())]
        sport = sport_of(activity_type)
        hours, km = (duration_s or 0) / 3600, (distance_m or 0) / 1000
        try:
            details = json.loads(raw or "{}")
        except ValueError:
            details = {}
        week["hours_by_sport"][sport] += hours
        week["sessions_by_sport"][sport] += 1
        if sport in week["distance_km_by_sport"]:
            week["distance_km_by_sport"][sport] += km
        label = details.get("trainingEffectLabel")
        if label in QUALITY_LABELS:
            week["quality_sessions"] += 1
            week["quality_by_label"][label] = week["quality_by_label"].get(label, 0) + 1
        week["hr_zone_4_5_minutes"] += sum(details.get(f"hrTimeInZone_{z}") or 0 for z in (4, 5)) / 60
        week["training_load"] += details.get("activityTrainingLoad") or 0
        if sport == "running":
            run = week["running"]
            run["distance_km"] += km
            run["runs"] += 1
            run["hours"] += hours
            run["longest_run_km"] = max(run["longest_run_km"], km)
            run["quality_sessions"] += label in QUALITY_LABELS

    # Gym sessions logged in Hevy without a matching Garmin strength activity count as strength.
    try:
        hevy_rows = conn.execute(
            "SELECT substr(COALESCE(start_local, start_time), 1, 10), start_time, end_time FROM hevy_workouts "
            "WHERE garmin_activity_id IS NULL AND substr(COALESCE(start_local, start_time), 1, 10) BETWEEN ? AND ?",
            (first.isoformat(), today.isoformat())).fetchall()
    except sqlite3.Error:  # no Hevy table yet
        hevy_rows = []
    for day, start, end in hevy_rows:
        day = date.fromisoformat(day)
        week = table[day - timedelta(days=day.weekday())]
        try:
            hours = (datetime.fromisoformat(end.replace("Z", "+00:00"))
                     - datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds() / 3600
        except (AttributeError, ValueError):
            hours = 0.0
        week["hours_by_sport"]["strength"] += max(hours, 0.0)
        week["sessions_by_sport"]["strength"] += 1

    ordered = [_round_week(table[start]) for start in sorted(table)]
    complete, in_progress = ordered[:-1], ordered[-1]
    previous_km = None
    for week in complete:
        km = week["running"]["distance_km"]
        week["running"]["change_vs_previous_week_pct"] = _pct(km, previous_km) if previous_km is not None else None
        week["running"]["longest_run_share_pct"] = round(week["running"]["longest_run_km"] / km * 100) if km else None
        previous_km = km
    in_progress["days_so_far"] = (today - current).days + 1

    def mean(values):
        return round(sum(values) / len(values), 1) if values else None

    last4, prior4 = complete[-4:], complete[-8:-4]
    run_km = [w["running"]["distance_km"] for w in complete]
    summary = {
        "weeks": len(complete),
        "avg_running_km": mean(run_km),
        "avg_runs_per_week": mean([w["running"]["runs"] for w in complete]),
        "avg_total_hours": mean([w["total_hours"] for w in complete]),
        "avg_quality_sessions": mean([w["quality_sessions"] for w in complete]),
        "max_running_km": max(run_km) if run_km else None,
        "longest_run_km": max((w["running"]["longest_run_km"] for w in complete), default=None),
        "last_4_weeks_avg_running_km": mean([w["running"]["distance_km"] for w in last4]),
        "running_km_change_vs_prior_4_weeks_pct": _pct(
            mean([w["running"]["distance_km"] for w in last4]) or 0,
            mean([w["running"]["distance_km"] for w in prior4]) or 0) if prior4 else None,
        "avg_hours_by_sport": {s: mean([w["hours_by_sport"][s] for w in complete]) for s in SPORTS},
    }
    return {"definitions": {"week": "Monday-Sunday", "quality_session": "Garmin training-effect label in "
                            + ", ".join(QUALITY_LABELS), "hours": "elapsed activity time",
                            "strength": "Garmin strength activities plus Hevy workouts not linked to one"},
            "summary": summary, "weeks": complete, "current_week": in_progress}
