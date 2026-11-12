"""Structured workouts: a small step schema in, Garmin Connect workout JSON out.

Pure functions only (no network, no database), so everything here is unit tested.

Enum ids come from Garmin's own GET /workout-service/workout/types. The JSON shape
(pace ranges stored fastest first, swim rest as fixed.rest, pool length on the
workout) was copied from workouts Garmin Connect and Runna created on the account.
"""
import hashlib
import json
import math
import re

PREFIX = "[CP] "

# key -> (id, displayOrder)
SPORTS = {"running": (1, 1), "cycling": (2, 2), "swimming": (4, 4), "strength_training": (5, 3)}
SPORT_ALIASES = {"running": "running", "run": "running", "cycling": "cycling", "bike": "cycling",
                 "lap_swimming": "swimming", "swimming": "swimming", "swim": "swimming",
                 "strength": "strength_training", "strength_training": "strength_training", "gym": "strength_training"}
STEP_TYPES = {"warmup": 1, "cooldown": 2, "interval": 3, "recovery": 4, "rest": 5, "repeat": 6, "other": 7, "main": 8}
CONDITIONS = {"lap.button": 1, "time": 2, "distance": 3, "iterations": 7, "fixed.rest": 8, "reps": 10}
TARGETS = {"no.target": 1, "power.zone": 2, "cadence": 3, "heart.rate.zone": 4, "pace.zone": 6}
STROKES = {"any_stroke": 1, "backstroke": 2, "breaststroke": 3, "drill": 4, "fly": 5, "free": 6,
           "individual_medley": 7, "mixed": 8}
STROKE_ALIASES = {"freestyle": "free", "any": "any_stroke", "im": "individual_medley", "butterfly": "fly",
                  "back": "backstroke", "breast": "breaststroke"}
DRILLS = {"kick": 1, "pull": 2, "drill": 3}
EQUIPMENT = {"fins": 1, "kickboard": 2, "paddles": 3, "pull_buoy": 4, "snorkel": 5}
UNITS = {"meter": {"unitId": 1, "unitKey": "meter", "factor": 100.0},
         "kilometer": {"unitId": 2, "unitKey": "kilometer", "factor": 100000.0}}
EXECUTABLE = ("warmup", "interval", "recovery", "rest", "cooldown", "other", "main")


class WorkoutError(ValueError):
    """The step description cannot be turned into a valid Garmin workout."""


# --- small helpers ----------------------------------------------------------------------

def sport_key(sport: str) -> str:
    key = SPORT_ALIASES.get(str(sport or "").strip().lower())
    if key is None:
        raise WorkoutError(f"Unknown sport {sport!r}; use running, cycling, lap_swimming or strength.")
    return key


def with_prefix(name: str) -> str:
    name = str(name or "").strip()
    if not name:
        raise WorkoutError("A workout needs a name.")
    return name if name.startswith(PREFIX) else PREFIX + name


def parse_pace(value) -> float:
    """Minutes per km from '4:30' or 4.5; returns seconds per km."""
    if isinstance(value, str):
        match = re.fullmatch(r"\s*(\d{1,2}):([0-5]\d)\s*", value)
        if not match:
            raise WorkoutError(f"Pace {value!r} must look like '4:30' (min:sec per km).")
        seconds = int(match[1]) * 60 + int(match[2])
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = float(value) * 60
    else:
        raise WorkoutError(f"Pace {value!r} must be 'm:ss' or minutes per km.")
    if not 120 <= seconds <= 1200:
        raise WorkoutError(f"Pace {value!r} is outside 2:00-20:00 per km.")
    return seconds


def format_pace(seconds_per_km: float) -> str:
    seconds = int(round(seconds_per_km))
    return f"{seconds // 60}:{seconds % 60:02d}"


def speed_from_pace(seconds_per_km: float) -> float:
    return round(1000.0 / seconds_per_km, 7)


def format_duration(seconds: float) -> str:
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else f"{rest // 60}:{rest % 60:02d}"


def _pair(value, label: str) -> tuple:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise WorkoutError(f"{label} must be a [low, high] pair.")
    return value[0], value[1]


