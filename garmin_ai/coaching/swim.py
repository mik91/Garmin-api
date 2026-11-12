"""Swimming: own pace, CSS and zones, trend, and the weekly swim plan.

For a developing swimmer the measures that matter are not the length of one long session but:
- continuous distance: how far the athlete swims nonstop at an easy pace (400 m -> 1500 m+);
- weekly volume: grows gradually (default +6% a week, lighter weeks -20%);
- efficiency: pace per 100 m and SWOLF (strokes + seconds per length, lower is better);
- CSS (critical swim speed) from a 400 m + 200 m test: CSS = (T400 - T200) / 2 per 100 m.
  It is the swim threshold pace and sets the zones for the main sets.

Phases follow the A race season (base: technique + continuous; build: CSS sets and pull;
peak: CSS and race-pace sets with sighting; taper: short and sharp). Open-water work belongs
to the triathlon block after the A race.
"""
from __future__ import annotations

import json
from datetime import date, timedelta
from statistics import mean

from garmin_ai import workouts
from garmin_ai.coaching import history

DEFAULTS = {
    "start_weekly_m": 3000, "weekly_step_pct": 6, "weekly_cap_m": 5500, "light_cut_pct": 20,
    "continuous_start_m": 400, "continuous_step_m": 100, "continuous_cap_m": 2000,
    "css_test_every": 7,
}
# Main sets at CSS, from short reps to longer ones; the dose rises slowly through build and peak.
CSS_SETS = [
    (6, 100, "+5 s", "20 s"), (8, 100, "+3 s", "20 s"), (10, 100, "+3 s", "15 s"), (4, 200, "+3 s", "30 s"),
    (5, 200, "+2 s", "30 s"), (3, 300, "+2 s", "30 s"), (3, 400, "+2 s", "45 s"), (4, 400, "+3 s", "45 s"),
]
DRILLS = ["catch-up", "side kick 6-3-6", "fingertip drag", "single arm", "kick on side with fins", "sculling"]
ZONES = {"recovery": (15, 20), "endurance": (8, 12), "steady": (4, 6), "css": (0, 3), "fast": (-5, -2)}


def fmt(seconds_per_100: float) -> str:
    return workouts.format_pace(seconds_per_100)


# --- own pace and trend ------------------------------------------------------------------

def _swims(conn, first: str, last: str):
    return conn.execute(
        "SELECT substr(start_time, 1, 10) AS day, type, distance_m, "
        "COALESCE(moving_duration_s, duration_s) AS seconds, raw_json FROM activities "
        "WHERE type LIKE '%swim%' AND distance_m > 0 AND substr(start_time, 1, 10) BETWEEN ? AND ?",
        (first, last)).fetchall()


def recent_pace(conn, before: str, days: int = 42, min_swims: int = 3) -> float | None:
    """Own pool pace in s/100 m (moving time over distance, drills included) in the `days`
    before `before`; used to estimate how long planned swims take."""
    end = date.fromisoformat(before) - timedelta(days=1)
    rows = [r for r in _swims(conn, (end - timedelta(days=days - 1)).isoformat(), end.isoformat())
            if "open_water" not in str(r["type"]) and r["seconds"]]
    if len(rows) < min_swims:
        return None
    return round(sum(r["seconds"] for r in rows) / sum(r["distance_m"] for r in rows) * 100)


def weekly_volume(conn, before: str, weeks: int = 4) -> float | None:
    end = date.fromisoformat(before) - timedelta(days=1)
    first = end - timedelta(days=7 * weeks - 1)
    rows = _swims(conn, first.isoformat(), end.isoformat())
    return round(sum(r["distance_m"] for r in rows) / weeks) if rows else None


