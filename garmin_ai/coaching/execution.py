"""How a quality session went: the workout's steps against the laps the watch recorded.

When a [TP] workout runs on the watch, every lap carries `wktStepIndex`, the step it belongs to
in the watch's workout file. That file lists a repeat's steps before the repeat marker, so for
warm-up, 4 x (rep, rest), cool-down the indexes are warm-up 0, rep 1, rest 2, (repeat 3),
cool-down 4. Consecutive laps with the same index are one step (auto-laps split long steps).

Per work rep: pace vs the target range (or HR vs a bpm range), completed or cut short. Per
session: share of reps on target, pace bias (signed s/km off the range), pace fade and HR drift
from the first to the last rep, and a decision:
- advance: done and on target, HR controlled -> the progression moves on;
- repeat:  reps missed, mostly slow, or a big fade / HR drift -> the next session of that type
           repeats this step (a "hold" saved with the season plan);
- ease / pace up: consistently off the range in one direction over several sessions -> flag it,
           the paces come from training_paces and a checkpoint or retest settles them.
Thresholds are in rules.json "execution".
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from statistics import median

from garmin_ai import workouts
from garmin_ai.coaching import history

DEFAULTS = {"pace_tolerance_s": 3, "on_target_share": 0.75, "complete_share": 0.9, "cut_short_share": 0.75,
            "max_fade_s": 8, "max_hr_drift": 12, "bias_s": 5, "bias_sessions": 2}
EASY_STEPS = ("warmup", "cooldown", "recovery", "rest")
KIND_NAMES = (("race_pace", r"goal|race pace|10k pace|cutdown|alternation"),
              ("vo2", r"vo2|\bi\b|interval|800|1 ?km|1000|hill repeat|pyramid.*\bi\b"),
              ("threshold", r"threshold|tempo|\bt\b|cruise|sub-threshold|progression|fartlek"))


def settings(rules: dict | None) -> dict:
    return {**DEFAULTS, **((rules or {}).get("execution") or {})}


def _key(value, field):
    return (value or {}).get(field) if isinstance(value, dict) else value


# --- workout file order -------------------------------------------------------------------

def fit_steps(payload: dict) -> list[dict | None]:
    """Steps in the watch's order: a repeat's steps, then a marker (None) for the repeat itself."""
    order: list[dict | None] = []
    for segment in payload.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                for child in step.get("workoutSteps") or []:
                    order.append({**child, "_iterations": step.get("numberOfIterations") or 1})
                order.append(None)
            else:
                order.append({**step, "_iterations": 1})
    return order


def _target(step: dict) -> dict:
    kind = _key(step.get("targetType"), "workoutTargetTypeKey")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    if kind == "pace.zone" and one and two:
        fast, slow = sorted((1000 / one, 1000 / two))
        return {"pace_s_per_km": [round(fast), round(slow)]}
    if kind == "heart.rate.zone" and one and two:
        return {"hr_bpm": sorted([one, two])}
    if kind == "heart.rate.zone" and step.get("zoneNumber"):
        return {"hr_zone": step["zoneNumber"]}
    return {}


def instances(splits_json) -> list[dict]:
    """Laps grouped into executed steps (consecutive laps with the same wktStepIndex)."""
    try:
        laps = (json.loads(splits_json) if isinstance(splits_json, str) else splits_json or {}).get("lapDTOs") or []
    except (TypeError, ValueError, AttributeError):
        return []
    groups: list[dict] = []
    for lap in laps:
        index = lap.get("wktStepIndex")
        if index is None:
            continue
        duration, distance = float(lap.get("duration") or 0), float(lap.get("distance") or 0)
        hr = lap.get("averageHR")
        if groups and groups[-1]["step_index"] == index:
            g = groups[-1]
            if hr and duration:
                g["_hr_time"] += hr * duration
                g["_hr_seconds"] += duration
            g["duration_s"] += duration
            g["distance_m"] += distance
            g["max_hr"] = max(g["max_hr"] or 0, lap.get("maxHR") or 0) or None
        else:
            groups.append({"step_index": index, "duration_s": duration, "distance_m": distance,
                           "max_hr": lap.get("maxHR"), "_hr_time": (hr or 0) * duration,
                           "_hr_seconds": duration if hr else 0})
    for g in groups:
        g["avg_hr"] = round(g.pop("_hr_time") / g["_hr_seconds"]) if g["_hr_seconds"] else None
        g.pop("_hr_seconds")
        g["duration_s"], g["distance_m"] = round(g["duration_s"], 1), round(g["distance_m"])
    return groups


# --- one session --------------------------------------------------------------------------

def analyze(payload: dict, splits_json, rules: dict | None = None) -> dict:
    cfg = settings(rules)
    steps = fit_steps(payload)
    work = {i: s for i, s in enumerate(steps)
            if s and _key(s.get("stepType"), "stepTypeKey") not in EASY_STEPS}
    planned = sum(s["_iterations"] for s in work.values())
    done = instances(splits_json)
    if not done:
        return {"status": "no_laps", "note": "No laps with workout steps: the session was not run as this workout."}
    reps = []
    for rep in done:
        step = work.get(rep["step_index"])
        if not step:
            continue
        end = _key(step.get("endCondition"), "conditionTypeKey")
        planned_value = float(step.get("endConditionValue") or 0)
        if end == "time" and planned_value:
            share = rep["duration_s"] / planned_value
        elif end == "distance" and planned_value:
            share = rep["distance_m"] / planned_value
        else:
            share = 1.0
        target = _target(step)
        pace = rep["duration_s"] / rep["distance_m"] * 1000 if rep["distance_m"] >= 50 else None
        entry = {"rep": len(reps) + 1, "duration_s": rep["duration_s"], "distance_m": rep["distance_m"],
                 "pace": workouts.format_pace(pace) if pace else None, "pace_s_per_km": round(pace) if pace else None,
                 "avg_hr": rep["avg_hr"], "complete": share >= cfg["complete_share"], "target": target}
        if pace and "pace_s_per_km" in target:
            fast, slow = target["pace_s_per_km"]
            tol = cfg["pace_tolerance_s"]
            off = pace - slow if pace > slow else pace - fast if pace < fast else 0.0
            entry["off_s_per_km"] = round(off, 1)
            entry["result"] = "on" if abs(off) <= tol else ("slow" if off > 0 else "fast")
        elif rep["avg_hr"] and "hr_bpm" in target:
            low, high = target["hr_bpm"]
            entry["result"] = "on" if low - 2 <= rep["avg_hr"] <= high + 2 else ("high" if rep["avg_hr"] > high else "low")
        reps.append(entry)

    complete = sum(1 for r in reps if r["complete"])
    judged = [r for r in reps if "result" in r]
    on = sum(1 for r in judged if r["result"] == "on")
    offs = [r["off_s_per_km"] for r in reps if "off_s_per_km" in r]
    paced = [r for r in reps if r["pace_s_per_km"] and r["complete"]]
    # HR lags at the start of a session, and does not settle within short reps: drift is measured
    # from the second rep to the last, on reps of 3 minutes or more.
    timed_hr = [r for r in reps if r["avg_hr"] and r["complete"] and r["duration_s"] >= 180][1:]
    half = len(paced) // 2
    fade = (round(sum(r["pace_s_per_km"] for r in paced[-half:]) / half
                  - sum(r["pace_s_per_km"] for r in paced[:half]) / half) if half >= 2 else None)
    late_slow = bool(half >= 2 and all(r.get("result") in ("slow",) for r in paced[-1:]))
    result = {
        "status": "analyzed", "planned_reps": planned, "reps_done": len(reps), "reps_complete": complete,
        "completion": round(complete / planned, 2) if planned else None,
        "on_target_share": round(on / len(judged), 2) if judged else None,
        "pace_bias_s_per_km": round(median(offs), 1) if offs else None,
        "pace_fade_s_per_km": fade,
        "hr_drift_bpm": (timed_hr[-1]["avg_hr"] - timed_hr[0]["avg_hr"]) if len(timed_hr) >= 2 else None,
        "reps": reps,
    }
    reasons = []
    if planned and complete / planned < cfg["cut_short_share"]:
        decision = "repeat"
        reasons.append(f"cut short: {complete} of {planned} reps")
    else:
        if planned and complete / planned < cfg["complete_share"]:
            reasons.append(f"{complete} of {planned} reps completed")
        slow = sum(1 for r in judged if r["result"] in ("slow", "low"))
        if judged and slow / len(judged) >= 0.5:
            reasons.append(f"{slow} of {len(judged)} reps slower than the range")
        if (fade or 0) > cfg["max_fade_s"] and (late_slow or not judged):
            reasons.append(f"faded {fade} s/km from the first to the second half of the reps")
        if (result["hr_drift_bpm"] or 0) > cfg["max_hr_drift"]:
            reasons.append(f"HR drifted +{result['hr_drift_bpm']} bpm across the reps")
        decision = "repeat" if reasons else "advance"
        if decision == "advance":
            reasons.append("reps done and on target" if judged else "reps done (no pace target to check)")
    bias = result["pace_bias_s_per_km"]
    if bias is not None and abs(bias) >= cfg["bias_s"]:
        result["pace_note"] = (f"Reps ran {abs(bias):g} s/km {'faster' if bias < 0 else 'slower'} than the range.")
    result.update(decision=decision, reasons=reasons)
    return result


# --- sessions in a date range ---------------------------------------------------------------

def kind_of(name: str, payload: dict, paces: dict | None, goal_s_per_km: float | None) -> str:
    """threshold, vo2 or race_pace: from the work steps' pace target if there is one, else the name."""
    targets = [_target(s).get("pace_s_per_km") for s in fit_steps(payload)
               if s and _key(s.get("stepType"), "stepTypeKey") not in EASY_STEPS]
    targets = [t for t in targets if t]
    table = (paces or {}).get("paces") or {}
    if targets and table:
        middle = sum(sum(t) / 2 for t in targets) / len(targets)
        options = {"threshold": sum(table["threshold"]["seconds_per_km"]) / 2,
                   "vo2": sum(table["interval"]["seconds_per_km"]) / 2}
        if goal_s_per_km:
            options["race_pace"] = goal_s_per_km
        return min(options, key=lambda k: abs(options[k] - middle))
    lowered = name.lower()
    for kind, pattern in KIND_NAMES:
        if re.search(pattern, lowered):
            return kind
    return "quality"


