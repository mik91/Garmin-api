"""Quality-session library: the dose progresses, the format rotates.

Each type has a dose ladder (minutes at threshold, minutes at VO2 pace, km at goal race pace)
that advances through the season, capped by Daniels' limits relative to the week's volume:
threshold <= ~10% of weekly km, VO2 work <= ~8%. The format that delivers the dose rotates
(cruise intervals, pyramids, progression runs, tempo fartlek, hills, cutdowns, alternations...),
so sessions stay varied without the progression or the safety limits depending on the model.

Paces: T (threshold), I (VO2max intervals), M (marathon), R (repetition) are Daniels paces
from the athlete's VDOT; "goal pace" is the A race goal. The coach reads fresh paces from
training_paces when building the workout; the plan only names the intensity.
"""
from __future__ import annotations

from garmin_ai import workouts

FRACTIONS = {"T": 0.88, "I": 0.975, "M": 0.81, "R": 1.05}

DOSES = {
    "threshold": [15, 16, 18, 20, 20, 22, 24, 24, 25, 26, 28, 30],  # minutes at T
    "vo2": [10, 12, 12, 14, 15, 16, 16, 18, 18, 20],                 # minutes at I
    "race_pace": [5.0, 5.0, 6.0, 6.0, 6.5, 7.0, 7.0],                # km at goal pace
}
LIMITS = {"threshold": 0.10, "vo2": 0.08}  # share of the week's km at that intensity

ROTATION = {
    "threshold": ["cruise", "pyramid", "progression", "cruise", "tempo", "fartlek"],
    "vo2": ["minutes", "800s", "pyramid", "1k", "hills", "minutes", "pyramid", "1k"],
    "race_pace": ["blocks", "alternations", "blocks", "cutdown"],
}
T_PYRAMIDS = [[2, 3, 4, 3, 2], [2, 4, 6, 4, 2], [3, 4, 5, 4, 3], [3, 5, 6, 5, 3], [4, 5, 6, 5, 4],
              [4, 6, 8, 6, 4]]
I_PYRAMIDS = [[1, 2, 3, 2, 1], [1, 2, 3, 3, 2, 1], [2, 3, 4, 3, 2], [1, 2, 3, 4, 3, 2, 1],
              [1, 2, 3, 4, 4, 3, 2, 1]]
SPEED = ["6 x 10 s hill sprints", "8 x 200 m at R pace / 200 m jog", "8 x 10 s hill sprints",
         "6 x 200 m at R pace / 200 m jog", "8 x 15 s hill sprints", "8 x 200 m at R pace / 200 m jog",
         "6 x 30 s hills, jog down"]


def pace(vdot: float, kind: str) -> float:
    """Seconds per km at Daniels intensity kind (T, I, M, R) for a VDOT."""
    return workouts._pace_for(FRACTIONS[kind], vdot)


def capped_dose(kind: str, step: int, week_km: float, vdot: float) -> float:
    ladder = DOSES[kind]
    dose = ladder[min(step, len(ladder) - 1)]
    if kind in LIMITS and week_km:
        seconds_per_km = pace(vdot, "T" if kind == "threshold" else "I")
        limit = LIMITS[kind] * week_km * seconds_per_km / 60  # minutes
        dose = min(dose, max(ladder[0] * 0.8, limit))
    return dose


