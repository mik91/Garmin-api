"""Season plan: week-by-week targets from today to the A race, generated in code.

Like a plan app, the shape of the season does not depend on which model is coaching:
- run volume rebuilds at `rebuild_pct` a week below 80% of the peak target, then `build_pct`,
  with a lighter week every `light_every` weeks once near the peak, and a taper at the end;
- the long run grows by at most `long_step_km` a week up to the distance's cap;
- quality sessions per phase (base: threshold; build: threshold + VO2; peak: goal race pace +
  VO2 or threshold; taper: sharpening), with a dose that progresses and a format that rotates
  (see sessions.py); long runs get marathon-pace or threshold finishes in build and peak;
- a multi-event race (e.g. 2K + 5K + 10K on one day) plans race day as all events and adds
  rehearsal days in the peak phase;
- swims: weekly volume and continuous distance grow, sets follow the phase, CSS test every
  few weeks (see swim.py);
- the flexible gym/bike slot follows the phase (rules structure.flex).

The anchors (start week, and the km / long run of the week before it, peak target,
fitness) are saved once in the profile
section `season_plan`; targets are recomputed from them, and the next week is capped by what
was actually run (see targets_for), so a missed or sick week slows the plan down instead of
being made up.
"""
from __future__ import annotations

import json
import math
import re
from datetime import date, timedelta

from garmin_ai import workouts
from garmin_ai.coaching import history, sessions, swim

SHARPEN = ["4 x 1 km @ goal pace / 2 min jog", "6 x 400 m at I / 2 min jog",
           "3 x 1 km @ goal pace / 2 min jog"]
# How often a type's dose advances by phase: 1 = every normal week it is planned, 2 = every other time.
ADVANCE_EVERY = {"base": {"threshold": 1}, "build": {"threshold": 2, "vo2": 1},
                 "peak": {"threshold": 2, "vo2": 2, "race_pace": 1}}
EVENT_KM = re.compile(r"(\d+(?:\.\d+)?)\s*k", re.IGNORECASE)

DEFAULTS = {
    "rebuild_pct": 10, "build_pct": 5, "light_every": 4, "light_cut_pct": 20,
    # Share of the peak run km each phase may reach: base tops out below peak, build gets there.
    "phase_peak_share": {"base": 0.9, "build": 1.0, "peak": 1.0},
    "long_share": 0.28, "long_step_km": 1.5, "long_light_cut_km": 2,
    "css_test_every": 7,
    "distances": {
        "5K": {"long_run_cap_km": 16, "phases": {"taper_weeks": 1}},
        "10K": {"long_run_cap_km": 18, "phases": {"taper_weeks": 1}},
        "half": {"long_run_cap_km": 22, "phases": {"taper_weeks": 2}},
        "marathon": {"long_run_cap_km": 32, "phases": {"taper_weeks": 3}},
    },
    "taper_km": {"race_week": 0.55, "taper_1": 0.75, "taper_2": 0.85, "taper_3": 0.9},
    # Fitness checkpoints: a time trial about this many weeks before the race, moved to the
    # nearest lighter week within 2 weeks (fresher legs, and the trial replaces that week's quality).
    "checkpoints": [{"weeks_to_race": 20, "distance": "5K"}, {"weeks_to_race": 11, "distance": "10K"},
                    {"weeks_to_race": 6, "distance": "5K"}],
}
DISTANCE_M = {"3K": 3000, "5K": 5000, "10K": 10000, "half": 21097.5, "marathon": 42195}


def settings(rules: dict) -> dict:
    return history._merge(DEFAULTS, rules.get("season") or {})


def distance_key(race: dict) -> str:
    text = str(race.get("distance") or race.get("name") or "").lower().replace(" ", "")
    for key, words in (("marathon", ("marathon", "42")), ("half", ("half", "21", "semi", "demi")),
                       ("10K", ("10k", "10km")), ("5K", ("5k", "5km"))):
        if any(w in text for w in words) and not (key == "marathon" and ("half" in text or "demi" in text)):
            return key
    return "10K"


def phases_for(race: dict, rules: dict) -> dict:
    cfg = settings(rules)
    return {**rules.get("phases", {}), **cfg["distances"].get(distance_key(race), {}).get("phases", {})}


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def phase_of(weeks_to_race: int, phases: dict) -> tuple[str, int]:
    """(phase, taper week number) for a week `weeks_to_race` before the race week."""
    taper, peak, build = phases.get("taper_weeks", 2), phases.get("peak_weeks", 4), phases.get("build_weeks", 10)
    if weeks_to_race <= 0:
        return "race_week", 0
    if weeks_to_race <= taper:
        return "taper", weeks_to_race
    if weeks_to_race <= taper + peak:
        return "peak", 0
    if weeks_to_race <= taper + peak + build:
        return "build", 0
    return "base", 0


