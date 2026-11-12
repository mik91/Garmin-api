"""Aerobic efficiency of easy running, corrected for speed, hills, warm-up and heat.

Ratios like metres per heartbeat or stride length change with how fast you choose to run,
so this compares like with like instead:

  HR = level[window] + slope * speed + heat * max(0, dew point - 10 C)

fitted on steady per-km laps of easy and long runs. slope and heat are shared by all windows
(they describe the athlete, and need many laps to estimate); each 3-week window gets its own
level, which is what fitness moves. From it: pace at a reference HR (e.g. 135 bpm) in cool
conditions, with a margin of error. A change only counts when it is bigger than the margins.

Laps used: from 10 min into the run to 60 min (no warm-up lag, no long-run drift), at least
800 m, grade-adjusted speed, not much faster than the laps around it (strides, pickups), and
from runs Garmin labels recovery / aerobic base (or unlabelled) or named easy / long / recovery.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from statistics import mean

from garmin_ai import workouts

EASY_LABELS = ("RECOVERY", "AEROBIC_BASE", None)
EASY_NAME = re.compile(r"\b(easy|long|recovery|base)\b", re.IGNORECASE)
WINDOW_WEEKS = 3
MIN_LAPS_WINDOW = 8
MIN_LAPS_TOTAL = 40
HEAT_FROM_C = 10.0  # dew point above which heat raises HR


def _f_to_c(value):
    return None if value is None else (float(value) - 32) * 5 / 9


def laps(conn, first: str, last: str) -> list[dict]:
    rows = conn.execute(
        "SELECT activity_id, substr(start_time, 1, 10) AS day, splits_json, weather_json, raw_json "
        "FROM activities WHERE type LIKE '%running%' AND splits_json IS NOT NULL "
        "AND substr(start_time, 1, 10) BETWEEN ? AND ?", (first, last)).fetchall()
    out = []
    for row in rows:
        try:
            raw = json.loads(row["raw_json"] or "{}")
            splits = json.loads(row["splits_json"] or "{}")
            weather = json.loads(row["weather_json"] or "{}") or {}
        except ValueError:
            continue
        if raw.get("trainingEffectLabel") not in EASY_LABELS and not EASY_NAME.search(raw.get("activityName") or ""):
            continue
        items = splits.get("lapDTOs") if isinstance(splits, dict) else None
        if not items:
            continue
        dew = _f_to_c(weather.get("dewPoint"))
        elapsed, speeds = 0.0, []
        candidates = []
        for lap in items:
            seconds = lap.get("movingDuration") or lap.get("duration") or 0
            start = elapsed
            elapsed += seconds
            speed = lap.get("avgGradeAdjustedSpeed") or lap.get("averageMovingSpeed") or lap.get("averageSpeed")
            hr = lap.get("averageHR")
            if not speed or not hr or (lap.get("distance") or 0) < 800:
                speeds.append(None)
                continue
            speeds.append(speed)
            if 600 <= start and elapsed <= 3600:
                candidates.append((len(speeds) - 1, speed, hr))
        known = [s for s in speeds if s]
        typical = sorted(known)[len(known) // 2] if known else None
        for index, speed, hr in candidates:
            if typical and speed > typical * 1.10:  # a pickup or strides lap
                continue
            out.append({"day": row["day"], "activity_id": row["activity_id"], "speed": speed, "hr": hr,
                        "heat": max(0.0, (dew if dew is not None else HEAT_FROM_C) - HEAT_FROM_C),
                        "dew_point_c": None if dew is None else round(dew, 1)})
    return out


def _solve2(a11, a12, a22, b1, b2):
    det = a11 * a22 - a12 * a12
    if abs(det) < 1e-12:
        return None
    return (b1 * a22 - b2 * a12) / det, (a11 * b2 - a12 * b1) / det


def fit(points: list[dict], window_of) -> dict | None:
    """Fixed-effects least squares: one level per window, shared slope and heat effect."""
    groups: dict[str, list[dict]] = {}
    for p in points:
        groups.setdefault(window_of(p["day"]), []).append(p)
    groups = {k: v for k, v in groups.items() if len(v) >= 3}
    if sum(len(v) for v in groups.values()) < MIN_LAPS_TOTAL:
        return None
    sxx = sxh = shh = sxy = shy = 0.0
    for members in groups.values():
        mx, mh, my = (mean(p[k] for p in members) for k in ("speed", "heat", "hr"))
        for p in members:
            x, h, y = p["speed"] - mx, p["heat"] - mh, p["hr"] - my
            sxx, sxh, shh, sxy, shy = sxx + x * x, sxh + x * h, shh + h * h, sxy + x * y, shy + h * y
    heat_varies = shh > 1.0
    if heat_varies:
        solved = _solve2(sxx, sxh, shh, sxy, shy)
        if not solved:
            return None
        slope, heat = solved
    else:
        slope, heat = (sxy / sxx if sxx else 0.0), 0.0
    if slope <= 0:
        return None
    residuals, levels = [], {}
    for key, members in groups.items():
        level = mean(p["hr"] - slope * p["speed"] - heat * p["heat"] for p in members)
        levels[key] = {"level": level, "n": len(members), "days": sorted({p["day"] for p in members})}
        residuals += [p["hr"] - level - slope * p["speed"] - heat * p["heat"] for p in members]
    dof = max(1, len(residuals) - len(groups) - (2 if heat_varies else 1))
    sigma = (sum(r * r for r in residuals) / dof) ** 0.5
    return {"slope": slope, "heat": heat, "sigma": sigma, "levels": levels, "n": len(residuals)}


def trend(conn, weeks: int = 24, ref_hr: float | None = None, today: date | None = None) -> dict:
    """Pace at a reference HR in cool weather, per 3-week window, with a margin of error."""
    today = today or date.today()
    first = today - timedelta(days=today.weekday()) - timedelta(days=7 * (max(6, int(weeks)) - 1))
    points = laps(conn, first.isoformat(), today.isoformat())

    def window_of(day: str) -> str:
        offset = (date.fromisoformat(day) - first).days // (7 * WINDOW_WEEKS)
        return (first + timedelta(days=7 * WINDOW_WEEKS * offset)).isoformat()

    model = fit(points, window_of)
    if not model:
        return {"available": False, "laps": len(points),
                "note": f"Needs at least {MIN_LAPS_TOTAL} steady easy laps with heart rate; laps are fetched "
                        "by the sync a few runs at a time."}
    ref_hr = ref_hr or round(sorted(p["hr"] for p in points)[len(points) // 2])
    windows = []
    for key in sorted(model["levels"]):
        info = model["levels"][key]
        if info["n"] < MIN_LAPS_WINDOW:
            continue
        speed = (ref_hr - info["level"]) / model["slope"]
        if speed <= 0:
            continue
        margin = 2 * model["sigma"] / info["n"] ** 0.5 / model["slope"]  # ~95% on speed
        pace, low, high = 1000 / speed, 1000 / (speed + margin), 1000 / max(0.1, speed - margin)
        windows.append({"window_start": key, "laps": info["n"], "runs_days": len(info["days"]),
                        "pace_at_ref_hr": workouts.format_pace(pace), "seconds_per_km": round(pace),
                        "margin_s": round((high - low) / 2)})
    result = {"available": True, "ref_hr": ref_hr, "conditions": f"cool (dew point <= {HEAT_FROM_C:g} C), flat",
              "model": {"bpm_per_m_s": round(model["slope"], 1),
                        "bpm_per_c_dew_point": round(model["heat"], 2), "laps": model["n"],
                        "noise_bpm": round(model["sigma"], 1)},
              "windows": windows}
    if len(windows) >= 3:
        latest, before = windows[-1], windows[-3]  # this window vs two windows (~6 weeks) earlier
        change = latest["seconds_per_km"] - before["seconds_per_km"]
        noise = (latest["margin_s"] ** 2 + before["margin_s"] ** 2) ** 0.5
        significant = abs(change) > max(5, noise)
        result["summary"] = {
            "change_s_per_km": change, "vs_window": before["window_start"], "margin_s": round(noise),
            "significant": significant,
            "reading": ("no clear change" if not significant else
                        "fitter: faster at the same HR" if change < 0 else "slower at the same HR"),
        }
    return result

