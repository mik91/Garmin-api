"""Coaching history kept in garmin.db: a decision log and the athlete profile.

- coach_log: one row per decision (weekly plan, mid-week change, review, note). Uploads made
  with upload_plan are logged automatically, so the record survives even if nobody writes a line.
- coach_profile: named sections (races, fitness anchors, cautions, ...). Every change is also
  written to coach_log with the previous text, so old versions are never lost.

Also builds the validator's `history` and the readiness `daily` rows from the synced data.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta

from garmin_ai.load_summary import QUALITY_LABELS

KINDS = ("plan", "adjustment", "review", "note", "upload", "profile")
READINESS = ("green", "yellow", "red", "unknown")
# No volume section: weekly km, peak and longest run come from the synced activities.
PROFILE_SECTIONS = ("races", "fitness_anchors", "setup_and_constraints", "cautions", "rule_overrides")
MAX_TEXT = 4000


class HistoryError(ValueError):
    pass


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _iso(value, field: str) -> str | None:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError:
        raise HistoryError(f"{field} must be YYYY-MM-DD, got {value!r}.") from None


def _text(value, field: str, required: bool = False) -> str | None:
    if value is None or (isinstance(value, str) and not value.strip()):
        if required:
            raise HistoryError(f"{field} is required.")
        return None
    if not isinstance(value, str):
        value = json.dumps(value, default=str)
    if len(value) > MAX_TEXT:
        raise HistoryError(f"{field} is longer than {MAX_TEXT} characters; keep log lines short.")
    return value.strip()


def _row(row) -> dict:
    entry = dict(row)
    entry["details"] = json.loads(entry.pop("details_json") or "null")
    return entry


# --- decision log -------------------------------------------------------------------------

def add_entry(conn, kind: str, summary: str, week_start=None, readiness=None, planned=None,
              actual=None, details=None) -> dict:
    kind = str(kind or "").lower()
    if kind not in KINDS:
        raise HistoryError(f"kind must be one of {', '.join(KINDS)}.")
    readiness = str(readiness).lower() if readiness else None
    if readiness and readiness not in READINESS:
        raise HistoryError(f"readiness must be one of {', '.join(READINESS)}.")
    week = _iso(week_start, "week_start")
    if week and date.fromisoformat(week).weekday() != 0:
        week = (date.fromisoformat(week) - timedelta(days=date.fromisoformat(week).weekday())).isoformat()
    values = (_now(), week, kind, readiness, _text(summary, "summary", required=True),
              _text(planned, "planned"), _text(actual, "actual"),
              json.dumps(details, default=str) if details is not None else None)
    cursor = conn.execute(
        """INSERT INTO coach_log (logged_at, week_start, kind, readiness, summary, planned, actual, details_json)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", values)
    return _row(conn.execute("SELECT * FROM coach_log WHERE id = ?", (cursor.lastrowid,)).fetchone())


def entries(conn, start=None, end=None, kind=None, limit: int = 50) -> list[dict]:
    """Newest first. start/end filter on the week (or the log date when there is no week)."""
    limit = max(1, min(int(limit or 50), 500))
    sql, args = "SELECT * FROM coach_log WHERE 1 = 1", []
    day = "COALESCE(week_start, substr(logged_at, 1, 10))"
    if start:
        sql += f" AND {day} >= ?"
        args.append(_iso(start, "start"))
    if end:
        sql += f" AND {day} <= ?"
        args.append(_iso(end, "end"))
    if kind:
        sql += " AND kind = ?"
        args.append(str(kind).lower())
    sql += " ORDER BY logged_at DESC, id DESC LIMIT ?"
    return [_row(r) for r in conn.execute(sql, (*args, limit)).fetchall()]


# --- athlete profile ---------------------------------------------------------------------

def _section(name: str) -> str:
    key = re.sub(r"[^a-z0-9]+", "_", str(name or "").lower()).strip("_")
    if not key or len(key) > 40:
        raise HistoryError("section must be a short name, e.g. " + ", ".join(PROFILE_SECTIONS) + ".")
    return key