def _number(value, label: str, low: float, high: float) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise WorkoutError(f"{label} must be a number.")
    if not low <= value <= high:
        raise WorkoutError(f"{label} {value} is outside {low:g}-{high:g}.")
    return float(value)


def _enum(group: dict, key: str, display: int | None = None, prefix: str = "") -> dict:
    ident = group[key]
    names = {"step": ("stepTypeId", "stepTypeKey"), "condition": ("conditionTypeId", "conditionTypeKey"),
             "target": ("workoutTargetTypeId", "workoutTargetTypeKey"), "stroke": ("strokeTypeId", "strokeTypeKey"),
             "drill": ("drillTypeId", "drillTypeKey"), "equipment": ("equipmentTypeId", "equipmentTypeKey")}[prefix]
    result = {names[0]: ident, names[1]: key, "displayOrder": display if display is not None else ident}
    if prefix == "condition":
        result["displayable"] = True
    return result


# --- building ---------------------------------------------------------------------------

def _duration(step: dict, sport: str, pool_length: float | None, rest: bool) -> tuple[str, float, dict | None]:
    duration = step.get("duration")
    if not isinstance(duration, dict) or len(duration) != 1:
        raise WorkoutError("Each step needs duration with exactly one of time_s, distance_m, lap_button"
                           + (" or reps" if sport == "strength_training" else "") + ".")
    (kind, value), = duration.items()
    if kind == "lap_button":
        if value is not True:
            raise WorkoutError("duration.lap_button must be true.")
        return "lap.button", 0.0, None
    if kind == "time_s":
        seconds = _number(value, "duration.time_s", 1, 6 * 3600)
        return ("fixed.rest" if rest and sport == "swimming" else "time"), seconds, None
    if kind == "distance_m":
        if sport == "strength_training":
            raise WorkoutError("Strength steps use time_s, reps or lap_button, not distance.")
        meters = _number(value, "duration.distance_m", 1, 200_000)
        if sport == "swimming":
            if rest:
                raise WorkoutError("Swim rest steps use time_s (fixed rest) or lap_button, not distance.")
            if pool_length and abs(meters / pool_length - round(meters / pool_length)) > 1e-6:
                raise WorkoutError(f"Swim distance {meters:g} m is not a whole number of {pool_length:g} m lengths.")
            return "distance", meters, UNITS["meter"]
        return "distance", meters, UNITS["kilometer"]
    if kind == "reps":
        if sport != "strength_training":
            raise WorkoutError("duration.reps is only for strength workouts.")
        return "reps", _number(value, "duration.reps", 1, 1000), None
    raise WorkoutError(f"Unknown duration {kind!r}; use time_s, distance_m, lap_button or reps.")


def _target(step: dict, sport: str) -> dict:
    target = step.get("target")
    if target in (None, "none", {}) or (isinstance(target, dict) and "none" in target):
        return {"targetType": _enum(TARGETS, "no.target", prefix="target")}
    if not isinstance(target, dict) or len(target) != 1:
        raise WorkoutError("target must be one of pace_min_per_km, hr_zone, hr_bpm, power_w, cadence or none.")
    (kind, value), = target.items()
    if kind == "pace_min_per_km":
        if sport != "running":
            raise WorkoutError("Pace targets are for running; use hr_zone or power_w for other sports.")
        low, high = sorted(parse_pace(v) for v in _pair(value, "target.pace_min_per_km"))
        # Garmin stores the faster speed first: 3:20-3:40/km -> 5.0, 4.545 m/s.
        return {"targetType": _enum(TARGETS, "pace.zone", prefix="target"),
                "targetValueOne": speed_from_pace(low), "targetValueTwo": speed_from_pace(high)}
    if kind == "hr_zone":
        zone = _number(value, "target.hr_zone", 1, 5)
        if zone != int(zone):
            raise WorkoutError("target.hr_zone must be a whole zone number 1-5.")
        return {"targetType": _enum(TARGETS, "heart.rate.zone", prefix="target"), "zoneNumber": int(zone)}
    if kind == "hr_bpm":
        low, high = sorted(_number(v, "target.hr_bpm", 40, 230) for v in _pair(value, "target.hr_bpm"))
        return {"targetType": _enum(TARGETS, "heart.rate.zone", prefix="target"),
                "targetValueOne": low, "targetValueTwo": high}
    if kind == "power_w":
        if sport != "cycling":
            raise WorkoutError("Power targets are for cycling.")
        low, high = sorted(_number(v, "target.power_w", 20, 2000) for v in _pair(value, "target.power_w"))
        return {"targetType": _enum(TARGETS, "power.zone", prefix="target"),
                "targetValueOne": low, "targetValueTwo": high}
    if kind == "cadence":
        low, high = sorted(_number(v, "target.cadence", 20, 250) for v in _pair(value, "target.cadence"))
        return {"targetType": _enum(TARGETS, "cadence", prefix="target"),
                "targetValueOne": low, "targetValueTwo": high}
    raise WorkoutError(f"Unknown target {kind!r}.")