def trend(conn, weeks: int = 12, today: date | None = None) -> dict:
    """Per Monday week: swims, distance, pace per 100 m (moving), SWOLF and longest swim."""
    weeks = max(2, min(int(weeks or 12), 52))
    today = today or date.today()
    first = today - timedelta(days=today.weekday()) - timedelta(days=7 * (weeks - 1))
    rows = _swims(conn, first.isoformat(), today.isoformat())
    table = []
    for index in range(weeks):
        monday = first + timedelta(days=7 * index)
        inside = [r for r in rows if monday.isoformat() <= r["day"] <= (monday + timedelta(days=6)).isoformat()]
        meters = sum(r["distance_m"] for r in inside)
        entry = {"week_start": monday.isoformat(), "swims": len(inside), "distance_m": round(meters)}
        timed = [r for r in inside if r["seconds"]]
        if timed:
            pace = sum(r["seconds"] for r in timed) / sum(r["distance_m"] for r in timed) * 100
            entry.update(pace_per_100m=fmt(pace), seconds_per_100m=round(pace),
                         longest_swim_m=round(max(r["distance_m"] for r in inside)))
        swolf = []
        for r in inside:
            try:
                value = json.loads(r["raw_json"] or "{}").get("averageSwolf")
            except ValueError:
                value = None
            if value and "open_water" not in str(r["type"]):
                swolf.append((value, r["distance_m"]))
        if swolf:
            entry["swolf"] = round(sum(v * m for v, m in swolf) / sum(m for _, m in swolf), 1)
        table.append(entry)
    data = [w for w in table if w["swims"]]
    summary = {"weeks_with_swims": len(data), "avg_weekly_m": round(mean(w["distance_m"] for w in table))}
    if len(data) >= 4:
        half = len(data) // 2
        for key, label in (("seconds_per_100m", "pace_change_s_per_100m"), ("swolf", "swolf_change")):
            early = [w[key] for w in data[:half] if key in w]
            late = [w[key] for w in data[half:] if key in w]
            if early and late:
                summary[label] = round(mean(late) - mean(early), 1)
        change = summary.get("swolf_change")
        summary["reading"] = ("more efficient (lower SWOLF)" if change is not None and change <= -2 else
                              "less efficient: check fatigue or technique" if change is not None and change >= 2
                              else "stable")
    return {"definitions": {"pace": "moving time over distance, drills and kick included",
                            "swolf": "strokes + seconds per length, distance-weighted; lower is better"},
            "summary": summary, "weeks": table}


# --- CSS ------------------------------------------------------------------------------

