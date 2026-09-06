import hashlib
import json
from datetime import date

import pandas as pd

from garmin_ai import analysis


def _first(mapping: dict, *keys, default=None):
    for key in keys:
        value = mapping.get(key)
        if value is not None:
            return value
    return default


def _number(value):
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def scheduled_items(payload) -> list[dict]:
    """Find calendar entries across the response shapes used by Garmin endpoints."""
    found = []

    def visit(value):
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        if value.get("calendarDate") and (
            value.get("workoutScheduleId") is not None
            or value.get("scheduleId") is not None
            or isinstance(value.get("workout"), dict)
        ):
            found.append(value)
            return
        for child in value.values():
            if isinstance(child, (dict, list)):
                visit(child)

    visit(payload)
    return found


def normalize_scheduled_workout(item: dict, seen_at: str) -> dict:
    workout = item.get("workout") if isinstance(item.get("workout"), dict) else {}
    workout_id = _first(item, "workoutId", "workout_id")
    if workout_id is None:
        workout_id = _first(workout, "workoutId", "id")
    schedule_id = _first(item, "workoutScheduleId", "scheduleId", "scheduledWorkoutId")
    if schedule_id is None:
        digest = hashlib.sha1(
            json.dumps(item, sort_keys=True, default=str).encode("utf-8")
        ).hexdigest()[:16]
        schedule_id = f"calendar:{digest}"

    sport_value = _first(item, "sportType", "sport", "activityType")
    if sport_value is None:
        sport_value = _first(workout, "sportType", "sport", "activityType")
    if isinstance(sport_value, dict):
        sport_value = _first(
            sport_value, "typeKey", "sportTypeKey", "displayOrder", "name"
        )

    return {
        "scheduled_workout_id": str(schedule_id),
        "workout_id": str(workout_id) if workout_id is not None else None,
        "date": str(item.get("calendarDate"))[:10],
        "name": _first(item, "newName", "workoutName", "name")
        or _first(workout, "workoutName", "name")
        or "Planned workout",
        "sport": str(sport_value) if sport_value is not None else None,
        "duration_s": _number(
            _first(item, "estimatedDurationInSecs", "duration", "durationSeconds")
            or _first(workout, "estimatedDurationInSecs", "duration", "durationSeconds")
        ),
        "distance_m": _number(
            _first(item, "estimatedDistanceInMeters", "distance", "distanceMeters")
            or _first(workout, "estimatedDistanceInMeters", "distance", "distanceMeters")
        ),
        "associated_activity_id": _first(item, "associatedActivityId", "activityId"),
        "raw_json": json.dumps(item, default=str),
        "last_seen_at": seen_at,
    }


def _similarity(planned, actual) -> float | None:
    planned = _number(planned)
    actual = _number(actual)
    if planned is None or actual is None or planned <= 0 or actual <= 0:
        return None
    return max(0.0, 1.0 - abs(actual - planned) / max(actual, planned))


def _sport_category(value) -> str:
    return analysis.activity_category(value)


def match_score(planned: pd.Series | dict, activity: pd.Series | dict) -> float:
    """Score a local candidate from sport plus planned duration and distance."""
    duration_score = _similarity(planned.get("duration_s"), activity.get("duration_s"))
    distance_score = _similarity(planned.get("distance_m"), activity.get("distance_m"))

    weighted = []
    if planned.get("sport"):
        planned_category = _sport_category(planned.get("sport"))
        actual_category = activity.get("category") or _sport_category(activity.get("type"))
        weighted.append((1.0 if planned_category == actual_category else 0.0, 0.55))
    if duration_score is not None:
        weighted.append((duration_score, 0.25))
    if distance_score is not None:
        weighted.append((distance_score, 0.20))
    if not weighted:
        return 0.0
    total_weight = sum(weight for _, weight in weighted)
    return sum(value * weight for value, weight in weighted) / total_weight


def build_adherence(
    scheduled: pd.DataFrame,
    activities: pd.DataFrame,
    overrides: pd.DataFrame | None = None,
    today: date | None = None,
) -> pd.DataFrame:
    columns = [
        "scheduled_workout_id", "date", "name", "sport", "duration_s", "distance_m",
        "status", "confidence", "match_score", "matched_activity_id", "matched_activity_name",
        "manual_override",
    ]
    if scheduled.empty:
        return pd.DataFrame(columns=columns)

    today = today or date.today()
    activities = analysis.ensure_activity_categories(activities).copy()
    if not activities.empty:
        activities["activity_date"] = pd.to_datetime(activities["start_time"]).dt.date
    override_map = {}
    if overrides is not None and not overrides.empty:
        override_map = overrides.set_index("scheduled_workout_id").to_dict("index")

    results = []
    used_activity_ids = set()
    for _, planned in scheduled.sort_values("date").iterrows():
        schedule_id = str(planned["scheduled_workout_id"])
        planned_date = pd.to_datetime(planned["date"]).date()
        matched = None
        score = None
        matched_activity_id = None
        associated_id = planned.get("associated_activity_id")
        if pd.notna(associated_id):
            matched_activity_id = int(associated_id)
            score = 1.0
            if not activities.empty:
                direct = activities[
                    activities["activity_id"].astype(str) == str(matched_activity_id)
                ]
                if not direct.empty:
                    matched = direct.iloc[0]

        if matched is None and score is None and not activities.empty:
            candidates = activities[
                (activities["activity_date"] == planned_date)
                & (~activities["activity_id"].isin(used_activity_ids))
            ]
            if not candidates.empty:
                scored = [(match_score(planned, row), row) for _, row in candidates.iterrows()]
                best_score, best = max(scored, key=lambda pair: pair[0])
                if best_score >= 0.55:
                    matched, score = best, best_score

        if matched is not None:
            matched_activity_id = matched.get("activity_id")
        status = "completed" if matched_activity_id is not None else (
            "missed" if planned_date < today else "planned"
        )
        manual = schedule_id in override_map
        if manual:
            override = override_map[schedule_id]
            status = override["status"]
            override_activity_id = override.get("matched_activity_id")
            if pd.notna(override_activity_id) and not activities.empty:
                selected = activities[
                    activities["activity_id"].astype(str) == str(int(override_activity_id))
                ]
                if not selected.empty:
                    matched, score = selected.iloc[0], 1.0
                    matched_activity_id = matched.get("activity_id")
            elif status != "completed":
                matched, score = None, None
                matched_activity_id = None

        if matched is not None:
            used_activity_ids.add(matched["activity_id"])
        confidence = None
        if score is not None:
            confidence = "high" if score >= 0.85 else "medium" if score >= 0.65 else "low"
        results.append({
            "scheduled_workout_id": schedule_id,
            "date": pd.Timestamp(planned_date),
            "name": planned.get("name") or "Planned workout",
            "sport": planned.get("sport"),
            "duration_s": planned.get("duration_s"),
            "distance_m": planned.get("distance_m"),
            "status": status,
            "confidence": confidence,
            "match_score": score,
            "matched_activity_id": matched_activity_id,
            "matched_activity_name": (
                json.loads(matched.get("raw_json") or "{}").get("activityName", matched.get("type"))
                if matched is not None else None
            ),
            "manual_override": manual,
        })
    return pd.DataFrame(results, columns=columns)
