"""Exploratory, inspectable comparisons; no causal or predictive claims."""
import json

import pandas as pd


def _raw(value):
    try:
        result = json.loads(value or "{}")
        return result if isinstance(result, dict) else {}
    except (ValueError, TypeError):
        return {}


def run_observations(activities, sleep, start, end, distance=(3, 20), heart_rate=(110, 165), max_climb=20):
    """One earliest qualifying run per day; sleep must end before that run."""
    columns = ["date", "start_time", "distance_km", "avg_hr", "climb_m_per_km", "pace_min_km", "sleep_hours"]
    if activities.empty:
        return pd.DataFrame(columns=columns)
    runs = activities.copy()
    runs = runs[runs["type"].isin(["running", "treadmill_running"])]
    runs["start_time"] = pd.to_datetime(runs["start_time"], errors="coerce")
    runs["date"] = runs["start_time"].dt.normalize()
    runs["distance_km"] = pd.to_numeric(runs["distance_m"], errors="coerce") / 1000
    duration = pd.to_numeric(runs["moving_duration_s"], errors="coerce")
    duration = duration.where(duration > 0, pd.to_numeric(runs["duration_s"], errors="coerce"))
    runs["pace_min_km"] = duration / 60 / runs["distance_km"]
    runs["climb_m_per_km"] = pd.to_numeric(runs["elevation_gain_m"], errors="coerce") / runs["distance_km"]
    runs = runs[
        runs["date"].between(pd.Timestamp(start), pd.Timestamp(end))
        & runs["distance_km"].between(*distance)
        & runs["avg_hr"].between(*heart_rate)
        & runs["climb_m_per_km"].between(0, max_climb)
        & runs["pace_min_km"].between(2, 15)
    ].sort_values("start_time").drop_duplicates("date")
    runs["sleep_hours"] = float("nan")
    if not sleep.empty:
        nights = sleep.copy()
        nights["date"] = pd.to_datetime(nights["date"]).dt.normalize()
        # Garmin's local timestamps are wall-clock epoch values, matching start_time.
        nights["sleep_end"] = nights["raw_json"].map(
            lambda value: _raw(value).get("dailySleepDTO", {}).get("sleepEndTimestampLocal")
        )
        nights["sleep_end"] = pd.to_datetime(nights["sleep_end"], unit="ms", errors="coerce")
        nights["hours"] = pd.to_numeric(nights["total_sleep_s"], errors="coerce") / 3600
        runs = runs.merge(nights[["date", "sleep_end", "hours"]].drop_duplicates("date"), on="date", how="left")
        valid = (runs["sleep_end"] <= runs["start_time"]) & runs["hours"].between(1, 16)
        runs.loc[valid, "sleep_hours"] = runs.loc[valid, "hours"]
    return runs.reindex(columns=columns).reset_index(drop=True)


def recovery_observations(activities, daily, start, end, lag=1):
    """Exact calendar-day change from activity morning to following morning(s)."""
    columns = ["date", "outcome_date", "load", "group", "hrv_change", "resting_hr_change"]
    if activities.empty or daily.empty:
        return pd.DataFrame(columns=columns)
    activity = activities.copy()
    activity["date"] = pd.to_datetime(activity["start_time"]).dt.normalize()
    activity["load"] = pd.to_numeric(activity["raw_json"].map(lambda value: _raw(value).get("activityTrainingLoad")), errors="coerce")
    activity.loc[activity["load"] < 0, "load"] = float("nan")
    grouped = activity.groupby("date")["load"]
    loads = grouped.sum(min_count=1).to_frame()
    # Exclude days with partially missing loads instead of understating their total.
    loads = loads[grouped.count() == grouped.size()].reset_index()
    loads = loads[loads["date"].between(pd.Timestamp(start), pd.Timestamp(end))]
    if loads.empty:
        return pd.DataFrame(columns=columns)
    threshold = loads["load"].quantile(.75)
    loads["group"] = loads["load"].map(lambda x: "Higher load" if x > threshold else "Other active days")
    loads["outcome_date"] = loads["date"] + pd.Timedelta(days=lag)
    loads = loads[loads["outcome_date"] <= pd.Timestamp(end)]
    stats = daily.reindex(columns=["date", "hrv_overnight", "resting_hr"]).copy()
    stats["date"] = pd.to_datetime(stats["date"]).dt.normalize()
    stats = stats.drop_duplicates("date")
    result = loads.merge(stats, on="date", how="left").merge(stats.rename(columns={"date": "outcome_date"}), on="outcome_date", how="left", suffixes=("_before", "_after"))
    for metric, output in [("hrv_overnight", "hrv_change"), ("resting_hr", "resting_hr_change")]:
        result[output] = pd.to_numeric(result[metric + "_after"], errors="coerce") - pd.to_numeric(result[metric + "_before"], errors="coerce")
    return result.reindex(columns=columns)


def compare_groups(frame, group, metric, first, second, minimum=5):
    """Suppress numerical conclusions when either group has too few days."""
    a = frame.loc[frame[group] == first, metric].dropna()
    b = frame.loc[frame[group] == second, metric].dropna()
    return {"first_n": len(a), "second_n": len(b), "difference": float(a.median() - b.median()) if min(len(a), len(b)) >= minimum else None}