def _flex(rules: dict, phase: str) -> str | None:
    flex = (rules.get("structure") or {}).get("flex") or {}
    prefer = flex.get("prefer") or {}
    if not flex:
        return None
    return (prefer.get(phase) or prefer.get("default")) if isinstance(prefer, dict) else prefer


TIME = re.compile(r"\b(?:(\d{1,2}):)?(\d{1,2}):(\d{2})\b")


def goal_pace(race: dict) -> dict | None:
    """The goal time in the race's goal text ("sub 38:30", "under 1:25:00") as pace and VDOT."""
    meters = {"5K": 5000, "10K": 10000, "half": 21097.5, "marathon": 42195}[distance_key(race)]
    match = TIME.search(str(race.get("goal") or ""))
    if not match:
        return None
    hours, minutes, secs = (int(x) if x else 0 for x in match.groups())
    seconds = hours * 3600 + minutes * 60 + secs
    if seconds < meters / 8:  # faster than 8 m/s: not a race time
        return None
    return {"time": match.group(0), "seconds": seconds,
            "pace_min_per_km": workouts.format_pace(seconds / meters * 1000),
            "vdot": round(workouts.vdot(meters, seconds), 1)}


def events(race: dict) -> list[dict]:
    """A multi-event race's events, e.g. "events": ["2K", "5K", "10K"]; the last is the goal."""
    found = []
    for item in race.get("events") or []:
        match = EVENT_KM.search(str(item))
        if match:
            found.append({"event": str(item), "km": float(match.group(1))})
    return found