def profile(conn) -> dict:
    rows = conn.execute("SELECT section, content, updated_at FROM coach_profile ORDER BY section").fetchall()
    sections = {r["section"]: {"content": r["content"], "updated_at": r["updated_at"]} for r in rows}
    return {"sections": sections, "suggested_sections": list(PROFILE_SECTIONS),
            "missing": [s for s in PROFILE_SECTIONS if s not in sections]}


def set_profile(conn, section: str, content: str | None, reason: str | None = None) -> dict:
    """Replace one section (content None or empty removes it). The old text goes to coach_log."""
    key = _section(section)
    new = _text(content, "content")
    if key == "rule_overrides" and new:
        try:
            overrides = json.loads(new)
        except ValueError:
            raise HistoryError('rule_overrides must be JSON, e.g. {"structure": {"runs": 4}}.') from None
        if not isinstance(overrides, dict):
            raise HistoryError("rule_overrides must be a JSON object.")
    if key == "season_plan" and new:
        try:
            anchor = json.loads(new)
        except ValueError:
            raise HistoryError("season_plan must be JSON; create it with start_season_plan.") from None
        missing = {"start_week", "start_run_km", "start_long_run_km", "peak_run_km"} - set(anchor)
        if not isinstance(anchor, dict) or missing:
            raise HistoryError(f"season_plan needs {', '.join(sorted(missing))}.")
    if key == "swim_css" and new:
        try:
            tests = json.loads(new)
        except ValueError:
            raise HistoryError("swim_css must be JSON; save tests with record_css_test.") from None
        if not isinstance(tests, list) or not all(isinstance(t, dict) and "css_s_per_100m" in t for t in tests):
            raise HistoryError("swim_css must be a list of tests; save them with record_css_test.")
    if key == "races" and new:
        from garmin_ai.coaching.review import parse_races
        new = json.dumps(parse_races(new))
    old = conn.execute("SELECT content FROM coach_profile WHERE section = ?", (key,)).fetchone()
    old = old["content"] if old else None
    if old == new:
        return {"section": key, "changed": False}
    if new:
        conn.execute("INSERT OR REPLACE INTO coach_profile VALUES (?, ?, ?)", (key, new, _now()))
    else:
        conn.execute("DELETE FROM coach_profile WHERE section = ?", (key,))
    add_entry(conn, "profile", reason or f"Profile section {key} {'updated' if new else 'removed'}.",
              details={"section": key, "previous": old, "new": new})
    return {"section": key, "changed": True, "previous": old, "content": new}


def _merge(base: dict, extra: dict) -> dict:
    merged = dict(base)
    for key, value in extra.items():
        merged[key] = (_merge(merged[key], value) if isinstance(value, dict) and isinstance(merged.get(key), dict)
                       else value)
    return merged


def rules(conn) -> dict:
    """rules.json with the profile's rule_overrides merged in (nested keys are merged)."""
    from garmin_ai.coaching.validator import load_rules
    row = conn.execute("SELECT content FROM coach_profile WHERE section = 'rule_overrides'").fetchone()
    return _merge(load_rules(), json.loads(row["content"])) if row else load_rules()


# --- inputs for the validator and the readiness gate from the synced history -----------------

def _runs(conn, first: date, last: date):
    return conn.execute(
        "SELECT substr(start_time, 1, 10) AS day, distance_m, raw_json FROM activities "
        "WHERE type LIKE '%running%' AND substr(start_time, 1, 10) BETWEEN ? AND ?",
        (first.isoformat(), last.isoformat())).fetchall()