def _activity_for(conn, day: str, name: str, matched_id) -> dict | None:
    if matched_id:
        row = conn.execute("SELECT activity_id, splits_json, raw_json FROM activities WHERE activity_id = ?",
                           (int(matched_id),)).fetchone()
        if row:
            return dict(row)
    for row in conn.execute("SELECT activity_id, splits_json, raw_json FROM activities "
                            "WHERE substr(start_time, 1, 10) = ? AND type LIKE '%running%'", (day,)):
        try:
            title = json.loads(row["raw_json"] or "{}").get("activityName") or ""
        except ValueError:
            title = ""
        if name.lower() in title.lower():
            return dict(row)
    return None


def reviews(conn, start: str, end: str, today: date | None = None) -> list[dict]:
    """Every [TP] quality run scheduled in [start, end] with how it went."""
    from garmin_ai import coach
    from garmin_ai.coaching import review, season
    rules = history.rules(conn)
    today = (today or date.today()).isoformat()
    paces = coach.current_paces()
    try:
        race = season.a_race(conn)
    except history.HistoryError:
        race = None
    goal = season.goal_pace(race) if race else None
    goal_s = goal["seconds"] / {"5K": 5, "10K": 10, "half": 21.0975, "marathon": 42.195}[season.distance_key(race)] \
        if goal else None
    plans = {str(p.get("scheduled_workout_id")): p for p in review._calendar(start, end)}
    rows = conn.execute(
        """SELECT s.schedule_id, s.date, w.workout_id, w.name, w.payload_json FROM coach_schedules s
           JOIN coach_workouts w ON w.workout_id = s.workout_id
           WHERE s.date BETWEEN ? AND ? AND w.sport = 'running' ORDER BY s.date""", (start, end)).fetchall()
    out = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        session = workouts.to_session(row["date"], payload, paces)
        if session["intensity"] not in ("quality", "race"):
            continue
        entry = {"date": row["date"], "name": row["name"], "schedule_id": row["schedule_id"],
                 "kind": kind_of(row["name"], payload, paces, goal_s)}
        plan = plans.get(row["schedule_id"], {})
        activity = _activity_for(conn, row["date"], row["name"], plan.get("matched_activity_id"))
        if row["date"] > today or (row["date"] == today and not activity):
            entry["status"] = "upcoming"
        elif not activity:
            entry.update(status="missed", decision="repeat", reasons=["not done"])
        elif not activity.get("splits_json"):
            entry.update(status="no_laps", activity_id=activity["activity_id"],
                         note="Laps not synced yet; they come with the next sync.")
        else:
            entry.update(activity_id=activity["activity_id"], **analyze(payload, activity["splits_json"], rules))
        out.append(entry)
    return out