def generate(anchor: dict, race: dict, rules: dict, css: float | None = None,
             with_steps: bool = False) -> list[dict]:
    """All weeks from anchor["start_week"] to the race week. Pure: the same inputs give the same
    plan. css (s/100 m, latest test) only turns swim set targets into paces. with_steps keeps
    each quality session's structured "steps" (for materialize.py)."""
    cfg = settings(rules)
    phases = phases_for(race, rules)
    race_day = date.fromisoformat(race["date"])
    race_monday = _monday(race_day)
    start = date.fromisoformat(anchor["start_week"])
    peak = float(anchor["peak_run_km"])
    cap = cfg["distances"].get(distance_key(race), {}).get("long_run_cap_km", 18)
    km, long_km = float(anchor["start_run_km"]), float(anchor["start_long_run_km"])
    swim_state = {"weekly_m": float(anchor.get("start_weekly_swim_m") or swim.DEFAULTS["start_weekly_m"]),
                  "continuous_m": float(anchor.get("start_continuous_swim_m") or swim.DEFAULTS["continuous_start_m"]),
                  "css_step": 0}
    vdot = float(anchor.get("start_vdot") or 45)
    kinds = ("threshold", "vo2", "race_pace")
    steps, seen, formats = dict.fromkeys(kinds, 0), dict.fromkeys(kinds, 0), dict.fromkeys(kinds, 0)
    speed_count = long_count = sharpen_count = 0
    # Holds from session reviews (execution.py): the next session of that type on or after
    # from_week repeats its step instead of advancing.
    pending = {kind: sorted((h for h in anchor.get("holds") or [] if h.get("type") == kind),
                            key=lambda h: h["from_week"]) for kind in kinds}
    multi = events(race)
    race_km = sum(e["km"] for e in multi) if multi else None
    rehearsals, last_rehearsal = 0, -2  # multi-event races: two rehearsal days in peak
    since_light, normal_km, normal_long = 0, km, long_km
    weeks, monday, index = [], start, 0
    struct = rules.get("structure") or {}
    while monday <= race_monday:
        to_race = (race_monday - monday).days // 7
        phase, taper_n = phase_of(to_race, phases)
        light = False
        if phase in ("taper", "race_week"):
            factor = cfg["taper_km"]["race_week" if phase == "race_week" else f"taper_{taper_n}"]
            km = round(normal_km * factor, 1)
            long_km = round(min(normal_long, cap) * (0.0 if phase == "race_week" else factor), 1)
        else:
            near_peak = normal_km >= 0.8 * peak
            # Back from a lighter week over two weeks: halfway, then the volume from before it.
            # Growth resumes the week after (the validator caps a week at the recent peak or
            # +10%, whichever is higher, so a week straight back above the old peak would fail).
            returning = index > 0 and weeks[-1]["light"]
            holding = index > 1 and weeks[-2]["light"] and not weeks[-1]["light"]
            # The anchor is the week before start_week, so week 1 already progresses.
            if index > 0 and near_peak and since_light + 1 >= cfg["light_every"] and not returning:
                light = True
            elif not (returning or holding):
                pct = cfg["rebuild_pct"] if not near_peak else cfg["build_pct"]
                phase_cap = peak * cfg["phase_peak_share"].get(phase, 1.0)
                normal_km = max(min(normal_km, peak), min(phase_cap, math.floor(normal_km * (1 + pct / 100) * 10 + 1e-6) / 10))
                normal_long = min(cap, normal_long + cfg["long_step_km"],
                                  round(normal_km * cfg["long_share"], 1))
            km = round(normal_km * (1 - cfg["light_cut_pct"] / 100), 1) if light else normal_km
            long_km = (round(max(0, min(normal_long - cfg["long_light_cut_km"], km * cfg["long_share"])), 1)
                       if light else normal_long)
            if returning:
                km = math.floor((weeks[-1]["run_km"] + normal_km) / 2 * 10 + 1e-6) / 10
                long_km = min(normal_long, math.floor(km * cfg["long_share"] * 10) / 10)
            since_light = 0 if light else since_light + 1

        def take(kind):
            session = sessions.build(kind, steps[kind], formats[kind], km, vdot, phase, light)
            formats[kind] += 1
            if not light:
                if pending[kind] and pending[kind][0]["from_week"] <= monday.isoformat():
                    # The step already advanced when the held session was planned: use the
                    # previous step again and do not advance.
                    hold = pending[kind].pop(0)
                    again = sessions.build(kind, max(0, steps[kind] - 1), formats[kind] - 1, km, vdot, phase)
                    return {**again, "repeat": f"Same step as {hold.get('date')}: {hold.get('reason')}"}
                seen[kind] += 1
                if seen[kind] % ADVANCE_EVERY.get(phase, {}).get(kind, 1) == 0:
                    steps[kind] += 1
            return session

        def sharpen():
            nonlocal sharpen_count
            sharpen_count += 1
            return {"type": "sharpen", "format": "sharpen", "session": SHARPEN[(sharpen_count - 1) % len(SHARPEN)]}

        quality = []
        if phase == "base":
            quality = [take("threshold")]
        elif phase == "build":
            quality = [take("threshold")] if light else [take("threshold"), take("vo2")]
        elif phase == "peak":
            rehearsing = bool(multi) and not light and rehearsals < 2 and index - last_rehearsal > 1
            if light or rehearsing:  # a rehearsal day is the week's only quality
                quality = [take("race_pace")]
            else:
                quality = [take("race_pace"), take("vo2") if index % 2 == 0 else take("threshold")]
        elif phase == "taper":
            quality = [sharpen()]
        elif phase == "race_week":
            quality = [{"type": "sharpen", "format": "sharpen",
                        "session": "3 x 400 m @ goal pace / 2 min jog, 2-3 days before"}]
            if multi:
                lead = multi[:-1]
                quality += [{"type": "race", "format": "event",
                             "session": f"{e['event']} controlled: easy to steady (marathon pace or slower)"}
                            for e in lead]
                quality.append({"type": "race", "format": "event",
                                "session": f"{multi[-1]['event']} race: {race.get('goal') or 'goal effort'}"})
            else:
                quality.append({"type": "race", "format": "event", "session": f"{race['name']} on {race['date']}"})
        runs = struct.get("runs", 5)
        rehearsal = None
        if (phase == "peak" and quality and quality[0].get("type") == "race_pace" and len(quality) == 1
                and not light and bool(multi) and rehearsals < 2 and index - last_rehearsal > 1):
            rehearsals, last_rehearsal = rehearsals + 1, index
            lead_km = sum(e["km"] for e in multi[:-1])
            rehearsal = (f"Race-day rehearsal: AM {lead_km:g} km steady (marathon pace or slower), "
                         f"PM {quality[0]['session']}")
            quality[0] = {**quality[0], "rehearsal": True, "session": rehearsal}
            runs += 1
        extras = []
        if phase in ("base", "build") and not light:
            extras.append(sessions.speed(speed_count) + " after an easy run")
            speed_count += 1
        if phase not in ("race_week",):
            extras.append("strides 4-6 x 20 s after 1-2 easy runs")
        long_format = sessions.long_run_format(phase, long_count, light or rehearsal is not None)
        if not light and phase not in ("taper", "race_week"):
            long_count += 1

        if index > 0:  # week 1 swims the start values
            swim.advance(swim_state, weeks[-1]["phase"], weeks[-1]["light"])
        css_test = index > 0 and index % cfg["css_test_every"] == 0 and phase not in ("taper", "race_week")
        swim_week = swim.week(phase, swim_state, light, index, css, css_test)
        weeks.append({
            "week_start": monday.isoformat(), "phase": phase, "weeks_to_race": to_race, "light": light,
            "run_km": km, "long_run_km": long_km, "long_run": long_format, "runs": runs,
            "quality": quality, "extras": extras, "swim": swim_week,
            **({"race_day_km": race_km, "note": f"Race day is {race_km:g} km of racing plus warm-ups; "
                "keep the other days short and easy."} if phase == "race_week" and race_km else {}),
            "gym_min": struct.get("gym_min", 0), "flex": _flex(rules, phase),
        })
        monday += timedelta(days=7)
        index += 1
    add_checkpoints(weeks, anchor, race, cfg)
    if not with_steps:
        for week in weeks:
            for session in week["quality"]:
                session.pop("steps", None)
    return weeks


