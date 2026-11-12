"""Looking back and ahead: week review, easy-pace trend, readiness calibration, season phase.

All functions take an open sqlite3 connection with row_factory = sqlite3.Row.
"""
from __future__ import annotations

import json
import sqlite3
from datetime import date, timedelta
from statistics import mean, median

from garmin_ai import load_summary, workouts
from garmin_ai.coaching import history
from garmin_ai.coaching.readiness import assess_readiness

EASY_LABELS = ("RECOVERY", "AEROBIC_BASE")
DONE = ("completed",)
NOT_DONE = ("missed", "skipped")


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _calendar(start: str, end: str) -> list[dict]:
    """Every planned workout (all sources) with completion status, from the synced calendar."""
    from garmin_ai import web_dashboard
    plans = web_dashboard.payload({"start": [start], "end": [end]})["calendar"]
    return [p for p in plans if start <= str(p.get("date", ""))[:10] <= end]


def _coach_ids(conn) -> set[str]:
    try:
        return {r[0] for r in conn.execute("SELECT schedule_id FROM coach_schedules")}
    except sqlite3.Error:
        return set()


def _readiness(conn, as_of: str, rules: dict) -> dict:
    result = assess_readiness(history.daily_rows(conn, as_of), as_of, [], rules)
    return {"as_of": as_of, "status": result["status"], "points": result["points"], "signals": result["signals"]}


# --- season phase ----------------------------------------------------------------------

def parse_races(text: str | None) -> list[dict]:
    """The profile's races section: a JSON list of {name, date, priority A|B|C, goal?, distance?}."""
    if not text:
        return []
    try:
        races = json.loads(text)
    except ValueError:
        raise history.HistoryError(
            'races must be a JSON list, e.g. [{"name": "Valencia Marathon", "date": "2026-12-06", '
            '"priority": "A", "goal": "sub 3:30", "distance": "marathon"}].') from None
    if not isinstance(races, list):
        raise history.HistoryError("races must be a JSON list of races.")
    for race in races:
        if not isinstance(race, dict) or not race.get("name"):
            raise history.HistoryError("Each race needs at least name, date and priority.")
        race["date"] = history._iso(race.get("date"), f"date of {race['name']}")
        if not race["date"]:
            raise history.HistoryError(f"{race['name']} needs a date (YYYY-MM-DD).")
        race["priority"] = str(race.get("priority") or "B").upper()
        if race["priority"] not in ("A", "B", "C"):
            raise history.HistoryError(f"{race['name']}: priority must be A, B or C.")
    return sorted(races, key=lambda r: r["date"])


def season_phase(races: list[dict], as_of: str, rules: dict) -> dict:
    """base / build / peak / taper / race_week / recovery from the next A race (weeks counted
    from the Monday of as_of). Phase lengths are in rules.json "phases"."""
    cfg = rules.get("phases", {})
    recovery_days = 7 * cfg.get("recovery_weeks_after_race", 1)
    today = date.fromisoformat(as_of)
    monday = _monday(today)
    result = {"as_of": as_of, "races_known": len(races)}
    recent = [r for r in races if r["priority"] in ("A", "B")
              and 0 <= (today - date.fromisoformat(r["date"])).days < recovery_days]
    upcoming = [r for r in races if r["date"] >= as_of]
    a_race = next((r for r in upcoming if r["priority"] == "A"), None)
    if a_race:  # the distance sets the taper (e.g. 1 week for a 10K, 3 for a marathon)
        from garmin_ai.coaching.season import phases_for
        cfg = phases_for(a_race, rules)
    taper, peak, build = cfg.get("taper_weeks", 2), cfg.get("peak_weeks", 4), cfg.get("build_weeks", 10)
    if a_race:
        weeks = (date.fromisoformat(a_race["date"]) - monday).days // 7
        phase = ("race_week" if weeks == 0 else "taper" if weeks <= taper else "peak" if weeks <= taper + peak
                 else "build" if weeks <= taper + peak + build else "base")
        result.update(next_a_race=a_race, weeks_to_a_race=weeks, phase=phase)
    else:
        result.update(phase="base", note="No upcoming A race in the profile; ask the athlete for the next goal.")
    if recent:
        result.update(phase="recovery", recovering_from=recent[-1],
                      note="Easy week after a race: no quality, reduced volume.")
    others = [r for r in upcoming if r is not a_race and r["priority"] != "A"
              and (date.fromisoformat(r["date"]) - today).days <= 56]
    if others:
        result["tune_up_races"] = others
    return result


def season(conn, as_of: str | None = None, rules: dict | None = None) -> dict:
    as_of = as_of or date.today().isoformat()
    row = conn.execute("SELECT content FROM coach_profile WHERE section = 'races'").fetchone()
    try:
        races = parse_races(row["content"] if row else None)
    except history.HistoryError as error:
        return {"as_of": as_of, "phase": "unknown", "note": str(error)}
    return season_phase(races, as_of, rules or history.rules(conn))


# --- week review ------------------------------------------------------------------------

