"""Season week target -> concrete workouts, one per day and sport (docs/rolling-plan.md).

season.generate() gives each Monday week numbers (run km, long run, quality sessions, swims,
gym, flex). This module places them on days and writes the workout steps, so the whole plan
to race day exists before anything is sent to Garmin. Pure and deterministic: the same target,
paces and schedule give the same days, so the result can be stored, diffed, tested over a full
season and ported to the mobile app.

Each item is {"date", "sport", "kind", "name", "spec", "session"}: spec is what upload_plan
takes as "workout" ({sport, name, steps, description, pool_length_m?}) and session holds its
hints (intensity, all_out...). Easy runs get an HR zone only, never a pace.
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta

from garmin_ai import workouts
from garmin_ai.coaching import sessions

DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
# Where things go in a normal week. Quality days are 48 h apart, the long run is not the day
# after quality, the hard swim shares the first quality day (one hard day, not two).
DEFAULT_SCHEDULE = {
    "quality": ["tue", "thu"], "long": "sun", "test": "wed",
    "easy": ["wed", "sat", "mon", "fri"],
    "swim_hard": "tue", "swims": ["mon", "fri", "wed"],
    "bike": "wed", "gym": "mon", "flex": "fri",
    # Optional: the day of the long continuous swim, and weekday swims done at midday (kept
    # within rules swim.max_midday_minutes; the meters cut go to the long swim).
    "swim_long": None, "midday_swims": [],
}
EASY_ZONE, STEADY_ZONE = 2, 3
WARMUP_M, COOLDOWN_M = 2000, 1500
MIN_EASY_KM = 3.0
POOL_M = 25
BIKE_MIN = {"normal": 45, "short": 30}
GYM_MIN = 40
PACE_NAMES = {"T": "T", "I": "I", "M": "M", "R": "R"}


# --- paces -------------------------------------------------------------------------------

def paces_for(vdot: float) -> dict:
    """The training_paces shape (workouts.training_paces) for a VDOT."""
    out = {}
    for name, (fast, slow) in workouts.PACE_FRACTIONS.items():
        quick, easy = workouts._pace_for(slow, vdot), workouts._pace_for(fast, vdot)
        if name != "easy":
            center = (quick + easy) / 2
            quick, easy = center - 5, center + 5
        out[name] = {"pace_min_per_km": [workouts.format_pace(quick), workouts.format_pace(easy)],
                     "seconds_per_km": [round(quick), round(easy)]}
    return {"vdot": round(vdot, 1), "paces": out}


def hr_targets(zones: dict | None) -> dict | None:
    """Easy and steady HR ranges from the watch's zones (zone floors, e.g. Garmin's lactate
    threshold zones): easy = zone 1 floor to below zone 3, steady (marathon pace) = zone 3."""
    floors = (zones or {}).get("floors") or []
    if len(floors) < 4 or not all(isinstance(f, (int, float)) and f > 0 for f in floors[:4]):
        return None
    return {"easy": {"hr_bpm": [floors[0], floors[2] - 1]}, "steady": {"hr_bpm": [floors[2], floors[3] - 1]}}


class Paces:
    """Pace targets from the VDOT and goal, and the HR targets for easy and steady running
    (the watch's zones when known, else Garmin zone 2 and 3)."""

    def __init__(self, vdot: float, goal_s_per_km: float | None, hr: dict | None = None):
        self.vdot = vdot
        self.goal = goal_s_per_km or sessions.pace(vdot, "T")  # no goal time: threshold pace
        self.easy = (hr or {}).get("easy") or {"hr_zone": EASY_ZONE}
        self.steady = (hr or {}).get("steady") or {"hr_zone": STEADY_ZONE}

    def seconds(self, name: str) -> float:
        return self.goal if name == "goal" else sessions.pace(self.vdot, PACE_NAMES[name])

    def target(self, name: str) -> dict:
        s = self.seconds(name)
        return {"pace_min_per_km": [workouts.format_pace(s - 5), workouts.format_pace(s + 5)]}

    def text(self, name: str) -> str:
        return workouts.format_pace(self.seconds(name)) + "/km"


def _resolve(steps: list[dict], paces: Paces) -> list[dict]:
    """sessions.py steps (named paces) -> upload_plan steps (pace ranges)."""
    out = []
    for step in steps:
        if step["type"] == "repeat":
            out.append({"type": "repeat", "repeat": {"count": step["repeat"]["count"],
                                                     "steps": _resolve(step["repeat"]["steps"], paces)}})
            continue
        clean = {"type": step["type"], "duration": step["duration"]}
        if step.get("pace"):
            clean["target"] = paces.target(step["pace"])
        if step.get("notes"):
            clean["notes"] = step["notes"]
        out.append(clean)
    return out


# --- run builders ------------------------------------------------------------------------

def _easy_step(target: dict, kind: str, meters: float, notes: str | None = None) -> dict:
    step = {"type": kind, "duration": {"distance_m": round(meters)}, "target": dict(target)}
    if notes:
        step["notes"] = notes
    return step


def _run(name: str, steps: list[dict], description: str | None = None) -> dict:
    return {"sport": "running", "name": name, "steps": steps, **({"description": description} if description else {})}


def _quality(session: dict, paces: Paces, name: str) -> dict:
    main = _resolve(session.get("steps") or _parse_reps(session["session"]), paces)
    steps = ([_easy_step(paces.easy, "warmup", WARMUP_M, "easy, then 4 strides")] + main
             + [_easy_step(paces.easy, "cooldown", COOLDOWN_M)])
    return _run(name, steps, f"{session['session']}" + (f" ({session['dose']})" if session.get("dose") else ""))


REPS = re.compile(r"(\d+)\s*x\s*(\d+(?:\.\d+)?)\s*(km|m)\s*(?:@ goal pace|at (\w+))\s*/\s*(\d+)\s*min jog")


def _parse_reps(text: str) -> list[dict]:
    """Sharpening sessions are short text ("4 x 1 km @ goal pace / 2 min jog")."""
    match = REPS.search(text)
    if not match:
        return [sessions.work(None, text, time_s=600)]
    n, size, unit, pace_name, rest = match.groups()
    meters = float(size) * (1000 if unit == "km" else 1)
    return [sessions.repeat(int(n), sessions.work(pace_name or "goal", distance_m=meters),
                            sessions.jog(time_s=int(rest) * 60))]


def _long(km: float, fmt: str, paces: Paces) -> dict:
    finish_m = 0
    if "last 15 min at marathon pace" in fmt:
        finish_m = round(15 * 60 / paces.seconds("M") * 1000 / 100) * 100
    elif "last 3 km at marathon pace" in fmt:
        finish_m = 3000
    steps = [_easy_step(paces.easy, "interval", km * 1000 - finish_m)]
    if finish_m:
        steps.append({"type": "interval", "duration": {"distance_m": finish_m},
                      "target": dict(paces.steady), "notes": f"marathon pace ~{paces.text('M')}"})
    return _run(f"Long run {km:g} km", steps, f"Long run, {fmt}.")


SPEED = re.compile(r"(\d+)\s*x\s*(\d+)\s*(s|m)\b")


def _extra_steps(text: str, paces: Paces) -> list[dict]:
    """Strides and short speed after an easy run: by feel, no pace target (keeps the run easy)."""
    if text.startswith("strides"):
        return [sessions.repeat(5, {"type": "interval", "duration": {"time_s": 20},
                                    "notes": "stride: quick and relaxed"},
                                {"type": "recovery", "duration": {"time_s": 60}})]
    match = SPEED.search(text)
    n, size, unit = (int(match[1]), int(match[2]), match[3]) if match else (6, 10, "s")
    work = {"type": "interval", "duration": {"time_s": size} if unit == "s" else {"distance_m": size},
            "notes": text.split(" after ")[0] + (f" (R ~{paces.text('R')})" if "R pace" in text else "")}
    back = {"type": "recovery", "duration": {"distance_m": size} if unit == "m" else {"time_s": 90}}
    return [sessions.repeat(n, work, back)]


def _easy(km: float, extras: list[str], paces: Paces, total_km: float | None = None) -> dict:
    """An easy run of km plus its extras; total_km (with the extras) is the name's distance."""
    steps = [_easy_step(paces.easy, "interval", km * 1000)]
    for text in extras:
        steps += _extra_steps(text, paces)
    label = " + strides" if any(e.startswith("strides") for e in extras) else ""
    if any(not e.startswith("strides") for e in extras):
        label = " + speed"
    hr = paces.easy.get("hr_bpm")
    return _run(f"Easy run {total_km or km:g} km{label}", steps, "Easy, conversational. " +
                (f"HR {hr[0]}-{hr[1]}." if hr else "HR zone 2.") +
                (" " + "; ".join(extras) + "." if extras else ""))


# --- swims, bike, gym --------------------------------------------------------------------

SWIM_EASY = re.compile(r"^(\d+) easy$")
SWIM_DRILLS = re.compile(r"^(\d+) m drills \((.+)\)$")
SWIM_REPS = re.compile(r"^(\d+) x (\d+) m (.+?)(?: / (\d+) s(?: rest)?)?$")
SWIM_ONE = re.compile(r"^(\d+) m (.+)$")


def _swim_detail(text: str) -> dict:
    lower = text.lower()
    if "kick" in lower:
        return {"stroke": "free", "drill": "kick"}
    if "pull" in lower:
        return {"stroke": "free", "equipment": "pull_buoy"}
    return {"stroke": "free"}


def _swim_steps(text: str) -> list[dict]:
    """swim.week session text -> steps ("200 easy, 300 m drills (...), 6 x 100 m @ CSS / 20 s, 100 easy")."""
    pieces = [p.strip() for p in text.split(", ")]
    steps: list[dict] = []
    for i, piece in enumerate(pieces):
        if m := SWIM_EASY.match(piece):
            steps.append({"type": "warmup" if not steps else "cooldown",
                          "duration": {"distance_m": int(m[1])}, "swim": {"stroke": "free"}, "notes": "easy"})
        elif m := SWIM_DRILLS.match(piece):
            steps.append({"type": "interval", "duration": {"distance_m": int(m[1])},
                          "swim": {"stroke": "drill"}, "notes": m[2]})
        elif m := SWIM_REPS.match(piece):
            body = {"type": "interval", "duration": {"distance_m": int(m[2])}, "swim": _swim_detail(m[3]),
                    "notes": m[3]}
            inner = [body] + ([{"type": "rest", "duration": {"time_s": int(m[4])}}] if m[4] else [])
            steps.append({"type": "repeat", "repeat": {"count": int(m[1]), "steps": inner}})
        elif m := SWIM_ONE.match(piece):
            steps.append({"type": "interval", "duration": {"distance_m": int(m[1])}, "swim": _swim_detail(m[2]),
                          "notes": m[2]})
        elif steps:  # a remark on the step before ("easy and relaxed", "sight every 6-9 strokes")
            last = steps[-1] if steps[-1]["type"] != "repeat" else steps[-1]["repeat"]["steps"][0]
            last["notes"] = f"{last.get('notes', '')}, {piece}".strip(", ")
    return steps


def _css_test_steps() -> list[dict]:
    return [{"type": "warmup", "duration": {"distance_m": 400}, "swim": {"stroke": "free"}, "notes": "easy + drills"},
            {"type": "interval", "duration": {"distance_m": 400}, "swim": {"stroke": "free"},
             "notes": "all-out, even pace: time it"},
            {"type": "interval", "duration": {"time_s": 600}, "swim": {"stroke": "free"}, "notes": "easy"},
            {"type": "interval", "duration": {"distance_m": 200}, "swim": {"stroke": "free"},
             "notes": "all-out: time it"},
            {"type": "cooldown", "duration": {"distance_m": 200}, "swim": {"stroke": "free"}, "notes": "easy"}]


def _swim(session: dict) -> dict:
    test = session["name"] == "CSS test"
    return {"sport": "lap_swimming", "name": f"Swim: {session['name']} {session['distance_m']} m",
            "steps": _css_test_steps() if test else _swim_steps(session["session"]),
            "description": session["session"], "pool_length_m": POOL_M}


def _steady(session: dict) -> dict:
    """A CSS set swum steady instead (a time-trial week), same distance."""
    main = max(100, (session["distance_m"] - 300) // 100 * 100)
    return {"name": "Steady", "distance_m": main + 300,
            "session": f"200 easy, {main // 100} x 100 m steady / 20 s, 100 easy"}


def _swim_steps_minutes(steps: list[dict], pace: float) -> float:
    total = 0.0
    for step in steps:
        if step["type"] == "repeat":
            total += step["repeat"]["count"] * _swim_steps_minutes(step["repeat"]["steps"], pace)
            continue
        duration = step["duration"]
        total += duration.get("distance_m", 0) * pace / 100 / 60 + duration.get("time_s", 0) / 60
    return total


def _swim_meters(steps: list[dict]) -> int:
    total = 0
    for step in steps:
        if step["type"] == "repeat":
            total += step["repeat"]["count"] * _swim_meters(step["repeat"]["steps"])
        else:
            total += step["duration"].get("distance_m", 0)
    return int(total)


def _trim(steps: list[dict], minutes: float, pace: float) -> int:
    """Cut a swim to `minutes`: main-set repeats first, then long single steps, then the
    warm-up. Returns the meters cut (the steps are changed in place)."""
    before = _swim_meters(steps)
    while _swim_steps_minutes(steps, pace) > minutes:
        repeats = [s for s in steps if s["type"] == "repeat" and s["repeat"]["count"] > 1]
        singles = [s for s in steps if s["type"] == "interval" and s["duration"].get("distance_m", 0) > 100]
        warm = [s for s in steps if s["type"] == "warmup" and s["duration"].get("distance_m", 0) > 100]
        if repeats:
            max(repeats, key=lambda s: s["repeat"]["count"])["repeat"]["count"] -= 1
        elif singles:
            max(singles, key=lambda s: s["duration"]["distance_m"])["duration"]["distance_m"] -= 50
        elif warm:
            warm[0]["duration"]["distance_m"] -= 50
        else:
            break
    return before - _swim_meters(steps)


def _rename(spec: dict) -> None:
    spec["name"] = re.sub(r"\d+ m$", f"{_swim_meters(spec['steps'])} m", spec["name"])


def _swim_text(steps: list[dict]) -> str:
    """A swim's steps as the plan's short text ("200 easy, 4 x 100 m steady / 20 s, 100 easy"),
    so the description matches the steps after a midday swim is cut or a long swim grows."""
    def one(step: dict) -> str:
        meters = step["duration"].get("distance_m")
        size = f"{meters} m" if meters else f"{round(step['duration'].get('time_s', 0) / 60)} min"
        notes = step.get("notes") or ""
        if step["type"] in ("warmup", "cooldown"):
            return f"{meters} easy" if meters else f"{size} easy"
        if (step.get("swim") or {}).get("stroke") == "drill":
            return f"{size} drills ({notes})"
        return f"{size} {notes}".strip()
    parts = []
    for step in steps:
        if step["type"] == "repeat":
            inner = step["repeat"]["steps"]
            rest = next((s["duration"].get("time_s") for s in inner if s["type"] == "rest"), None)
            work = next(s for s in inner if s["type"] != "rest")
            parts.append(f"{step['repeat']['count']} x {one(work)}" + (f" / {rest} s" if rest else ""))
        else:
            parts.append(one(step))
    return ", ".join(parts)


def _fit_midday(swims: list[list], sched: dict, rules: dict, pace: float) -> None:
    """Midday swims within rules swim.max_midday_minutes: a CSS test that cannot fit trades
    days with the long swim; others are cut, and the meters cut are added to the long swim as
    easy aerobic swimming, up to rules swim.max_long_minutes (default 75)."""
    midday = set(sched.get("midday_swims") or [])
    limit = (rules.get("swim") or {}).get("max_midday_minutes", 25)
    long_entry = next((e for e in swims if e[0] == sched.get("swim_long")), None)
    for entry in swims:
        if entry[0] in midday and entry[1]["name"] == "CSS test" and long_entry and long_entry[0] not in midday \
                and _swim_steps_minutes(entry[2]["steps"], pace) > limit:
            entry[0], long_entry[0] = long_entry[0], entry[0]
            long_entry = entry
    cut = 0
    for day, session, spec in swims:
        if day in midday and session["name"] != "CSS test":
            moved = _trim(spec["steps"], limit, pace)
            if moved:
                cut += moved
                _rename(spec)
                spec["description"] = f"{_swim_text(spec['steps'])} (kept within {limit} min for a midday swim)"
    long_max = (rules.get("swim") or {}).get("max_long_minutes", 75)
    if long_entry and long_entry[1]["name"] != "CSS test" and long_entry[0] not in midday:
        room = (long_max - _swim_steps_minutes(long_entry[2]["steps"], pace)) * 60 / pace * 100
        cut = min(cut, max(0, room))  # what does not fit is dropped: the week swims a little less
    else:
        cut = 0  # never add meters to a CSS test
    cut = int(cut) // 50 * 50
    if cut and long_entry:
        spec = long_entry[2]
        extra = {"type": "interval", "duration": {"distance_m": cut}, "swim": {"stroke": "free"},
                 "notes": "easy aerobic (the meters cut from the midday swims)"}
        at = next((i for i, s in enumerate(spec["steps"]) if s["type"] == "cooldown"), len(spec["steps"]))
        spec["steps"].insert(at, extra)
        _rename(spec)
        spec["description"] = _swim_text(spec["steps"])


def _timed(sport: str, name: str, minutes: int, zone: int | None, description: str) -> dict:
    step = {"type": "interval", "duration": {"time_s": minutes * 60}}
    if zone:
        step["target"] = {"hr_zone": zone}
    return {"sport": sport, "name": name, "steps": [step], "description": description}


# --- the week ----------------------------------------------------------------------------

class _Week:
    def __init__(self, monday: date, schedule: dict):
        self.monday, self.schedule, self.items = monday, schedule, []

    def day(self, name: str) -> str:
        return (self.monday + timedelta(days=DAYS.index(name))).isoformat()

    def add(self, day: str, kind: str, spec: dict, session: dict | None = None) -> dict:
        item = {"date": day, "sport": workouts.VALIDATOR_SPORTS[workouts.sport_key(spec["sport"])],
                "kind": kind, "name": spec["name"], "spec": spec, "session": session or {}}
        self.items.append(item)
        return item

    def run_days(self) -> set[str]:
        return {i["date"] for i in self.items if i["sport"] == "run"}


def km_of(spec: dict, paces: dict) -> float:
    """Run km of a spec the way the validator counts it (workouts.to_session)."""
    payload = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                     spec.get("pool_length_m"))
    return workouts.to_session("2000-01-03", payload, paces).get("km") or 0.0


def week(target: dict, vdot: float, goal_s_per_km: float | None = None, race: dict | None = None,
         schedule: dict | None = None, rules: dict | None = None, hr: dict | None = None,
         swim_pace: float | None = None) -> list[dict]:
    """The workouts of one season week (a season.generate(..., with_steps=True) entry). hr:
    hr_targets() from the watch's zones; without it easy runs target Garmin zone 2. swim_pace
    (s/100 m, the athlete's own) times the midday swims.

    A time-trial week (a fitness checkpoint, always a lighter week) runs the trial mid-week on
    the "test" day, has one run fewer, and swims its CSS set steady instead."""
    sched = {**DEFAULT_SCHEDULE, **(schedule or {})}
    paces = Paces(vdot, goal_s_per_km, hr)
    table = paces_for(vdot)
    w = _Week(date.fromisoformat(target["week_start"]), sched)
    phase, quality = target["phase"], list(target["quality"])
    race_day = race and race.get("date")
    race_week = phase == "race_week" and race_day and w.day("mon") <= race_day <= w.day("sun")
    runs_left = target["runs"]

    # 1. Quality, races, time trials.
    if race_week:
        race_date = date.fromisoformat(race_day)
        sharpen_day = max(race_date - timedelta(days=3), w.monday).isoformat()
        events = [q for q in quality if q["type"] == "race"]
        from garmin_ai.coaching import season
        multi = season.events(race)
        labels = ([(e["event"], e["km"]) for e in multi] if multi else
                  [(season.distance_key(race), season.DISTANCE_M[season.distance_key(race)] / 1000)])
        for q in quality:
            if q["type"] == "sharpen":
                w.add(sharpen_day, "sharpen", _quality(q, paces, "Sharpen: goal pace"), {"intensity": "quality"})
        for i, (q, (label, km)) in enumerate(zip(events, labels)):
            if i == len(events) - 1:
                steps = [_easy_step(paces.easy, "warmup", WARMUP_M if km <= 10 else 1000, "easy, then 4 strides"),
                         {"type": "interval", "duration": {"distance_m": km * 1000}, "notes": q["session"]},
                         _easy_step(paces.easy, "cooldown", 1000)]
                w.add(race_day, "race", _run(f"Race day: {label}", steps, q["session"]),
                      {"intensity": "race", "all_out": True})
            else:
                steps = [{"type": "interval", "duration": {"distance_m": km * 1000},
                          "target": dict(paces.steady), "notes": q["session"]}]
                w.add(race_day, "event", _run(f"{label} controlled", steps, q["session"]), {"intensity": "easy"})
        runs_left -= 2 if events and any(q["type"] == "sharpen" for q in quality) else 1
        allowed = lambda d: d < race_day
    else:
        allowed = lambda d: True
        days = iter(sched["quality"])
        for q in quality:
            if q["type"] == "time_trial":
                spec = _run(f"{target.get('checkpoint', {}).get('test', '5K')} time trial",
                            [_easy_step(paces.easy, "warmup", WARMUP_M, "15 min easy + 4 strides"),
                             {"type": "interval", "duration": {"distance_m": target["checkpoint"]["distance_m"]},
                              "notes": "all-out at even pace"},
                             _easy_step(paces.easy, "cooldown", COOLDOWN_M, "10 min easy")], q["session"])
                w.add(w.day(sched["test"]), "time_trial", spec, {"intensity": "race", "all_out": True})
                runs_left -= 2  # the trial, and one easy run fewer that week
                continue
            day = w.day(next(days))
            label = {"threshold": "Threshold", "vo2": "VO2", "race_pace": "Goal pace",
                     "sharpen": "Sharpen"}.get(q["type"], q["type"].title())
            if q.get("rehearsal"):
                lead_km = _rehearsal_km(q["session"])
                w.add(day, "rehearsal_am", _run(f"Rehearsal AM steady {lead_km:g} km",
                                                [{"type": "interval", "duration": {"distance_m": lead_km * 1000},
                                                  "target": dict(paces.steady),
                                                  "notes": "steady: marathon pace or slower"}], q["session"]),
                      {"intensity": "easy"})
                runs_left -= 1
                label = "Rehearsal PM goal pace"
            w.add(day, q["type"], _quality(q, paces, f"{label}: {q.get('format', '')}".rstrip(": ")),
                  {"intensity": "quality"})
            runs_left -= 1

    # 2. Long run.
    long_km = target.get("long_run_km") or 0
    if long_km and not race_week:
        w.add(w.day(sched["long"]), "long", _long(long_km, target.get("long_run") or "easy", paces), {"kind": "long"})
        runs_left -= 1

    # 3. Easy runs fill the rest of the week's km.
    taken = w.run_days()
    unused_quality = [d for d in sched["quality"] if w.day(d) not in taken]
    order = list(dict.fromkeys(sched["easy"] + unused_quality + list(DAYS)))
    if race_week:  # before the race; the day before the race is the last choice
        before = (date.fromisoformat(race_day) - timedelta(days=1)).isoformat()
        order.sort(key=lambda d: w.day(d) == before)
    days = [w.day(d) for d in order if w.day(d) not in taken and allowed(w.day(d))][:max(0, runs_left)]
    fixed = sum(km_of(i["spec"], table) for i in w.items if i["sport"] == "run")
    _fill_easy(w, sorted(days), max(0.0, target["run_km"] - fixed), target.get("extras") or [], paces, table)

    # 4. Swims: the hard one on the hard-swim day, the long continuous one on the long-swim day
    #    (when set), the others in order; midday swims are kept short.
    plan = list((target.get("swim") or {}).get("plan") or [])
    if any(q["type"] == "time_trial" for q in quality):  # the trial is the week's hard effort
        plan = [_steady(s) if s["name"] == "CSS set" else s for s in plan]
    # A CSS set or test goes on the hard-swim day even in a lighter week (where it is not
    # flagged hard), so it never lands next to a quality run.
    hard = next((s for s in plan if s.get("hard") or s["name"] in ("CSS set", "CSS test")), None)
    longest = next((s for s in plan if s["name"] == "Continuous"), None) if sched.get("swim_long") else None
    placed = {}
    if hard is not None:
        placed[id(hard)] = sched["swim_hard"]
    if longest is not None and sched["swim_long"] not in placed.values():
        placed[id(longest)] = sched["swim_long"]
    free = iter([d for d in sched["swims"] if d not in placed.values()])
    swims = []
    for s in plan:
        day = placed.get(id(s)) or next(free, None)
        if day is None or (race_week and w.day(day) >= race_day):
            continue
        swims.append([day, s, _swim(s)])
    _fit_midday(swims, sched, rules or {}, swim_pace or workouts.SWIM_S_PER_100M)
    for day, s, spec in swims:
        hints = {"slot": "midday"} if day in (sched.get("midday_swims") or []) else {}
        w.add(w.day(day), "swim_css" if s is hard else "swim", spec, hints)

    # 5. Bike, gym, flexible slot.
    short = phase in ("taper", "race_week")
    bike_day = w.day(sched["bike"])
    bikes = ((rules or {}).get("structure") or {}).get("bikes_min", 1)
    if bikes and (not race_week or bike_day < race_day):
        minutes = BIKE_MIN["short" if short else "normal"]
        w.add(bike_day, "bike", _timed("cycling", f"Bike easy {minutes} min", minutes, EASY_ZONE,
                                       "Easy spin, HR zone 2."))
    for i in range(target.get("gym_min") or 0):
        w.add(w.day(sched["gym"]), "gym", _timed("strength", f"Gym {GYM_MIN} min" + (f" #{i + 1}" if i else ""),
                                                 GYM_MIN, None, "Strength session."))
    flex = target.get("flex")
    if flex and not race_week:
        sport, minutes = ("cycling", BIKE_MIN["normal"]) if flex == "bike" else ("strength", GYM_MIN)
        w.add(w.day(sched["flex"]), "flex", _timed(sport, f"{flex.title()} (opt) {minutes} min", minutes,
                                                   EASY_ZONE if flex == "bike" else None,
                                                   "Optional: only if you feel fresh."))
    return sorted(w.items, key=lambda i: (i["date"], i["sport"] != "run", i["name"]))


HARD_KINDS = {"threshold", "vo2", "race_pace", "sharpen", "time_trial", "race", "event", "rehearsal_am"}


def _load_next_to(day: str, hard: set[str], longs: set[str]) -> int:
    """How much a day of short speed work would stack on hard days: the day before a quality
    run counts most (it would make two hard days in a row), the day after one next, then the
    days around the long run."""
    def near(offset: int) -> str:
        return (date.fromisoformat(day) + timedelta(days=offset)).isoformat()
    return 3 * (near(1) in hard) + 2 * (near(-1) in hard) + (near(1) in longs) + (near(-1) in longs)


def _fill_easy(w: _Week, days: list[str], km: float, extras: list[str], paces: Paces, table: dict) -> None:
    while days and km / len(days) < MIN_EASY_KM:  # too little left: fewer, longer easy runs
        days = days[:-1]
    if not days:
        return
    extra_for = {d: [] for d in days}
    speed = [e for e in extras if not e.startswith("strides")]
    strides = [e for e in extras if e.startswith("strides")]
    hard = {i["date"] for i in w.items if i["kind"] in HARD_KINDS}
    longs = {i["date"] for i in w.items if i["kind"] == "long"}
    ranked = sorted(days, key=lambda d: (_load_next_to(d, hard, longs), d))
    if speed:  # short speed (200s, hill sprints) on the easy day furthest from hard days
        extra_for[ranked[0]] += speed
    if strides:
        extra_for[ranked[1] if len(ranked) > 1 and speed else ranked[0]] += strides
    overhead = {d: km_of(_easy(0.5, extra_for[d], paces), table) - 0.5 for d in days}
    base = max(0.0, km - sum(overhead.values())) / len(days)
    share = [int(base * 2) / 2 for _ in days]  # half-km steps, never over the target
    share[-1] = math.floor((max(0.0, km - sum(overhead.values())) - sum(share[:-1])) * 10 + 1e-6) / 10
    # Each run is rounded to 0.1 km when the validator adds the week up: trim the last one
    # until the week is at or under its target.
    while share[-1] > 0 and sum(km_of(_easy(k, extra_for[d], paces), table) for d, k in zip(days, share)) > km + 1e-6:
        share[-1] = round(share[-1] - 0.1, 1)
    for d, k in zip(days, share):
        if k <= 0:
            continue
        total = km_of(_easy(k, extra_for[d], paces), table)
        w.add(d, "easy", _easy(k, extra_for[d], paces, total), {"intensity": "easy"})


def _rehearsal_km(text: str) -> float:
    match = re.search(r"AM (\d+(?:\.\d+)?) km", text)
    return float(match[1]) if match else 7.0


def to_sessions(items: list[dict], paces: dict, swim_s_per_100m: float = workouts.SWIM_S_PER_100M) -> list[dict]:
    """Validator sessions for materialized items (what upload_plan would check)."""
    out = []
    for item in items:
        spec = item["spec"]
        payload = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                         spec.get("pool_length_m"))
        out.append(workouts.to_session(item["date"], payload, paces, item.get("session"), swim_s_per_100m))
    workouts.mark_long_runs(out)
    return out