def race_time(distance_m: float, vdot: float) -> float:
    """Seconds to run distance_m at a VDOT (inverse of workouts.vdot, by bisection)."""
    low, high = distance_m / 8, distance_m / 1.5  # 8 m/s .. 1.5 m/s
    for _ in range(60):
        middle = (low + high) / 2
        if workouts.vdot(distance_m, middle) > vdot:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def expected_vdot(anchor: dict, race: dict, weeks_total: int, index: int) -> float | None:
    """Straight line from the starting VDOT (the week before the plan) to the goal VDOT in race week."""
    goal, start = goal_pace(race), anchor.get("start_vdot")
    if not goal or not start or weeks_total <= 0:
        return None
    return round(start + (goal["vdot"] - start) * min(index + 1, weeks_total) / weeks_total, 1)


def add_checkpoints(weeks: list[dict], anchor: dict, race: dict, cfg: dict) -> None:
    taken = set()
    for check in cfg.get("checkpoints") or []:
        wanted = check["weeks_to_race"]
        candidates = [i for i, w in enumerate(weeks) if w["phase"] in ("base", "build", "peak")
                      and i not in taken and not any(q.get("rehearsal") for q in w["quality"])]
        if not candidates:
            continue
        light = [i for i in candidates if weeks[i]["light"] and abs(weeks[i]["weeks_to_race"] - wanted) <= 2]
        pool = light or candidates
        index = min(pool, key=lambda i: (abs(weeks[i]["weeks_to_race"] - wanted), -weeks[i]["weeks_to_race"]))
        taken.add(index)
        meters = DISTANCE_M.get(check["distance"], 5000)
        vdot = expected_vdot(anchor, race, len(weeks), index)
        expected = {"vdot": vdot, "time": workouts.format_duration(race_time(meters, vdot))} if vdot else None
        where = "parkrun, race or track" if meters <= 5000 else "a race or a flat measured route"
        text = (f"{check['distance']} time trial ({where}): 15 min easy + 4 strides, "
                f"{check['distance']} all-out at even pace, 10 min easy"
                + (f". On track if about {expected['time']} (VDOT {expected['vdot']})." if expected else "."))
        weeks[index]["quality"] = [{"type": "time_trial", "format": "checkpoint", "session": text, "all_out": True}]
        weeks[index]["checkpoint"] = {"test": check["distance"], "distance_m": meters, "expected": expected}
        weeks[index]["long_run"] = "easy"


# --- anchors in the profile ---------------------------------------------------------------

def a_race(conn) -> dict | None:
    from garmin_ai.coaching.review import parse_races
    row = conn.execute("SELECT content FROM coach_profile WHERE section = 'races'").fetchone()
    races = parse_races(row["content"] if row else None)
    today = date.today().isoformat()
    return next((r for r in races if r["priority"] == "A" and r["date"] >= today), None)


def anchor(conn) -> dict | None:
    row = conn.execute("SELECT content FROM coach_profile WHERE section = 'season_plan'").fetchone()
    return json.loads(row["content"]) if row else None