def _swim(step: dict, rest: bool) -> dict:
    swim = step.get("swim") or {}
    if not isinstance(swim, dict):
        raise WorkoutError("swim must be an object like {stroke: 'free'}.")
    if rest:
        return {}
    stroke = str(swim.get("stroke") or "any_stroke").lower()
    stroke = STROKE_ALIASES.get(stroke, stroke)
    if stroke not in STROKES:
        raise WorkoutError(f"Unknown stroke {stroke!r}; use one of {', '.join(STROKES)}.")
    result = {"strokeType": _enum(STROKES, stroke, prefix="stroke")}
    if swim.get("drill"):
        drill = str(swim["drill"]).lower()
        if drill not in DRILLS:
            raise WorkoutError(f"Unknown drill {drill!r}; use kick, pull or drill.")
        result["drillType"] = _enum(DRILLS, drill, prefix="drill")
    if swim.get("equipment"):
        equipment = str(swim["equipment"]).lower()
        if equipment not in EQUIPMENT:
            raise WorkoutError(f"Unknown equipment {equipment!r}; use one of {', '.join(EQUIPMENT)}.")
        result["equipmentType"] = _enum(EQUIPMENT, equipment, prefix="equipment")
    return result


class _Builder:
    def __init__(self, sport: str, pool_length: float | None):
        self.sport, self.pool_length = sport, pool_length
        self.order = 0
        self.groups = 0

    def steps(self, steps, child: int | None = None) -> list[dict]:
        if not isinstance(steps, list) or not steps:
            raise WorkoutError("steps must be a non-empty list.")
        return [self.step(step, child) for step in steps]

    def step(self, step, child: int | None) -> dict:
        if not isinstance(step, dict):
            raise WorkoutError("Each step must be an object.")
        kind = str(step.get("type") or "").lower()
        self.order += 1
        if kind == "repeat":
            if child is not None:
                raise WorkoutError("Repeat blocks cannot be nested.")
            repeat = step.get("repeat")
            if not isinstance(repeat, dict):
                raise WorkoutError("A repeat step needs repeat: {count, steps}.")
            count = _number(repeat.get("count"), "repeat.count", 1, 99)
            if count != int(count):
                raise WorkoutError("repeat.count must be a whole number.")
            self.groups += 1
            group = {"type": "RepeatGroupDTO", "stepOrder": self.order,
                     "stepType": _enum(STEP_TYPES, "repeat", prefix="step"), "childStepId": self.groups,
                     "numberOfIterations": int(count), "smartRepeat": False,
                     "endCondition": _enum(CONDITIONS, "iterations", prefix="condition"),
                     "endConditionValue": float(int(count))}
            if self.sport == "swimming":
                group["skipLastRestStep"] = bool(repeat.get("skip_last_rest", False))
            group["workoutSteps"] = self.steps(repeat.get("steps"), self.groups)
            return group
        if kind not in EXECUTABLE:
            raise WorkoutError(f"Unknown step type {kind!r}; use warmup, interval, recovery, rest, cooldown or repeat.")
        if self.sport == "swimming" and kind == "interval":
            kind = "main"  # Garmin's own swim workouts use "main" for swim sets
        rest = kind == "rest"
        condition, value, unit = _duration(step, self.sport, self.pool_length, rest)
        result = {"type": "ExecutableStepDTO", "stepOrder": self.order,
                  "stepType": _enum(STEP_TYPES, kind, prefix="step"), "childStepId": child,
                  "endCondition": _enum(CONDITIONS, condition, prefix="condition"), "endConditionValue": value}
        if unit:
            result["preferredEndConditionUnit"] = unit
        if rest:
            if step.get("target") not in (None, "none", {}):
                raise WorkoutError("Rest steps cannot have a target.")
        else:
            result.update(_target(step, self.sport))
        if self.sport == "swimming":
            result.update(_swim(step, rest))
        notes = step.get("notes")
        if notes:
            result["description"] = str(notes)[:512]
        return result