def week_review(conn, week_start: str | None = None) -> dict:
    """Planned vs done for one Monday-Sunday week (default: last complete week)."""
    rules = history.rules(conn)
    monday = _monday(date.fromisoformat(week_start)) if week_start else _monday(date.today()) - timedelta(days=7)
    sunday = monday + timedelta(days=6)
    first, last = monday.isoformat(), sunday.isoformat()

    coach_ids = _coach_ids(conn)
    calendar = _calendar(first, last)
    planned = [{"date": str(p["date"])[:10], "name": p.get("name"), "sport": p.get("sport"),
                "status": p.get("status"), "created_by_coach": str(p.get("scheduled_workout_id")) in coach_ids,
                "matched_activity": p.get("matched_activity_name")} for p in calendar]
    done = [p for p in planned if p["status"] in DONE]
    missed = [p for p in planned if p["status"] in NOT_DONE]

    paces = None
    try:
        from garmin_ai.coach import current_paces
        paces = current_paces()
    except Exception:
        pass
    coach_sessions = []
    for row in conn.execute(
            """SELECT s.date, w.payload_json FROM coach_schedules s JOIN coach_workouts w
               ON w.workout_id = s.workout_id WHERE s.date BETWEEN ? AND ? ORDER BY s.date""", (first, last)):
        coach_sessions.append(workouts.to_session(row["date"], json.loads(row["payload_json"]), paces,
                                                  easy_s_per_km=history.recent_easy_pace(conn, first)))
    swims = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(distance_m), 0) FROM activities WHERE type LIKE '%swim%' "
        "AND substr(start_time, 1, 10) BETWEEN ? AND ?", (first, last)).fetchone()
    planned_km = round(sum(s.get("km") or 0 for s in coach_sessions if s["sport"] == "run"), 1)
    planned_quality = sum(1 for s in coach_sessions if s["intensity"] in ("quality", "race"))

    actual = load_summary.weekly_load(conn, 1, today=sunday + timedelta(days=1))["weeks"][0]
    run = actual["running"]
    previous_km = load_summary.weekly_load(conn, 1, today=monday)["weeks"][0]["running"]["distance_km"]
    log = history.entries(conn, first, last, limit=20)

    missed_text = ", ".join(f"{p['date'][5:]} {p['name']}" for p in missed) or "none"
    actual_line = (f"{run['distance_km']:.0f} km run in {run['runs']} runs (longest {run['longest_run_km']:.0f} km), "
                   f"{actual['quality_sessions']} quality, {actual['total_hours']:.1f} h total; missed: {missed_text}")
    return {
        "week_start": first, "week_end": last,
        "planned": {"sessions": planned, "count": len(planned), "completed": len(done), "missed": len(missed),
                    "completion_pct": round(len(done) / (len(done) + len(missed)) * 100)
                    if done or missed else None,
                    "coach_run_km_estimate": planned_km if coach_sessions else None,
                    "coach_quality_sessions": planned_quality if coach_sessions else None},
        "actual": {"run_km": run["distance_km"], "runs": run["runs"], "longest_run_km": run["longest_run_km"],
                   "run_km_change_vs_previous_week_pct": round((run["distance_km"] / previous_km - 1) * 100)
                   if previous_km else None,
                   "quality_sessions": actual["quality_sessions"], "hours_by_sport": actual["hours_by_sport"],
                   "swims": swims[0], "swim_m": round(swims[1]),
                   "sessions_by_sport": actual["sessions_by_sport"], "training_load": actual["training_load"]},
        "readiness": {"start": _readiness(conn, (monday - timedelta(days=1)).isoformat(), rules),
                      "end": _readiness(conn, last, rules)},
        "log": log,
        "season_target": _season_target(conn, first),
        "suggested_actual": actual_line,
    }


def _season_target(conn, monday: str) -> dict | None:
    from garmin_ai.coaching import season
    try:
        return season.targets_for(conn, monday)
    except (sqlite3.Error, history.HistoryError):
        return None


# --- easy pace at heart rate -------------------------------------------------------------