def fit(total: float, rep: float) -> int:
    """Reps of `rep` that fit in `total` (a little over is fine, never a whole rep over)."""
    return int((total + rep * 0.25) // rep)


def _closest(patterns: list[list[int]], dose: float) -> list[int]:
    fitting = [p for p in patterns if sum(p) <= dose + 1] or patterns[:1]
    return max(fitting, key=sum)


def threshold(fmt: str, minutes: float, step: int) -> tuple[str, float]:
    rest = "2 min" if step < 4 else "90 s" if step < 8 else "60 s"
    if fmt == "tempo" and minutes < 18:
        fmt = "cruise"
    if fmt == "cruise":
        rep = 5 if minutes < 20 else 6 if minutes < 24 else 8 if minutes < 28 else 10
        n = max(2, fit(minutes, rep))
        return f"cruise intervals: {n} x {rep} min T / {rest} jog", n * rep
    if fmt == "pyramid":
        p = _closest(T_PYRAMIDS, minutes)
        return f"threshold pyramid: {'-'.join(map(str, p))} min at T / 90 s jog", sum(p)
    if fmt == "progression":
        m = round(minutes)
        return (f"progression run: 15 min easy, then {m} min building from marathon pace to T "
                "(last 5 min at T)", m * 0.6)
    if fmt == "tempo":
        m = round(min(minutes, 30))
        return f"tempo: {m} min continuous at T", m
    k = 4 if minutes < 20 else 5
    n = max(3, fit(minutes, k))
    return f"tempo fartlek: {n} x ({k} min at T / 2 min steady), no stops", n * k


def vo2(fmt: str, minutes: float, i_pace: float, phase: str) -> tuple[str, float]:
    if fmt == "hills" and phase not in ("base", "build"):
        fmt = "minutes"
    if fmt == "minutes":
        rep = 2 if minutes < 14 else 3
        n = max(4, fit(minutes, rep))
        return f"{n} x {rep} min at I / {rep} min jog", n * rep
    if fmt in ("800s", "1k"):
        meters = 800 if fmt == "800s" else 1000
        n = max(4, fit(minutes * 60, meters / 1000 * i_pace))
        rest = "2 min" if meters == 800 else "2:30"
        return f"{n} x {meters} m at I / {rest} jog", n * meters / 1000 * i_pace / 60
    if fmt == "pyramid":
        p = _closest(I_PYRAMIDS, minutes)
        return f"VO2 pyramid: {'-'.join(map(str, p))} min at I / equal jog", sum(p)
    n = max(4, fit(minutes, 3))
    return f"{n} x 3 min uphill at I effort / jog down", n * 3


def race_pace(fmt: str, km: float, step: int) -> tuple[str, float]:
    if fmt == "alternations":
        total = round(km * 2)
        return (f"alternations: {total} km continuous, alternating 1 km @ goal pace / "
                "1 km at marathon pace", total / 2)
    if fmt == "cutdown":
        n = round(km)
        return (f"cutdown: {n} km continuous, starting at T and a little faster each km, "
                "last 2 km at goal pace", 2)
    rep = [1, 1.5, 2, 1, 3, 1][min(step, 5)]
    n = max(2, round(km / rep))
    rest = "1 min" if rep <= 1 else "90 s" if rep < 2 else "2 min"
    shown = f"{rep:g} km"
    return f"{n} x {shown} @ goal pace / {rest} jog", n * rep


def build(kind: str, step: int, seen: int, week_km: float, vdot: float, phase: str,
          light: bool = False) -> dict:
    """One quality session: the dose for `step`, the format for the `seen`-th time this type
    appears. A lighter week takes 80% of the dose."""
    dose = capped_dose(kind, step, week_km, vdot)
    if light:
        dose *= 0.8
    fmt = ROTATION[kind][seen % len(ROTATION[kind])]
    if kind == "threshold":
        text, at = threshold(fmt, dose, step)
        unit = "min at T"
    elif kind == "vo2":
        text, at = vo2(fmt, dose, pace(vdot, "I"), phase)
        unit = "min at I"
    else:
        text, at = race_pace(fmt, dose, step)
        unit = "km at goal pace"
    return {"type": kind, "format": fmt, "dose": f"{round(at, 1):g} {unit}", "session": text}


def long_run_format(phase: str, seen: int, light: bool) -> str:
    """Easy in base. In build and peak every other long run ends at marathon pace (moderate,
    so it does not become a third hard day next to two quality sessions). light covers lighter
    and rehearsal weeks: easy only."""
    if light or phase in ("base", "taper", "race_week"):
        return "easy"
    if phase == "build" and seen % 2 == 1:
        return "easy, last 15 min at marathon pace"
    if phase == "peak" and seen % 2 == 1:
        return "easy, last 3 km at marathon pace"
    return "easy"


def speed(index: int) -> str:
    return SPEED[index % len(SPEED)]