def build_workout(sport: str, name: str, steps: list, description: str | None = None,
                  pool_length_m: float | None = None) -> dict:
    """The Garmin Connect JSON for one workout. Raises WorkoutError with a fixable message."""
    key = sport_key(sport)
    pool = None
    if key == "swimming":
        pool = _number(pool_length_m if pool_length_m is not None else 25, "pool_length_m", 10, 100)
    elif pool_length_m is not None:
        raise WorkoutError("pool_length_m is only for lap_swimming workouts.")
    ident, display = SPORTS[key]
    sport_type = {"sportTypeId": ident, "sportTypeKey": key, "displayOrder": display}
    workout = {
        "workoutName": with_prefix(name),
        "description": str(description or "")[:1024] or None,
        "sportType": sport_type,
        "workoutSegments": [{"segmentOrder": 1, "sportType": sport_type,
                             "workoutSteps": _Builder(key, pool).steps(steps)}],
    }
    if pool:
        workout["poolLength"] = pool
        workout["poolLengthUnit"] = UNITS["meter"]
    return workout


# --- reading workouts back --------------------------------------------------------------

def _key(value, field):
    return value.get(field) if isinstance(value, dict) else None


def canonical_steps(workout: dict) -> list[dict]:
    """Every step in order, reduced to what the watch uses, so a sent workout and the one
    Garmin returns (with ids and nulls added) can be compared."""
    rows = []

    def visit(steps, depth):
        for step in steps or []:
            row = {"depth": depth, "step": _key(step.get("stepType"), "stepTypeKey"),
                   "end": _key(step.get("endCondition"), "conditionTypeKey")}
            if step.get("type") == "RepeatGroupDTO":
                row["count"] = step.get("numberOfIterations")
                rows.append(row)
                visit(step.get("workoutSteps"), depth + 1)
                continue
            if row["end"] != "lap.button":
                row["value"] = round(float(step.get("endConditionValue") or 0), 1)
            target = _key(step.get("targetType"), "workoutTargetTypeKey") or "no.target"
            row["target"] = target
            if target != "no.target":
                row["zone"] = step.get("zoneNumber") or None
                for field in ("targetValueOne", "targetValueTwo"):
                    value = step.get(field)
                    row[field] = round(float(value), 3) if value is not None else None
            for field, key in (("strokeType", "strokeTypeKey"), ("drillType", "drillTypeKey"),
                               ("equipmentType", "equipmentTypeKey")):
                if _key(step.get(field), key):
                    row[field] = step[field][key]
            rows.append(row)

    for segment in workout.get("workoutSegments") or []:
        visit(segment.get("workoutSteps"), 0)
    return rows


def compare(sent: dict, stored: dict) -> list[str]:
    """Differences between what was uploaded and what Garmin stored; empty when they match."""
    problems = []
    if stored.get("workoutName") != sent.get("workoutName"):
        problems.append(f"name: sent {sent.get('workoutName')!r}, Garmin has {stored.get('workoutName')!r}")
    if _key(stored.get("sportType"), "sportTypeKey") != _key(sent.get("sportType"), "sportTypeKey"):
        problems.append("sport differs")
    if sent.get("poolLength") and float(stored.get("poolLength") or 0) != float(sent["poolLength"]):
        problems.append(f"pool length: sent {sent['poolLength']}, Garmin has {stored.get('poolLength')}")
    mine, theirs = canonical_steps(sent), canonical_steps(stored)
    if len(mine) != len(theirs):
        problems.append(f"step count: sent {len(mine)}, Garmin has {len(theirs)}")
    for index, (a, b) in enumerate(zip(mine, theirs), start=1):
        if a != b:
            fields = sorted(k for k in set(a) | set(b) if a.get(k) != b.get(k))
            problems.append(f"step {index}: " + ", ".join(f"{k} sent {a.get(k)!r} got {b.get(k)!r}" for k in fields))
    return problems


