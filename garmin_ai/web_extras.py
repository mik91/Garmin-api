"""Extra summaries for the web interface: running focus and data-quality notes."""
import json
from datetime import date, datetime, timedelta

import pandas as pd


def _raw(row):
    try:
        value = json.loads(row.get("raw_json") or "{}")
        return value if isinstance(value, dict) else {}
    except (TypeError, ValueError):
        return {}


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(number) else round(number, 2)


def _best_effort(runs, key):
    best = None
    for _, row in runs.iterrows():
        seconds = _number(_raw(row).get(key))
        if seconds and (best is None or seconds < best["seconds"]):
            best = {"seconds": seconds, "activity_id": int(row["activity_id"]), "activity": _raw(row).get("activityName") or row["type"],
                    "date": row["start_time"].date().isoformat()}
    return best


def running_focus(activities, start, end):
    """Running summary for the selected window; None when there are no runs."""
    if activities.empty or "category" not in activities:
        return None
    runs = activities[activities["category"] == "Running"].dropna(subset=["start_time"]).copy()
    if runs.empty:
        return None
    runs["distance_km"] = runs["distance_m"] / 1000
    moving = runs.apply(lambda row: _number(_raw(row).get("movingDuration")) or row["duration_s"], axis=1)
    total_km = runs["distance_km"].sum(skipna=True)
    days = max((pd.Timestamp(end) - pd.Timestamp(start)).days + 1, 1)
    longest = runs.sort_values("distance_km", ascending=False).iloc[0]
    return {
        "run_count": int(len(runs)),
        "total_distance_km": _number(total_km),
        "avg_run_distance_km": _number(runs["distance_km"].mean(skipna=True)),
        "weekly_volume_km": _number(total_km / days * 7),
        "avg_pace_min_km": _number(moving.sum(skipna=True) / 60 / total_km) if total_km else None,
        "longest_run": {"activity_id": int(longest["activity_id"]), "activity": _raw(longest).get("activityName") or longest["type"],
                        "date": longest["start_time"].date().isoformat(),
                        "distance_km": _number(longest["distance_km"])},
        "best_5k": _best_effort(runs, "fastestSplit_5000"),
        "best_10k": _best_effort(runs, "fastestSplit_10000"),
        "best_mile": _best_effort(runs, "fastestSplit_1609"),
    }


def data_quality_notes(daily, sleep, activities, body, sync_state, start, end):
    notes = []
    expected_days = max((pd.Timestamp(end) - pd.Timestamp(start)).days + 1, 1)
    last_synced_at = sync_state.get("last_synced_at")
    if not last_synced_at and not sync_state.get("last_synced_date"):
        notes.append("No successful sync recorded yet.")
    elif last_synced_at:
        try:
            synced = datetime.fromisoformat(last_synced_at)
            if datetime.now() - synced > timedelta(days=2):
                notes.append(f"Last sync was {synced:%b %d, %Y at %I:%M %p}.")
        except ValueError:
            notes.append(f"Last sync timestamp looks unusual: {last_synced_at}.")
    if daily.empty:
        notes.append("No daily stats in the selected range.")
    elif daily["date"].dt.date.nunique() < max(1, expected_days * 0.6):
        notes.append("Daily stats cover less than 60% of this date range.")
    else:
        coverage = [daily[f].notna().mean() for f in ("hrv_overnight", "training_readiness", "recovery_time_hours") if f in daily]
        if coverage and sum(coverage) / len(coverage) < 0.5:
            notes.append("Recovery metrics are sparse; sync this range again to add HRV and training readiness.")
    if sleep.empty:
        notes.append("No sleep records in the selected range.")
    elif sleep["date"].dt.date.nunique() < max(1, expected_days * 0.5):
        notes.append("Sleep data is sparse for this range.")
    if activities.empty:
        notes.append("No activities in the selected range.")
    elif "avg_hr" in activities and activities["avg_hr"].isna().mean() > 0.35:
        notes.append("More than a third of activities are missing average heart rate.")
    if body.empty:
        notes.append("No body composition entries in this range.")
    elif "body_fat_pct" in body and not body["body_fat_pct"].notna().any():
        notes.append("Weight is available, but Garmin has no body-fat readings for this range.")
    return notes[:5]