# --- feedback into the season plan ------------------------------------------------------------

def _monday(day: str) -> date:
    value = date.fromisoformat(day)
    return value - timedelta(days=value.weekday())


def apply_holds(conn, found: list[dict]) -> list[dict]:
    """For each session that needs repeating, save a hold so the next session of that type
    repeats the step instead of advancing. One hold per session; returns the new holds."""
    from garmin_ai.coaching import season
    anchor = season.anchor(conn)
    if not anchor:
        return []
    holds = list(anchor.get("holds") or [])
    seen = {h.get("schedule_id") for h in holds}
    added = []
    for entry in found:
        if entry.get("decision") != "repeat" or entry.get("kind") not in ("threshold", "vo2", "race_pace"):
            continue
        if entry["schedule_id"] in seen:
            continue
        hold = {"type": entry["kind"], "from_week": (_monday(entry["date"]) + timedelta(days=7)).isoformat(),
                "schedule_id": entry["schedule_id"], "session": entry["name"], "date": entry["date"],
                "reason": "; ".join(entry.get("reasons") or [])}
        holds.append(hold)
        added.append(hold)
    if added:
        history.set_profile(conn, "season_plan", json.dumps({**anchor, "holds": holds}),
                            "Repeat step: " + "; ".join(f"{h['session']} ({h['date']}): {h['reason']}" for h in added))
    return added


def pace_bias(found: list[dict], rules: dict | None = None) -> dict | None:
    """The same direction off the pace range in the last `bias_sessions` analyzed sessions."""
    cfg = settings(rules)
    recent = [e["pace_bias_s_per_km"] for e in found if e.get("status") == "analyzed"
              and e.get("pace_bias_s_per_km") is not None][-cfg["bias_sessions"]:]
    if len(recent) < cfg["bias_sessions"]:
        return None
    if all(b <= -cfg["bias_s"] for b in recent):
        return {"direction": "fast", "s_per_km": round(sum(recent) / len(recent), 1),
                "message": "Reps keep running faster than the range: paces may be too easy. A checkpoint or "
                           "race will confirm; until then keep reps in the range (faster is not better)."}
    if all(b >= cfg["bias_s"] for b in recent):
        return {"direction": "slow", "s_per_km": round(sum(recent) / len(recent), 1),
                "message": "Reps keep running slower than the range: paces may be too ambitious for now, "
                           "or fatigue is building. Check readiness; consider easing paces until the next checkpoint."}
    return None