def payload_hash(workout: dict) -> str:
    return hashlib.sha256(json.dumps(workout, sort_keys=True).encode()).hexdigest()


# --- human summary and estimates --------------------------------------------------------

def _describe_target(step: dict) -> str:
    target = _key(step.get("targetType"), "workoutTargetTypeKey")
    one, two = step.get("targetValueOne"), step.get("targetValueTwo")
    if target == "pace.zone":
        return f" @ {format_pace(1000 / one)}-{format_pace(1000 / two)}/km"
    if target == "heart.rate.zone":
        return f" @ HR zone {step['zoneNumber']}" if step.get("zoneNumber") else f" @ HR {one:g}-{two:g} bpm"
    if target == "power.zone":
        return f" @ {one:g}-{two:g} W"
    if target == "cadence":
        return f" @ {one:g}-{two:g} rpm/spm"
    return ""


def _length(step: dict) -> str:
    end = _key(step.get("endCondition"), "conditionTypeKey")
    value = step.get("endConditionValue") or 0
    if end == "lap.button":
        return "until lap button"
    if end in ("time", "fixed.rest"):
        return format_duration(value)
    if end == "reps":
        return f"{value:g} reps"
    return f"{value / 1000:g} km" if value >= 1000 else f"{value:g} m"


def _describe_step(step: dict) -> str:
    kind = _key(step.get("stepType"), "stepTypeKey")
    text = f"{kind.capitalize()} {_length(step)}{_describe_target(step)}"
    stroke = _key(step.get("strokeType"), "strokeTypeKey")
    if stroke:
        text += f" {stroke.replace('_', ' ')}"
    for field, key in (("drillType", "drillTypeKey"), ("equipmentType", "equipmentTypeKey")):
        if _key(step.get(field), key):
            text += f" ({step[field][key].replace('_', ' ')})"
    if step.get("description"):
        text += f" - {step['description']}"
    return text


def summary(workout: dict) -> str:
    sport = _key(workout.get("sportType"), "sportTypeKey")
    lines = [f"{workout['workoutName']} ({sport}"
             + (f", {workout['poolLength']:g} m pool" if workout.get("poolLength") else "") + ")"]
    for segment in workout["workoutSegments"]:
        for step in segment["workoutSteps"]:
            if step["type"] == "RepeatGroupDTO":
                skip = ", skip last rest" if step.get("skipLastRestStep") else ""
                lines.append(f"  {step['numberOfIterations']}x{skip}:")
                lines.extend("    " + _describe_step(child) for child in step["workoutSteps"])
            else:
                lines.append("  " + _describe_step(step))
    estimate = estimate_totals(workout)
    totals = [part for part in (
        f"{format_duration(estimate['duration_s'])}" if estimate["duration_s"] and not estimate["duration_partial"] else None,
        f"{estimate['distance_m'] / 1000:g} km" if estimate["distance_m"] and not estimate["distance_partial"] else None)
        if part]
    if totals:
        lines.append("  Total: about " + ", ".join(totals))
    return "\n".join(lines)


def _step_row(step: dict) -> dict:
    end = _key(step.get("endCondition"), "conditionTypeKey")
    value = float(step.get("endConditionValue") or 0)
    target = _key(step.get("targetType"), "workoutTargetTypeKey")
    seconds = None
    if end in ("time", "fixed.rest"):
        seconds = value
    elif end == "distance" and target == "pace.zone":
        seconds = value / ((step["targetValueOne"] + step["targetValueTwo"]) / 2)
    row = {"kind": _key(step.get("stepType"), "stepTypeKey"), "end": end, "value": value,
           "length": _length(step), "target": _describe_target(step).removeprefix(" @ ") or None,
           "notes": step.get("description") or None,
           "seconds": round(seconds) if seconds else None, "hr_zone": step.get("zoneNumber") or None}
    if target == "pace.zone":
        row["pace_s_per_km"] = sorted(round(1000 / step[f]) for f in ("targetValueOne", "targetValueTwo"))
    for field, key, name in (("strokeType", "strokeTypeKey", "stroke"), ("drillType", "drillTypeKey", "drill"),
                             ("equipmentType", "equipmentTypeKey", "equipment")):
        if _key(step.get(field), key):
            row[name] = step[field][key].replace("_", " ")
    return row


