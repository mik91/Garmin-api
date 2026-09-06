import json
import math
from datetime import date, timedelta

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

RECOMMENDED_WEEKLY_KM = {
    "5K": 15.0,
    "10K": 25.0,
    "Half marathon": 35.0,
    "Marathon": 55.0,
}

RECOMMENDED_LONG_RUN_KM = {
    "5K": 5.0,
    "10K": 10.0,
    "Half marathon": 18.0,
    "Marathon": 30.0,
}

RECOMMENDED_RUNS_PER_WEEK = {
    "5K": 2.5,
    "10K": 3.0,
    "Half marathon": 3.5,
    "Marathon": 4.0,
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
    return {
        "seconds": seconds,
        "formatted": format_seconds(seconds),
        "source_effort": source,
        "age_days": max(0, (as_of - date.fromisoformat(source["date"])).days),
    }


def _training_profile(activities: pd.DataFrame, as_of: date) -> dict:
    """Summarize only recent running used by the independent model."""
    if activities.empty:
        return {"weekly_km": 0.0, "runs_per_week": 0.0, "longest_run_km": 0.0, "run_count": 0}
    runs = analysis.ensure_activity_categories(activities)
    runs = runs[runs["category"] == "Running"].copy()
    runs["activity_date"] = pd.to_datetime(runs["start_time"], errors="coerce").dt.date
    start = as_of - timedelta(days=41)
    runs = runs[(runs["activity_date"] >= start) & (runs["activity_date"] <= as_of)]
    distances = pd.to_numeric(runs.get("distance_m"), errors="coerce").fillna(0) / 1000
    return {
        "weekly_km": round(float(distances.sum()) / 6, 1),
        "runs_per_week": round(len(runs) / 6, 1),
        "longest_run_km": round(float(distances.max()), 1) if len(distances) else 0.0,
        "run_count": int(len(runs)),
        "window_days": 42,
    }


def _independent_prediction(
    local: dict | None,
    label: str,
    target_distance_m: float,
    profile: dict,
) -> dict | None:
    """Adjust race equivalence with bounded, auditable training-support penalties."""
    if local is None:
        return None
    source = local["source_effort"]
    base_seconds = local["seconds"]
    age_penalty = min(local["age_days"] / 180 * 0.02, 0.04)

    volume_shortfall = max(0.0, 1 - profile["weekly_km"] / RECOMMENDED_WEEKLY_KM[label])
    long_run_shortfall = max(0.0, 1 - profile["longest_run_km"] / RECOMMENDED_LONG_RUN_KM[label])
    frequency_shortfall = max(0.0, 1 - profile["runs_per_week"] / RECOMMENDED_RUNS_PER_WEEK[label])
    extrapolating_up = target_distance_m > source["distance_m"]
    endurance_penalty = (
        min(0.12, volume_shortfall * 0.06 + long_run_shortfall * 0.06 + frequency_shortfall * 0.025)
        if extrapolating_up else 0.0
    )
    total_adjustment = min(0.16, age_penalty + endurance_penalty)
    expected = base_seconds * (1 + total_adjustment)

    distance_ratio = max(target_distance_m, source["distance_m"]) / min(
        target_distance_m, source["distance_m"]
    )
    uncertainty = min(
        0.10,
        0.035 + min(0.02, math.log(distance_ratio) * 0.007) + min(0.015, local["age_days"] / 365 * 0.015),
    )
    confidence = "high" if local["age_days"] <= 60 and distance_ratio <= 2.2 else "medium"
    return {
        "expected_seconds": expected,
        "expected": format_seconds(expected),
        "range_low_seconds": expected * (1 - uncertainty),
        "range_high_seconds": expected * (1 + uncertainty),
        "range": [format_seconds(expected * (1 - uncertainty)), format_seconds(expected * (1 + uncertainty))],
        "confidence": confidence,
        "base_equivalent_seconds": base_seconds,
        "base_equivalent": format_seconds(base_seconds),
        "source_effort": source,
        "source_age_days": local["age_days"],
        "recency_adjustment_pct": round(age_penalty * 100, 1),
        "endurance_adjustment_pct": round(endurance_penalty * 100, 1),
        "total_adjustment_pct": round(total_adjustment * 100, 1),
        "uncertainty_pct": round(uncertainty * 100, 1),
    }


def build_model(
    activities: pd.DataFrame,
    garmin_history: pd.DataFrame,
    as_of: date | None = None,
) -> dict:
    as_of = as_of or date.today()
    efforts = credible_race_efforts(activities, as_of)
    profile = _training_profile(activities, as_of)
    latest = None
    if not garmin_history.empty:
        eligible = garmin_history[pd.to_datetime(garmin_history["date"]).dt.date <= as_of]
        if not eligible.empty:
            latest = eligible.sort_values("date").iloc[-1]

    predictions = {}
    for label, target_distance_m in DISTANCES_M.items():
        garmin_seconds = _number(latest.get(GARMIN_COLUMNS[label])) if latest is not None else None
        local = _best_local_prediction(efforts, target_distance_m, as_of)
        independent = _independent_prediction(local, label, target_distance_m, profile)
        if independent is None:
            continue
        disagreement = (
            abs(independent["expected_seconds"] - garmin_seconds) / garmin_seconds
            if garmin_seconds else None
        )
        predictions[label] = {
            **independent,
            # Garmin is deliberately comparison-only. It is not an input to any
            # expected-time, adjustment, confidence, or range calculation above.
            "garmin_comparison_seconds": garmin_seconds,
            "garmin_comparison": format_seconds(garmin_seconds),
            "difference_from_garmin_pct": round(disagreement * 100, 1) if disagreement is not None else None,
        }

    return {
        "as_of": as_of.isoformat(),
        "method": "Independent race equivalence with recency and training-support adjustments",
        "predictions": predictions,
        "credible_efforts": efforts,
        "training_profile": profile,
        "limitations": [
            "Estimates describe current fitness; future training gains are not assumed.",
            "Weather, course elevation, pacing, taper quality, and illness can move race-day results outside the range.",
            "The model uses only activities explicitly labelled as races or time trials as performance anchors.",
            "Garmin's prediction is displayed for comparison and is never used in the independent calculation.",
            "This is a statistical model, not machine learning: there are not yet enough labeled race outcomes to train and validate an athlete-specific ML model honestly.",
        ],
    }