def easy_pace_trend(conn, weeks: int = 12, hr_low: float | None = None, hr_high: float | None = None,
                    today: date | None = None) -> dict:
    """Pace of easy runs inside one heart-rate band, per week. Faster at the same HR = fitter;
    slower = fatigue, heat or illness. Default band: median HR of the period's easy runs +/- 4 bpm."""
    weeks = max(2, min(int(weeks or 12), 52))
    today = today or date.today()
    first = _monday(today) - timedelta(days=7 * (weeks - 1))
    rows = conn.execute(
        "SELECT substr(start_time, 1, 10) AS day, distance_m, duration_s, moving_duration_s, avg_hr, raw_json "
        "FROM activities WHERE type LIKE '%running%' AND avg_hr IS NOT NULL AND distance_m >= 3000 "
        "AND substr(start_time, 1, 10) BETWEEN ? AND ?", (first.isoformat(), today.isoformat())).fetchall()
    runs = []
    for row in rows:
        try:
            label = json.loads(row["raw_json"] or "{}").get("trainingEffectLabel")
        except ValueError:
            label = None
        seconds = row["moving_duration_s"] or row["duration_s"]
        if seconds:
            runs.append({"day": row["day"], "km": row["distance_m"] / 1000, "seconds": seconds,
                         "hr": row["avg_hr"], "easy": label in EASY_LABELS or label is None})
    easy_hr = [r["hr"] for r in runs if r["easy"]]
    if hr_low is None or hr_high is None:
        if not easy_hr:
            return {"weeks": [], "note": "No easy runs with heart rate in this period."}
        center = median(easy_hr)
        hr_low, hr_high = (hr_low if hr_low is not None else center - 4), (hr_high if hr_high is not None else center + 4)
    band = [r for r in runs if hr_low <= r["hr"] <= hr_high and r["easy"]]

    table = []
    for index in range(weeks):
        monday = first + timedelta(days=7 * index)
        inside = [r for r in band if monday.isoformat() <= r["day"] <= (monday + timedelta(days=6)).isoformat()]
        km = sum(r["km"] for r in inside)
        entry = {"week_start": monday.isoformat(), "runs": len(inside), "km": round(km, 1)}
        if inside:
            pace = sum(r["seconds"] for r in inside) / km
            entry.update(pace_min_per_km=workouts.format_pace(pace), seconds_per_km=round(pace),
                         avg_hr=round(sum(r["hr"] * r["km"] for r in inside) / km))
        table.append(entry)
    with_data = [w for w in table if w["runs"]]
    summary = {"hr_band": [round(hr_low), round(hr_high)], "runs_in_band": len(band)}
    if len(with_data) >= 4:
        early = mean(w["seconds_per_km"] for w in with_data[:len(with_data) // 2])
        late = mean(w["seconds_per_km"] for w in with_data[len(with_data) // 2:])
        change = round(late - early)
        summary.update(change_s_per_km=change, reading="fitter (faster at the same HR)" if change <= -3 else
                       "slower at the same HR: check fatigue, heat or illness" if change >= 3 else "stable")
    else:
        summary["reading"] = "Not enough weeks with easy runs in the band to read a trend."
    return {"definitions": {"easy_run": f"3 km+, Garmin label {' or '.join(EASY_LABELS)} (or none), "
                            "average HR inside the band; pace uses moving time"},
            "summary": summary, "weeks": table}


# --- readiness calibration ---------------------------------------------------------------

def readiness_calibration(conn, weeks: int = 16, today: date | None = None) -> dict:
    """For each past week: readiness the day before it started vs what happened (planned
    sessions completed or missed, run km change, logged adjustments). If yellow or red weeks
    go no worse than green ones, the thresholds are too strict; if green weeks often fall
    apart, they are too loose. Thresholds are in rules.json "readiness"."""
    rules = history.rules(conn)
    weeks = max(4, min(int(weeks or 16), 52))
    today = today or date.today()
    current = _monday(today)
    first = current - timedelta(days=7 * weeks)
    calendar = _calendar(first.isoformat(), (current - timedelta(days=1)).isoformat())
    loads = load_summary.weekly_load(conn, weeks + 1, today=today)["weeks"]
    km_by_week = {w["week_start"]: w["running"]["distance_km"] for w in loads}
    try:
        adjustments = {r["week_start"] for r in conn.execute(
            "SELECT DISTINCT week_start FROM coach_log WHERE kind = 'adjustment'")}
    except sqlite3.Error:
        adjustments = set()

    rows = []
    for index in range(weeks):
        monday = first + timedelta(days=7 * index)
        start, end = monday.isoformat(), (monday + timedelta(days=6)).isoformat()
        status = _readiness(conn, (monday - timedelta(days=1)).isoformat(), rules)
        plans = [p for p in calendar if start <= str(p["date"])[:10] <= end]
        done = sum(1 for p in plans if p.get("status") in DONE)
        missed = sum(1 for p in plans if p.get("status") in NOT_DONE)
        km, before = km_by_week.get(start), km_by_week.get((monday - timedelta(days=7)).isoformat())
        rows.append({"week_start": start, "readiness": status["status"], "points": status["points"],
                     "planned": len(plans), "completed": done, "missed": missed,
                     "completion_pct": round(done / (done + missed) * 100) if done + missed else None,
                     "run_km": km, "run_km_change_pct": round((km / before - 1) * 100) if km is not None and before else None,
                     "adjusted": start in adjustments})
    by_status = {}
    for status in ("green", "yellow", "red", "unknown"):
        group = [r for r in rows if r["readiness"] == status]
        if not group:
            continue
        rates = [r["completion_pct"] for r in group if r["completion_pct"] is not None]
        by_status[status] = {"weeks": len(group), "avg_completion_pct": round(mean(rates)) if rates else None,
                             "avg_missed": round(mean(r["missed"] for r in group), 1),
                             "weeks_adjusted": sum(r["adjusted"] for r in group)}
    return {"thresholds": rules["readiness"], "by_status": by_status, "weeks": rows,
            "how_to_read": "Compare completion and missed sessions by status. Change thresholds with "
                           "update_athlete_profile section rule_overrides, e.g. "
                           '{"readiness": {"hrv_drop_pct": {"yellow": 12, "red": 22}}}.'}