def structured_steps(workout: dict) -> list[dict]:
    """The steps as plain rows for an app to draw; a repeat holds its child steps."""
    rows = []
    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                rows.append({"kind": "repeat", "count": step.get("numberOfIterations") or 1,
                             "skip_last_rest": bool(step.get("skipLastRestStep")),
                             "steps": [_step_row(child) for child in step.get("workoutSteps") or []]})
            else:
                rows.append(_step_row(step))
    return rows


def category(workout: dict, threshold_s_per_km: float | None = None) -> str:
    """easy, long or quality for runs; swim, bike, strength or other for the rest."""
    sport = _key(workout.get("sportType"), "sportTypeKey")
    if sport == "running":
        if is_hard(workout, threshold_s_per_km):
            return "quality"
        return "long" if estimate_totals(workout)["duration_s"] >= 3600 else "easy"
    return {"swimming": "swim", "cycling": "bike", "strength_training": "strength"}.get(sport, "other")


def estimate_totals(workout: dict, default_speed_ms: float | None = None) -> dict:
    """Rough duration and distance. Time steps at a pace target (or default_speed_ms) add
    distance; distance steps add time the same way. partial=True when a step could not be
    estimated (lap button, reps, no speed), with duration_partial/distance_partial saying which."""
    totals = {"duration_s": 0.0, "distance_m": 0.0, "duration_partial": False, "distance_partial": False}
    swim = _key(workout.get("sportType"), "sportTypeKey") == "swimming"

    def speed(step):
        if _key(step.get("targetType"), "workoutTargetTypeKey") == "pace.zone":
            return (step["targetValueOne"] + step["targetValueTwo"]) / 2
        return default_speed_ms

    def add(step, times):
        end = _key(step.get("endCondition"), "conditionTypeKey")
        value = float(step.get("endConditionValue") or 0)
        rest = _key(step.get("stepType"), "stepTypeKey") == "rest"
        if end in ("time", "fixed.rest"):
            totals["duration_s"] += value * times
            if not rest:
                if speed(step):
                    totals["distance_m"] += value * speed(step) * times
                else:
                    totals["distance_partial"] = True
        elif end == "distance":
            totals["distance_m"] += value * times
            if speed(step) and not swim:
                totals["duration_s"] += value / speed(step) * times
            else:
                totals["duration_partial"] = True
        else:
            totals["duration_partial"] = True
            totals["distance_partial"] = totals["distance_partial"] or not rest

    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                for child in step.get("workoutSteps") or []:
                    add(child, step.get("numberOfIterations") or 1)
            else:
                add(step, 1)
    totals["duration_s"] = round(totals["duration_s"])
    totals["distance_m"] = round(totals["distance_m"])
    totals["partial"] = totals["duration_partial"] or totals["distance_partial"]
    return totals


# --- training paces (Daniels/Gilbert VDOT) ----------------------------------------------

# Fractions of VDOT, calibrated against Daniels' published VDOT 50 paces
# (E ~5:05-5:35, M 4:31, T 4:15, I 3:55, R 3:41 per km).
PACE_FRACTIONS = {"easy": (0.62, 0.70), "marathon": (0.81, 0.81), "threshold": (0.88, 0.88),
                  "interval": (0.975, 0.975), "repetition": (1.05, 1.05)}


def vdot(distance_m: float, seconds: float) -> float:
    minutes = seconds / 60
    velocity = distance_m / minutes
    vo2 = -4.60 + 0.182258 * velocity + 0.000104 * velocity ** 2
    fraction = 0.8 + 0.1894393 * math.exp(-0.012778 * minutes) + 0.2989558 * math.exp(-0.1932605 * minutes)
    return vo2 / fraction


def _pace_for(fraction: float, value: float) -> float:
    a, b, c = 0.000104, 0.182258, -4.60 - fraction * value
    meters_per_min = (-b + math.sqrt(b * b - 4 * a * c)) / (2 * a)
    return 60000 / meters_per_min  # seconds per km


