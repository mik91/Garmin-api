"""Turn recent recovery data into green / yellow / red with fixed consequences.

daily = [{"date": "YYYY-MM-DD", "hrv_overnight", "resting_hr", "total_sleep_s", "training_readiness"}, ...]
Matches the columns of the Garmin Insights `daily_stats` and `sleep` tables (join on date).
"""
from __future__ import annotations

from datetime import date, timedelta
from statistics import mean, median

from .validator import load_rules

ACTIONS = {
    "green": ["Plan as scheduled."],
    "yellow": [
        "Downgrade the next quality session to easy (or shorten it by about a third).",
        "Cut planned weekly volume by about 10%. Keep the long run unless it is the day after poor sleep.",
    ],
    "red": [
        "No quality sessions this week.",
        "Cut planned weekly volume by 20-30%; easy HR-capped sessions only.",
        "If there is pain that changes how you move or illness below the neck: no running; swim or bike only if pain-free.",
    ],
    "unknown": ["Not enough recovery data; plan conservatively and say so."],
}


def _vals(rows, key, lo, hi):
    out = []
    for r in rows:
        v = r.get(key)
        if v is None:
            continue
        if lo <= date.fromisoformat(r["date"]) <= hi:
            out.append(float(v))
    return out


def _level(value, thresholds, higher_is_worse):
    """Return (0|1|2) points for yellow/red thresholds."""
    if value is None:
        return 0
    worse = (lambda a, b: a >= b) if higher_is_worse else (lambda a, b: a <= b)
    if worse(value, thresholds["red"]):
        return 2
    if worse(value, thresholds["yellow"]):
        return 1
    return 0


def assess_readiness(daily: list[dict], as_of: str, reported: list[str] | None = None,
                     rules: dict | None = None) -> dict:
    cfg = (rules or load_rules())["readiness"]
    end = date.fromisoformat(as_of)
    r_lo, b_hi = end - timedelta(days=cfg["recent_days"] - 1), end - timedelta(days=cfg["recent_days"])
    b_lo = b_hi - timedelta(days=cfg["baseline_days"] - 1)

    reported = [x.lower() for x in (reported or [])]
    forced = [x for x in reported if x in cfg["reported_red"]]

    signals, points = [], 0

    def add(name, value, pts, note):
        nonlocal points
        points += pts
        signals.append({"name": name, "value": value, "points": pts, "note": note})

    # HRV: 7-day median vs prior 21-day median, in %
    hrv_now, hrv_base = _vals(daily, "hrv_overnight", r_lo, end), _vals(daily, "hrv_overnight", b_lo, b_hi)
    if len(hrv_now) >= cfg["min_recent_days"] and len(hrv_base) >= cfg["min_baseline_days"]:
        change = round((median(hrv_now) / median(hrv_base) - 1) * 100, 1)
        add("hrv_change_pct", change, _level(-change, {"yellow": cfg["hrv_drop_pct"]["yellow"],
                                                       "red": cfg["hrv_drop_pct"]["red"]}, True),
            "7-day median vs prior 21-day median")
    # Resting HR: same windows, in bpm
    rhr_now, rhr_base = _vals(daily, "resting_hr", r_lo, end), _vals(daily, "resting_hr", b_lo, b_hi)
    if len(rhr_now) >= cfg["min_recent_days"] and len(rhr_base) >= cfg["min_baseline_days"]:
        delta = round(median(rhr_now) - median(rhr_base), 1)
        add("resting_hr_delta_bpm", delta, _level(delta, cfg["resting_hr_rise_bpm"], True),
            "7-day median vs prior 21-day median")
    # Sleep: last 3 nights
    sleep = [v / 3600 for v in _vals(daily, "total_sleep_s", end - timedelta(days=2), end)]
    if len(sleep) >= 2:
        avg = round(mean(sleep), 2)
        add("sleep_hours_last3", avg, _level(avg, cfg["sleep_hours_last3_avg"], False), "average of last 3 nights")
    # Garmin training readiness: latest value
    tr = _vals(daily, "training_readiness", end - timedelta(days=1), end)
    if tr:
        add("training_readiness", tr[-1], _level(tr[-1], cfg["training_readiness_below"], False), "latest value")

    if forced:
        status = "red"
    elif not signals:
        status = "unknown"
    elif points >= cfg["red_points"]:
        status = "red"
    elif points >= cfg["yellow_points"]:
        status = "yellow"
    else:
        status = "green"

    return {"status": status, "points": points, "signals": signals, "reported": reported,
            "actions": ACTIONS[status]}