def propose_anchor(conn, today: date | None = None, peak_run_km: float | None = None,
                   start_continuous_swim_m: float | None = None) -> dict:
    """Starting point for a new season from the synced history: next Monday, the current
    week's run km (done + still planned), the longest run in 30 days, the 12-week peak."""
    from garmin_ai import coach
    today = today or date.today()
    rules = history.rules(conn)
    start = _monday(today) + timedelta(days=7)
    used = history.plan_history(conn, start.isoformat(), rules["light_week"]["reduction_pct_min"],
                                rules["ramp"]["peak_lookback_weeks"])
    swim_pace = swim.recent_pace(conn, today.isoformat()) or \
        rules.get("swim", {}).get("default_pace_s_per_100m", workouts.SWIM_S_PER_100M)
    easy = history.recent_easy_pace(conn, today.isoformat())
    used = coach._with_rest_of_week(conn, used, start.isoformat(), today, coach.current_paces(), swim_pace, easy,
                                    rules["light_week"]["reduction_pct_min"])
    weekly = used["weekly_run_km"]
    recent_peak = max(weekly) if weekly else 0
    paces = coach.current_paces()
    return {
        "start_week": start.isoformat(), "created": today.isoformat(),
        "start_run_km": weekly[-1] if weekly else 20.0,
        "start_long_run_km": round(used.get("longest_run_km_30d") or 8.0, 1),
        "peak_run_km": float(peak_run_km or round(max(recent_peak, 40) * 1.05)),
        "start_weekly_swim_m": swim.weekly_volume(conn, start.isoformat()) or swim.DEFAULTS["start_weekly_m"],
        "start_continuous_swim_m": start_continuous_swim_m or swim.DEFAULTS["continuous_start_m"],
        "start_vdot": paces.get("vdot") if paces else None,
        "basis": {"weekly_run_km": weekly, "current_week": used.get("current_week"),
                  "paces": paces.get("based_on") if paces else None},
    }


def plan(conn, weeks: int | None = None, start: str | None = None) -> dict:
    rules = history.rules(conn)
    race, saved = a_race(conn), anchor(conn)
    if not race:
        return {"error": "No upcoming A race in the profile races section."}
    if not saved:
        return {"error": "No season plan yet; create one with start_season_plan."}
    all_weeks = generate(saved, race, rules, swim.latest_css(conn))
    if start:
        all_weeks = [w for w in all_weeks if w["week_start"] >= _monday(date.fromisoformat(start)).isoformat()]
    return {"race": race, "goal": goal_pace(race), "anchor": saved,
            "weeks": all_weeks[:weeks] if weeks else all_weeks}


def targets_for(conn, monday: str, last_week_km: float | None = None) -> dict | None:
    """The season target for one week, capped by what was actually run: last_week_km grown at
    the rebuild rate, or at the plan's own growth from last week's target when that is larger
    (coming back from a lighter week), so missed weeks slow the plan down but a lighter week
    run as planned does not."""
    try:
        race, saved = a_race(conn), anchor(conn)
    except Exception:
        return None
    if not race or not saved:
        return None
    rules = history.rules(conn)
    weeks = generate(saved, race, rules, swim.latest_css(conn))
    index = next((i for i, w in enumerate(weeks) if w["week_start"] == monday), None)
    if index is None:
        return None
    week = dict(weeks[index])
    if last_week_km and week["phase"] not in ("taper", "race_week") and not week["light"]:
        growth = 1 + settings(rules)["rebuild_pct"] / 100
        if index > 0 and weeks[index - 1]["run_km"]:
            growth = max(growth, week["run_km"] / weeks[index - 1]["run_km"])
        cap = math.floor(last_week_km * growth * 10 + 1e-6) / 10
        if cap < week["run_km"]:
            week.update(planned_run_km=week["run_km"], run_km=cap,
                        note=f"Capped at {cap} km: last week was {last_week_km} km.")
    return week


def compare(target: dict, summary: dict, sessions: list[dict]) -> list[dict]:
    """Warnings where a week's plan differs from its season target."""
    out = []
    km = summary.get("planned_run_km") or 0
    if target["run_km"] and km > target["run_km"] * 1.1:
        out.append(f"{km:.1f} km is above the season target of {target['run_km']} km for this week.")
    elif target["run_km"] and km < target["run_km"] * 0.8 and target["phase"] not in ("taper", "race_week"):
        out.append(f"{km:.1f} km is well below the season target of {target['run_km']} km.")
    longs = [s.get("km") or 0 for s in sessions if s.get("sport") == "run" and s.get("kind") == "long"]
    if longs and target["long_run_km"] and max(longs) > target["long_run_km"] + 1.5:
        out.append(f"Long run {max(longs):.1f} km is over the target {target['long_run_km']} km.")
    want = sum(1 for q in target["quality"] if q["type"] != "race")
    have = summary.get("quality_sessions") or 0
    if have != want and target["phase"] != "race_week":
        out.append(f"{have} quality sessions planned; the season plan has {want} "
                   f"({', '.join(q['session'] for q in target['quality'])}).")
    return [{"rule": "season", "message": m} for m in out]
