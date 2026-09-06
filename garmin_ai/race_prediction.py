import json
import math
from datetime import date

import pandas as pd

from garmin_ai import analysis


DISTANCES_M = {
    "5K": 5_000.0,
    "10K": 10_000.0,
    "Half marathon": 21_097.5,
    "Marathon": 42_195.0,
}

GARMIN_COLUMNS = {
    "5K": "time_5k_s",
    "10K": "time_10k_s",
    "Half marathon": "time_half_s",
    "Marathon": "time_marathon_s",
}

SPLIT_KEYS = {
    "5K": "fastestSplit_5000",
    "10K": "fastestSplit_10000",
    "Half marathon": "fastestSplit_21097",
    "Marathon": "fastestSplit_42195",
}


def _number(value):
    try:
        value = float(value)
        return value if math.isfinite(value) and value > 0 else None
    except (TypeError, ValueError):
        return None


def format_seconds(value) -> str:
    seconds = _number(value)
    if seconds is None:
        return "-"
    rounded = int(round(seconds))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes}:{secs:02d}"


def normalize_garmin_prediction(item: dict, fetched_at: str) -> dict | None:
    prediction_date = item.get("calendarDate") or date.today().isoformat()
    if not prediction_date:
        return None
    return {
        "date": str(prediction_date)[:10],
        "time_5k_s": _number(item.get("time5K")),
        "time_10k_s": _number(item.get("time10K")),
        "time_half_s": _number(item.get("timeHalfMarathon")),
        "time_marathon_s": _number(item.get("timeMarathon")),
        "raw_json": json.dumps(item, default=str),
        "fetched_at": fetched_at,
    }


def riegel_time(source_seconds: float, source_distance_m: float, target_distance_m: float) -> float:
    """Classic race-equivalence estimate; callers decide whether an effort is credible."""
    return source_seconds * (target_distance_m / source_distance_m) ** 1.06


def credible_race_efforts(activities: pd.DataFrame, as_of: date | None = None) -> list[dict]:
    """Select race/time-trial evidence without treating ordinary training splits as races."""
    if activities.empty:
        return []
    as_of = as_of or date.today()
    runs = analysis.ensure_activity_categories(activities)
    runs = runs[runs["category"] == "Running"].copy()
    if runs.empty:
        return []

    efforts = []
    for _, row in runs.iterrows():
        activity_date = pd.to_datetime(row.get("start_time"), errors="coerce")
        if pd.isna(activity_date) or activity_date.date() > as_of:
            continue
        if (as_of - activity_date.date()).days > 365:
            continue
        try:
            raw = json.loads(row.get("raw_json") or "{}")
        except (TypeError, ValueError):
            raw = {}
        name = str(raw.get("activityName") or row.get("type") or "Run")
        normalized_name = name.casefold()
        verified = any(
            marker in normalized_name
            for marker in ("race", "time trial", "time-trial", "parkrun")
        )
        if not verified:
            continue
        activity_distance = _number(row.get("distance_m")) or 0
        for label, distance_m in DISTANCES_M.items():
            if not 0.97 <= activity_distance / distance_m <= 1.08:
                continue
            split_seconds = _number(raw.get(SPLIT_KEYS[label]))
            if split_seconds is None:
                split_seconds = _number(raw.get("movingDuration")) or _number(row.get("duration_s"))
            if split_seconds is None:
                continue
            efforts.append({
                "distance": label,
                "distance_m": distance_m,
                "seconds": split_seconds,
                "formatted": format_seconds(split_seconds),
                "date": activity_date.date().isoformat(),
                "activity": name,
                "evidence": "verified race/time trial",
            })
    return efforts


def _best_local_prediction(
    efforts: list[dict], target_distance_m: float, as_of: date
) -> dict | None:
    if not efforts:
        return None
    candidates = []
    for effort in efforts:
        predicted = riegel_time(effort["seconds"], effort["distance_m"], target_distance_m)
        age_days = max(0, (as_of - date.fromisoformat(effort["date"])).days)
        # Compare performances after a small staleness penalty. A recent easy or
        # poorly executed race must not displace a substantially stronger result.
        rank = predicted * (1 + min(age_days, 365) / 365 * 0.03)
        candidates.append((rank, predicted, effort))
    _, seconds, source = min(candidates, key=lambda candidate: candidate[0])
    return {"seconds": seconds, "formatted": format_seconds(seconds), "source_effort": source}


def build_model(
    activities: pd.DataFrame,
    garmin_history: pd.DataFrame,
    as_of: date | None = None,
) -> dict:
    as_of = as_of or date.today()
    efforts = credible_race_efforts(activities, as_of)
    latest = None
    if not garmin_history.empty:
        eligible = garmin_history[pd.to_datetime(garmin_history["date"]).dt.date <= as_of]
        if not eligible.empty:
            latest = eligible.sort_values("date").iloc[-1]

    predictions = {}
    for label, target_distance_m in DISTANCES_M.items():
        garmin_seconds = _number(latest.get(GARMIN_COLUMNS[label])) if latest is not None else None
        local = _best_local_prediction(efforts, target_distance_m, as_of)
        local_seconds = local["seconds"] if local else None

        # Garmin is the primary calibrated model. Local race equivalence is an independent
        # transparent check and widens uncertainty when the two disagree.
        expected = garmin_seconds or local_seconds
        if expected is None:
            continue
        disagreement = (
            abs(local_seconds - garmin_seconds) / garmin_seconds
            if garmin_seconds and local_seconds else None
        )
        uncertainty = max(0.025, (disagreement or 0) * 0.65)
        uncertainty = min(0.08, uncertainty if garmin_seconds and local else max(0.05, uncertainty))
        confidence = "high" if garmin_seconds and local and (disagreement or 0) <= 0.05 else (
            "medium" if garmin_seconds or local else "low"
        )
        predictions[label] = {
            "expected_seconds": expected,
            "expected": format_seconds(expected),
            "range_low_seconds": expected * (1 - uncertainty),
            "range_high_seconds": expected * (1 + uncertainty),
            "range": [format_seconds(expected * (1 - uncertainty)), format_seconds(expected * (1 + uncertainty))],
            "confidence": confidence,
            "official_garmin_seconds": garmin_seconds,
            "official_garmin": format_seconds(garmin_seconds),
            "local_riegel_seconds": local_seconds,
            "local_riegel": format_seconds(local_seconds),
            "difference_pct": round(disagreement * 100, 1) if disagreement is not None else None,
            "local_source": local["source_effort"] if local else None,
        }

    return {
        "as_of": as_of.isoformat(),
        "method": "Garmin Race Predictor with verified-race Riegel cross-check",
        "predictions": predictions,
        "credible_efforts": efforts,
        "limitations": [
            "Estimates describe current fitness; future training gains are not assumed.",
            "Weather, course elevation, pacing, taper quality, and illness can move race-day results outside the range.",
            "The local cross-check uses only activities explicitly labelled as races or time trials.",
        ],
    }