def _seconds(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parts = [int(p) for p in str(value).strip().split(":")]
    return float(sum(p * 60 ** i for i, p in enumerate(reversed(parts))))


def css_from_test(t400, t200) -> float:
    t400, t200 = _seconds(t400), _seconds(t200)
    if not 0 < t200 < t400 or t400 < 2 * t200:
        raise history.HistoryError("Times must be the 400 m and the 200 m (the 400 takes more than twice the 200).")
    return round((t400 - t200) / 2, 1)


def zones(css: float) -> dict:
    return {name: {"per_100m": [fmt(css + low), fmt(css + high)] if low != high else fmt(css + low),
                   "seconds": [round(css + low), round(css + high)]}
            for name, (low, high) in ZONES.items()}


def css_history(conn) -> list[dict]:
    row = conn.execute("SELECT content FROM coach_profile WHERE section = 'swim_css'").fetchone()
    return json.loads(row["content"]) if row else []


def latest_css(conn) -> float | None:
    tests = css_history(conn)
    return tests[-1]["css_s_per_100m"] if tests else None


def record_css(conn, t400, t200, day: str | None = None, note: str | None = None) -> dict:
    css = css_from_test(t400, t200)
    day = day or date.today().isoformat()
    tests = [t for t in css_history(conn) if t["date"] != day]
    entry = {"date": day, "t400": workouts.format_duration(_seconds(t400)),
             "t200": workouts.format_duration(_seconds(t200)), "css_s_per_100m": css, "css": fmt(css)}
    if note:
        entry["note"] = note
    tests = sorted(tests + [entry], key=lambda t: t["date"])
    previous = [t for t in tests if t["date"] < day]
    history.set_profile(conn, "swim_css", json.dumps(tests),
                        f"CSS test {day}: {fmt(css)}/100 m" + (f" (was {previous[-1]['css']})" if previous else ""))
    return {**entry, "previous": previous[-1] if previous else None, "zones": zones(css)}


def paces(conn) -> dict:
    tests = css_history(conn)
    if not tests:
        return {"css": None, "note": "No CSS test yet: swim sets use effort words until one is recorded "
                                     "(record_css_test with the 400 m and 200 m times)."}
    css = tests[-1]["css_s_per_100m"]
    return {"css": fmt(css), "tested": tests[-1]["date"], "zones": zones(css), "tests": tests}


# --- weekly swim plan -----------------------------------------------------------------

def _at(css: float | None, offset: str) -> str:
    if css is None:
        return f"CSS{offset}" if offset else "CSS"
    seconds = css + float(offset.replace(" s", "") or 0)
    return f"CSS{offset} (~{fmt(seconds)}/100 m)"


def _round50(value: float) -> int:
    return int(max(0, round(value / 50) * 50))


def week(phase: str, state: dict, light: bool, index: int, css: float | None, css_test: bool) -> dict:
    """Three sessions for one week. state carries weekly_m, continuous_m and css_step across weeks."""
    weekly = state["weekly_m"] * (1 - DEFAULTS["light_cut_pct"] / 100) if light else state["weekly_m"]
    continuous = state["continuous_m"] * (0.75 if light or phase in ("taper", "race_week") else 1)
    drill = f"{DRILLS[index % len(DRILLS)]} and {DRILLS[(index + 2) % len(DRILLS)]}"
    c_total = 200 + _round50(continuous) + 100
    rest = max(1000, weekly - c_total)
    each = _round50(rest / 2)

    def session(name, drills_m, main_text, main_m):
        total = 200 + drills_m + main_m + 100
        return {"name": name, "distance_m": total,
                "session": f"200 easy, {drills_m} m drills ({drill}), {main_text}, 100 easy"}

    css_set = CSS_SETS[min(state["css_step"], len(CSS_SETS) - 1)]
    reps, dist, offset, rest_s = css_set
    css_text = f"{reps} x {dist} m @ {_at(css, offset)} / {rest_s} rest"
    css_m = reps * dist
    continuous_text = f"{_round50(continuous)} m continuous, easy and relaxed (endurance pace" + \
                      (f" ~{fmt(css + 10)}/100 m)" if css else ")")
    sessions = []
    if css_test:
        sessions.append({"name": "CSS test", "distance_m": 1300,
                         "session": "400 easy + drills, 400 m all-out (time it), 10 min easy, "
                                    "200 m all-out (time it), 200 easy. Record with record_css_test.",
                         "hard": True})
    if phase == "base":
        main = max(200, each - 500)
        sessions += [session("Technique", 300, f"{main // 100} x 100 m steady / 20 s", _round50(main)),
                     session("Kick + aerobic", 200, f"4 x 50 m kick, {max(1, (main - 200) // 200)} x 200 m easy / 30 s",
                             _round50(main))]
    elif phase in ("build", "peak"):
        room = each - 400  # the main set's share of the session
        pull = _round50(max(0, room - css_m))
        over = max(0, css_m - room)  # a CSS set bigger than its share is taken from the pull session
        reps300 = max(1, int(max(300, room - over) // 300))
        sessions += [session("CSS set", 200, css_text + (f", {pull} m pull easy" if pull else ""), css_m + pull),
                     session("Pull + sighting" if phase == "peak" else "Pull endurance", 200,
                             f"{reps300} x 300 m pull buoy steady / 30 s"
                             + (", sight every 6-9 strokes" if phase == "peak" else ""), reps300 * 300)]
        sessions[-2]["hard"] = not light
    else:  # taper and race week: short, smooth, a few fast 50s
        sessions += [session("Sharpen", 200, "6 x 50 m fast / 30 s, 4 x 100 m steady / 20 s", 700),
                     session("Easy technique", 300, "4 x 100 m easy / 20 s", 400)]
    sessions.append({"name": "Continuous", "distance_m": c_total,
                     "session": f"200 easy, {continuous_text}, 100 easy"})
    if css_test:  # the test is the week's hard swim: keep an easy middle session
        easy = next((x for x in sessions[1:-1] if not x.get("hard")), sessions[1])
        sessions = [sessions[0], easy, sessions[-1]]
    return {"sessions": len(sessions), "weekly_m": sum(s["distance_m"] for s in sessions),
            "target_weekly_m": _round50(weekly), "continuous_m": _round50(continuous),
            "css_test": css_test, "plan": sessions}


def advance(state: dict, phase: str, light: bool) -> None:
    """Grow volume and continuous distance after a normal week; CSS sets advance in build and peak."""
    if light or phase in ("taper", "race_week"):
        return
    state["weekly_m"] = min(DEFAULTS["weekly_cap_m"], state["weekly_m"] * (1 + DEFAULTS["weekly_step_pct"] / 100))
    state["continuous_m"] = min(DEFAULTS["continuous_cap_m"], state["continuous_m"] + DEFAULTS["continuous_step_m"])
    if phase in ("build", "peak"):
        state["css_step"] += 1
