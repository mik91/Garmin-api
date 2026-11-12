"""Season progress: is the plan working? One call for the weekly or block review.

Compares the season plan with what happened:
- volume: run km of the last 4 complete weeks vs their targets;
- completion: [CP] sessions completed vs missed in those weeks;
- fitness: current VDOT vs the straight line from the starting VDOT to the goal VDOT, and the
  checkpoint time trials (expected vs actual, read from the synced runs);
- trends: easy pace at the same HR, swim pace and SWOLF, CSS test age, readiness.
Each finding becomes a flag with a level (ok, watch, act) and an overall verdict.
"""
from __future__ import annotations

from datetime import date, timedelta

from garmin_ai import workouts
from garmin_ai.coaching import efficiency, history, review, season, swim
from garmin_ai.coaching.readiness import assess_readiness


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def checkpoint_result(conn, week: dict) -> dict | None:
    """The fastest run that week within 3% short / 10% long of the test distance."""
    check = week["checkpoint"]
    first = week["week_start"]
    last = (date.fromisoformat(first) + timedelta(days=6)).isoformat()
    meters = check["distance_m"]
    rows = conn.execute(
        "SELECT substr(start_time, 1, 10) AS day, distance_m, duration_s FROM activities "
        "WHERE type LIKE '%running%' AND distance_m BETWEEN ? AND ? AND duration_s > 0 "
        "AND substr(start_time, 1, 10) BETWEEN ? AND ?", (meters * 0.97, meters * 1.10, first, last)).fetchall()
    if not rows:
        return None
    best = min(rows, key=lambda r: r["duration_s"] / r["distance_m"])
    vdot = round(workouts.vdot(best["distance_m"], best["duration_s"]), 1)
    result = {"date": best["day"], "distance_m": round(best["distance_m"]),
              "time": workouts.format_duration(best["duration_s"]), "vdot": vdot}
    expected = (check.get("expected") or {}).get("vdot")
    if expected:
        result["vs_expected_vdot"] = round(vdot - expected, 1)
    return result