def training_paces(distance_m: float, seconds: float) -> dict:
    """Daniels-style training paces (min:sec per km) from one race result."""
    value = vdot(distance_m, seconds)
    paces = {}
    for name, (fast, slow) in PACE_FRACTIONS.items():
        quick, easy = _pace_for(slow, value), _pace_for(fast, value)
        center = (quick + easy) / 2
        if name != "easy":  # single-point paces get a +/-5 s/km working range
            quick, easy = center - 5, center + 5
        paces[name] = {"pace_min_per_km": [format_pace(quick), format_pace(easy)],
                       "seconds_per_km": [round(quick), round(easy)]}
    return {"vdot": round(value, 1), "paces": paces}


# --- plan checks (warnings, never blocks) -----------------------------------------------

def _steps(workout: dict):
    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                yield from step.get("workoutSteps") or []
            else:
                yield step


def is_hard(workout: dict, threshold_s_per_km: float | None = None) -> bool:
    """Any work step at HR zone 4+, or (running) faster than marathon-ish pace."""
    for step in _steps(workout):
        if _key(step.get("stepType"), "stepTypeKey") in ("warmup", "cooldown", "rest", "recovery"):
            continue
        if (step.get("zoneNumber") or 0) >= 4:
            return True
        if threshold_s_per_km and _key(step.get("targetType"), "workoutTargetTypeKey") == "pace.zone":
            if 1000 / step["targetValueOne"] <= threshold_s_per_km + 15:
                return True
    return False


def _step_hard(step: dict, threshold_s_per_km: float | None) -> bool:
    if (step.get("zoneNumber") or 0) >= 4:
        return True
    return bool(threshold_s_per_km and _key(step.get("targetType"), "workoutTargetTypeKey") == "pace.zone"
                and 1000 / step["targetValueOne"] <= threshold_s_per_km + 15)


def _hard_seconds(workout: dict, threshold_s_per_km: float | None, default_speed_ms: float | None) -> float:
    """Time in work steps at HR zone 4+ or near-threshold pace (repeats counted)."""
    total = 0.0

    def add(step, times):
        nonlocal total
        if _key(step.get("stepType"), "stepTypeKey") in ("warmup", "cooldown", "rest", "recovery"):
            return
        if not _step_hard(step, threshold_s_per_km):
            return
        end = _key(step.get("endCondition"), "conditionTypeKey")
        value = float(step.get("endConditionValue") or 0)
        if end in ("time", "fixed.rest"):
            total += value * times
        elif end == "distance":
            speed = ((step["targetValueOne"] + step["targetValueTwo"]) / 2
                     if _key(step.get("targetType"), "workoutTargetTypeKey") == "pace.zone" else default_speed_ms)
            if speed:
                total += value / speed * times

    for segment in workout.get("workoutSegments") or []:
        for step in segment.get("workoutSteps") or []:
            if step.get("type") == "RepeatGroupDTO":
                for child in step.get("workoutSteps") or []:
                    add(child, step.get("numberOfIterations") or 1)
            else:
                add(step, 1)
    return total


VALIDATOR_SPORTS = {"running": "run", "cycling": "bike", "swimming": "swim", "strength_training": "gym"}
# "test" is left out on purpose: drill tests (kick, buoy) are not races; set session.all_out
# for a real CSS or 5K test.
HARD_SWIM = re.compile(r"\b(css|threshold|race pace|fast|sprint|test)\b", re.IGNORECASE)
ALL_OUT = re.compile(r"\b(race|time trial|tt|all[- ]out)\b(?! pace)", re.IGNORECASE)
NAME_MINUTES = re.compile(r"(\d+)\s*min\b", re.IGNORECASE)
SWIM_S_PER_100M = 120  # default pace for swim distance steps