def weeks_since_light(weekly: list[float], light_reduction_pct: float = 15, compare_weeks: int = 3) -> int | None:
    """Weeks since a lighter week (newest last; 1 = the last week was lighter). A week is
    lighter when it is light_reduction_pct below the highest of the compare_weeks weeks before
    it, so the low weeks after a race or illness all count, not just the first one."""
    for back in range(1, len(weekly)):
        index = len(weekly) - back
        before = [km for km in weekly[max(0, index - compare_weeks):index] if km > 0]
        if before and weekly[index] <= max(before) * (1 - light_reduction_pct / 100):
            return back
    return len(weekly) or None


EASY_LABELS = ("RECOVERY", "AEROBIC_BASE")


def recent_easy_pace(conn, before: str, days: int = 42, min_runs: int = 3) -> float | None:
    """The athlete's own easy pace in s/km: moving time over distance of easy runs (3 km+,
    Garmin label recovery / aerobic base or none) in the `days` before `before`."""
    end = date.fromisoformat(before) - timedelta(days=1)
    rows = conn.execute(
        "SELECT distance_m, COALESCE(moving_duration_s, duration_s) AS seconds, raw_json FROM activities "
        "WHERE type LIKE '%running%' AND distance_m >= 3000 AND substr(start_time, 1, 10) BETWEEN ? AND ?",
        ((end - timedelta(days=days - 1)).isoformat(), end.isoformat())).fetchall()
    meters = seconds = runs = 0
    for row in rows:
        try:
            label = json.loads(row["raw_json"] or "{}").get("trainingEffectLabel")
        except ValueError:
            label = None
        if row["seconds"] and (label in EASY_LABELS or label is None):
            meters, seconds, runs = meters + row["distance_m"], seconds + row["seconds"], runs + 1
    return round(seconds / meters * 1000) if runs >= min_runs and meters else None


def plan_history(conn, week_start: str, light_reduction_pct: float = 15, weeks: int = 12) -> dict:
    """The validator's `history` for a week starting week_start, from completed activities."""
    from garmin_ai.coach import recent_weekly_run_km
    start = date.fromisoformat(week_start)
    weekly = recent_weekly_run_km(conn, start, weeks)
    before = start - timedelta(days=1)
    longest = max(((r["distance_m"] or 0) / 1000 for r in _runs(conn, start - timedelta(days=30), before)),
                  default=0.0)

    since = weeks_since_light(weekly, light_reduction_pct)

    last_quality = last_all_out = None
    rows = conn.execute(
        "SELECT substr(start_time, 1, 10) AS day, raw_json FROM activities "
        "WHERE substr(start_time, 1, 10) BETWEEN ? AND ? ORDER BY start_time DESC",
        ((start - timedelta(days=60)).isoformat(), before.isoformat())).fetchall()
    for row in rows:
        try:
            details = json.loads(row["raw_json"] or "{}")
        except ValueError:
            continue
        if last_quality is None and details.get("trainingEffectLabel") in QUALITY_LABELS:
            last_quality = row["day"]
        if last_all_out is None and (details.get("eventType") or {}).get("typeKey") == "race":
            last_all_out = row["day"]
    history = {"weekly_run_km": weekly, "longest_run_km_30d": round(longest, 1), "weeks_since_light": since}
    if last_quality:
        history["last_quality_date"] = last_quality
    if last_all_out:
        history["last_all_out_date"] = last_all_out
    return history


def daily_rows(conn, as_of: str, days: int = 35) -> list[dict]:
    """Rows for assess_readiness: HRV, resting HR, training readiness and sleep per day."""
    end = date.fromisoformat(as_of)
    first = (end - timedelta(days=days - 1)).isoformat()
    rows = conn.execute(
        """SELECT d.date, d.hrv_overnight, COALESCE(d.resting_hr, s.resting_hr) AS resting_hr,
                  d.training_readiness, s.total_sleep_s
           FROM daily_stats d LEFT JOIN sleep s ON s.date = d.date
           WHERE d.date BETWEEN ? AND ? ORDER BY d.date""", (first, end.isoformat())).fetchall()
    return [dict(r) for r in rows]