def progress(conn, today: date | None = None) -> dict:
    from garmin_ai import coach
    today = today or date.today()
    rules = history.rules(conn)
    race, anchor = season.a_race(conn), season.anchor(conn)
    if not race or not anchor:
        return {"error": "Needs an upcoming A race and a saved season plan (start_season_plan)."}
    weeks = season.generate(anchor, race, rules, swim.latest_css(conn))
    current = _monday(today).isoformat()
    index = next((i for i, w in enumerate(weeks) if w["week_start"] == current), None)
    done = [w for w in weeks if w["week_start"] < current]
    flags = []

    def flag(level, area, message):
        flags.append({"level": level, "area": area, "message": message})

    # --- volume and completion over the last 4 complete weeks -------------------------------
    recent = done[-4:]
    volume = []
    for w in recent:
        actual = coach.recent_weekly_run_km(conn, date.fromisoformat(w["week_start"]) + timedelta(days=7), 1)[0]
        volume.append({"week_start": w["week_start"], "target_km": w["run_km"], "actual_km": actual,
                       "pct": round(actual / w["run_km"] * 100) if w["run_km"] else None})
    if volume:
        ratio = sum(v["actual_km"] for v in volume) / max(1, sum(v["target_km"] for v in volume))
        if ratio < 0.8:
            flag("act", "volume", f"Running is {ratio:.0%} of target over {len(volume)} weeks: the plan is ahead of "
                                  "you. Hold volume, or lower the peak target with start_season_plan.")
        elif ratio < 0.9:
            flag("watch", "volume", f"Running is {ratio:.0%} of target over {len(volume)} weeks.")
        elif ratio > 1.15:
            flag("watch", "volume", f"Running is {ratio:.0%} of target: more than planned, watch for niggles.")
        else:
            flag("ok", "volume", f"Running is {ratio:.0%} of target over {len(volume)} weeks.")

    completion = None
    if recent:
        first, last = recent[0]["week_start"], (date.fromisoformat(recent[-1]["week_start"]) + timedelta(days=6)).isoformat()
        coach_ids = review._coach_ids(conn)
        plans = [p for p in review._calendar(first, last) if str(p.get("scheduled_workout_id")) in coach_ids]
        completed = sum(1 for p in plans if p.get("status") == "completed")
        missed = sum(1 for p in plans if p.get("status") in ("missed", "skipped"))
        if completed + missed:
            completion = {"completed": completed, "missed": missed, "pct": round(completed / (completed + missed) * 100)}
            level = "ok" if completion["pct"] >= 85 else "watch" if completion["pct"] >= 70 else "act"
            flag(level, "completion", f"{completion['pct']}% of planned sessions done ({missed} missed) in {len(recent)} weeks.")

    # --- fitness: trajectory and checkpoints -----------------------------------------------
    goal = season.goal_pace(race)
    paces = coach.current_paces()
    fitness = {"start_vdot": anchor.get("start_vdot"), "goal_vdot": goal and goal["vdot"],
               "current_vdot": paces and paces.get("vdot"), "based_on": paces and paces.get("based_on")}
    position = index if index is not None else len(done) - 1
    if position >= 0:
        fitness["expected_vdot_now"] = season.expected_vdot(anchor, race, len(weeks), position)
    checkpoints = []
    for w in weeks:
        if not w.get("checkpoint"):
            continue
        entry = {"week_start": w["week_start"], **w["checkpoint"]}
        if w["week_start"] < current or w["week_start"] == current:
            result = checkpoint_result(conn, w)
            entry["result"] = result
            entry["status"] = "done" if result else ("missed" if w["week_start"] < current else "this week")
        else:
            entry["status"] = "upcoming"
        checkpoints.append(entry)
    latest = next((c for c in reversed(checkpoints) if c.get("result")), None)
    if latest and "vs_expected_vdot" in latest["result"]:
        gap = latest["result"]["vs_expected_vdot"]
        if gap <= -1.0:
            flag("act", "fitness", f"{latest['test']} checkpoint was {gap:+.1f} VDOT vs the line to the goal: "
                                   "decide whether to adjust the goal or the plan.")
        elif gap < -0.4:
            flag("watch", "fitness", f"{latest['test']} checkpoint was {gap:+.1f} VDOT vs the line to the goal.")
        else:
            flag("ok", "fitness", f"{latest['test']} checkpoint was {gap:+.1f} VDOT vs the line: on track.")
    missed_checks = [c for c in checkpoints if c["status"] == "missed"]
    if missed_checks:
        flag("watch", "fitness", f"No result found for the {missed_checks[-1]['test']} checkpoint of "
                                 f"{missed_checks[-1]['week_start']} (no run of that distance that week).")
    if fitness.get("current_vdot") and fitness.get("expected_vdot_now"):
        gap = round(fitness["current_vdot"] - fitness["expected_vdot_now"], 1)
        fitness["current_vs_expected"] = gap
        if not latest:  # predictions are a weaker signal than a time trial
            flag("ok" if gap > -1 else "watch", "fitness",
                 f"Garmin-based VDOT {fitness['current_vdot']} vs {fitness['expected_vdot_now']} expected now ({gap:+.1f}).")

    # --- trends ----------------------------------------------------------------------------
    # Easy-run efficiency, corrected for speed, hills, warm-up and heat. Only a change bigger
    # than its margin of error becomes a flag, so normal week-to-week noise stays quiet.
    corrected = efficiency.trend(conn, 24, today=today)
    easy = corrected.get("summary") or {}
    if easy.get("significant"):
        latest = corrected["windows"][-1]
        flag("watch" if easy["change_s_per_km"] > 0 else "ok", "easy_pace",
             f"Easy pace at {corrected['ref_hr']} bpm (heat and hills corrected): {easy['reading']}, "
             f"{easy['change_s_per_km']:+d} s/km vs 6 weeks ago (now {latest['pace_at_ref_hr']}/km, "
             f"margin {easy['margin_s']} s).")
    swim_summary = swim.trend(conn, 8, today=today)["summary"]
    if swim_summary.get("reading"):
        flag("watch" if "less" in swim_summary["reading"] else "ok", "swim", f"Swimming: {swim_summary['reading']}.")
    tests = swim.css_history(conn)
    css_age = (today - date.fromisoformat(tests[-1]["date"])).days if tests else None
    if css_age is None:
        flag("watch", "swim", "No CSS test recorded: swim sets use effort words.")
    elif css_age > 63:
        flag("watch", "swim", f"Last CSS test was {css_age} days ago: retest (every 6-8 weeks).")

    ready = assess_readiness(history.daily_rows(conn, today.isoformat()), today.isoformat(), [], rules)
    if ready["status"] in ("yellow", "red"):
        flag("watch" if ready["status"] == "yellow" else "act", "readiness",
             f"Readiness is {ready['status']}: " + "; ".join(ready["actions"]))

    levels = [f["level"] for f in flags]
    verdict = "act" if "act" in levels else "watch" if "watch" in levels else "on track"
    this_week = weeks[index] if index is not None else (weeks[0] if weeks and weeks[0]["week_start"] > current else None)
    return {
        "as_of": today.isoformat(), "verdict": verdict, "flags": flags,
        "season": {"race": race["name"], "race_date": race["date"], "goal": goal,
                   "weeks_done": len(done), "weeks_total": len(weeks),
                   "phase": this_week and this_week["phase"], "this_week_target": this_week and {
                       k: this_week[k] for k in ("week_start", "run_km", "long_run_km", "quality") if k in this_week}},
        "volume": volume, "completion": completion, "fitness": fitness, "checkpoints": checkpoints,
        "easy_pace": {"available": corrected.get("available"), **easy,
                      **({"ref_hr": corrected["ref_hr"], "now": corrected["windows"][-1]["pace_at_ref_hr"]}
                         if corrected.get("windows") else {})},
        "swim": {**swim_summary, "css_tests": len(tests), "days_since_css": css_age},
        "readiness": {"status": ready["status"], "points": ready["points"]},
        "how_to_use": "Run weekly (or each lighter week). 'act' flags need a decision with the athlete; "
                      "log it with log_coaching_decision.",
    }