def to_session(day: str, workout: dict, paces: dict | None = None, extra: dict | None = None,
               swim_s_per_100m: float = SWIM_S_PER_100M, easy_s_per_km: float | None = None) -> dict:
    """One Garmin payload as a coaching-validator session (see garmin_ai/coaching/validator.py).

    Minutes and run km are estimated from the steps; run steps without a pace use
    easy_s_per_km (the athlete's own recent easy pace) or else the middle of the VDOT easy
    range, swim distance steps use swim_s_per_100m. When steps have no duration
    (reps, lap button), a "40min" in the name is used instead. extra overrides or adds fields
    (slot, kind, intensity, all_out, ...)."""
    pace_table = (paces or {}).get("paces") or {}
    easy = pace_table.get("easy", {}).get("seconds_per_km")
    threshold = (pace_table.get("threshold", {}).get("seconds_per_km") or [None])[0]
    easy_speed = (1000 / easy_s_per_km if easy_s_per_km else
                  1000 / ((easy[0] + easy[1]) / 2) if easy else None)
    sport_type = _key(workout.get("sportType"), "sportTypeKey")
    sport = VALIDATOR_SPORTS.get(sport_type, "other")
    totals = estimate_totals(workout, easy_speed if sport == "run" else None)
    name = str(workout.get("workoutName") or "").removeprefix(PREFIX)
    hard = is_hard(workout, threshold)
    seconds = totals["duration_s"]
    if sport == "swim":  # estimate_totals cannot time swim distance steps
        seconds += totals["distance_m"] / 100 * swim_s_per_100m
        totals["duration_partial"] = False
    named = NAME_MINUTES.search(name)
    if named and totals["duration_partial"]:
        seconds = max(seconds, int(named.group(1)) * 60)
    session = {"date": day, "sport": sport, "name": name,
               "minutes": round(seconds / 60), "intensity": "quality" if hard else "easy"}
    if hard:
        session["hard_minutes"] = round(_hard_seconds(workout, threshold, easy_speed) / 60)
    if sport == "run":
        session["km"] = round(totals["distance_m"] / 1000, 1)
        if totals["partial"]:
            session["estimate_partial"] = True
        if not hard and any(_key(st.get("targetType"), "workoutTargetTypeKey") == "pace.zone"
                            and _key(st.get("stepType"), "stepTypeKey") not in ("warmup", "cooldown", "recovery")
                            for st in _steps(workout)):
            session["pace_target"] = True
        if "long" in name.lower():
            session["kind"] = "long"
    if sport == "swim" and HARD_SWIM.search(name) and not re.search(r"\b(kick|buoy|drill)", name, re.IGNORECASE):
        session["hard"] = True  # a hard swim: spaced from run quality, not counted as run quality
    if ALL_OUT.search(name) and "easy" not in name.lower():
        session.update(intensity="race", all_out=True)
    session.update(extra or {})
    return session


def mark_long_runs(sessions: list[dict]) -> None:
    """Without a run named "long", the week's longest run of 60+ minutes is the long run."""
    runs = [s for s in sessions if s["sport"] == "run"]
    if not runs or any(s.get("kind") == "long" for s in runs):
        return
    longest = max(runs, key=lambda s: s.get("km") or 0)
    if longest["minutes"] >= 60 and longest.get("intensity") == "easy":
        longest["kind"] = "long"


def pace_warnings(items: list[dict], paces: dict | None = None) -> list[str]:
    """Easy steps (warm-up, cool-down, recovery, easy or long runs) paced faster than marathon pace.

    items: [{"date": "YYYY-MM-DD", "workout": <Garmin payload>}]. The other weekly checks
    (volume ramp, long run, quality spacing...) are in garmin_ai/coaching/validator.py."""
    warnings = []
    pace_table = (paces or {}).get("paces") or {}
    easy = pace_table.get("easy", {}).get("seconds_per_km")
    marathon = pace_table.get("marathon", {}).get("seconds_per_km")
    if not marathon or not easy:
        return warnings
    for item in items:
        day, workout = item["date"], item["workout"]
        name = workout.get("workoutName", "")
        for step in _steps(workout):
            if _key(step.get("targetType"), "workoutTargetTypeKey") != "pace.zone":
                continue
            fast = 1000 / step["targetValueOne"]
            kind = _key(step.get("stepType"), "stepTypeKey")
            easy_like = kind in ("warmup", "cooldown", "recovery") or "easy" in name.lower() or "long" in name.lower()
            if easy_like and fast < marathon[0] - 5:
                warnings.append(f"{day} {name}: a {kind} step at {format_pace(fast)}/km is faster than marathon "
                                f"pace ({format_pace(marathon[0])}/km); easy running is ~{format_pace(easy[0])}-"
                                f"{format_pace(easy[1])}/km.")
                break
    return warnings
