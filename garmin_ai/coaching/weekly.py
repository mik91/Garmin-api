"""The automatic weekly review: deterministic, no LLM.

Every Sunday evening (GARMIN_WEEKLY_REVIEW, default "sun 19:00" server time) the web server
syncs, then stores one coach_log entry of kind "review" for the week that is ending:
- progress.progress: verdict (on track / watch / act) and flags;
- review.week_review: planned vs done, readiness at the start and end of the week;
- season.targets_for: next week's target, capped by this week's run km.
Re-running for the same week replaces the automatic entry; reviews written by the coach
(log_coaching_decision) are never touched. The app shows it as the "This week" card.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, time, timedelta

from garmin_ai.analysis import activity_category
from garmin_ai.coaching import history, progress, review, season

log = logging.getLogger(__name__)
SUMMARY_PREFIX = "Weekly review (auto)"
LEVEL_ORDER = {"act": 0, "watch": 1, "ok": 2}
DAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
SCHEDULE = re.compile(r"^\s*(mon|tue|wed|thu|fri|sat|sun)\w*\s+(\d{1,2}):(\d{2})\s*$", re.IGNORECASE)


class Schedule:
    """When the review runs each week: a weekday (0 = Monday) and a time of day."""

    def __init__(self, weekday: int, at: time):
        self.weekday, self.at = weekday, at

    @classmethod
    def parse(cls, text: str | None) -> Schedule | None:
        """"sun 19:00" (any weekday name, 24 h time); "off", "" or None turn it off."""
        if not text or text.strip().lower() in ("off", "0", "false", "no"):
            return None
        match = SCHEDULE.match(text)
        if not match or int(match[2]) > 23 or int(match[3]) > 59:
            raise ValueError(f"GARMIN_WEEKLY_REVIEW must look like 'sun 19:00' or 'off', got {text!r}.")
        return cls(DAYS.index(match[1].lower()[:3]), time(int(match[2]), int(match[3])))

    def last_run_before(self, now: datetime) -> datetime:
        """The most recent scheduled moment at or before now."""
        moment = datetime.combine(now.date() - timedelta(days=(now.weekday() - self.weekday) % 7), self.at)
        return moment if moment <= now else moment - timedelta(days=7)

    def describe(self) -> str:
        return f"{DAYS[self.weekday].capitalize()} {self.at:%H:%M}"


def monday_of(day: date) -> date:
    return day - timedelta(days=day.weekday())


def week_start_of(value) -> str:
    try:
        return monday_of(date.fromisoformat(str(value)[:10])).isoformat()
    except ValueError:
        raise ValueError("week must be YYYY-MM-DD.") from None


# --- building the review ------------------------------------------------------------------

def _counts(sessions: list[dict]) -> dict:
    """Planned sessions by sport (Garmin calendar, all sources)."""
    sports = [activity_category(s.get("sport")) for s in sessions]
    return {"runs": sports.count("Running"), "swims": sports.count("Swimming"), "rides": sports.count("Cycling")}


def _next_week(target: dict | None) -> dict | None:
    if not target:
        return None
    swim_week = target.get("swim") or {}
    out = {"week_start": target["week_start"], "phase": target["phase"], "light": target.get("light", False),
           "run_km": target["run_km"], "long_run_km": target.get("long_run_km"), "long_run": target.get("long_run"),
           "runs": target.get("runs"), "quality": [q["session"] for q in target.get("quality") or []],
           "swims": swim_week.get("sessions"), "swim_m": swim_week.get("weekly_m"),
           "css_test": bool(swim_week.get("css_test"))}
    for key in ("note", "planned_run_km"):
        if target.get(key) is not None:
            out[key] = target[key]
    if target.get("checkpoint"):
        out["checkpoint"] = target["checkpoint"]["test"]
    return out


def build(conn, week_start: str, today: date | None = None) -> dict:
    """The review payload for the Monday-Sunday week starting week_start."""
    monday = date.fromisoformat(week_start_of(week_start))
    sunday = monday + timedelta(days=6)
    today = min(today or date.today(), sunday)
    week = review.week_review(conn, monday.isoformat())
    week.pop("log", None)  # earlier log lines (and earlier reviews) would nest in every review
    try:
        status = progress.progress(conn, today=today)
    except Exception as error:  # a broken profile must not stop the rest of the review
        log.warning("Season progress failed: %s", error)
        status = {"error": str(error)[:300]}
    status.pop("how_to_use", None)
    run_km = week["actual"]["run_km"]
    target = season.targets_for(conn, (monday + timedelta(days=7)).isoformat(), run_km or None)

    flags = sorted(status.get("flags") or [], key=lambda f: LEVEL_ORDER.get(f.get("level"), 3))
    planned = week["planned"]
    counts = _counts(planned["sessions"])
    actual = week["actual"]
    card = {
        "week_start": week["week_start"], "week_end": week["week_end"],
        "verdict": status.get("verdict"), "verdict_note": status.get("error"),
        "flags": flags,
        "done": {"runs": actual["runs"], "run_km": actual["run_km"], "longest_run_km": actual["longest_run_km"],
                 "quality_sessions": actual["quality_sessions"], "swims": actual["swims"], "swim_m": actual["swim_m"],
                 "run_km_change_pct": actual["run_km_change_vs_previous_week_pct"]},
        "planned": {"sessions": planned["count"], "completed": planned["completed"], "missed": planned["missed"],
                    "completion_pct": planned["completion_pct"], "runs": counts["runs"], "swims": counts["swims"],
                    "run_km": planned["coach_run_km_estimate"],
                    "quality_sessions": planned["coach_quality_sessions"],
                    "target_run_km": (week.get("season_target") or {}).get("run_km")},
        "readiness": {"start": week["readiness"]["start"]["status"], "end": week["readiness"]["end"]["status"]},
        "phase": (status.get("season") or {}).get("phase"),
        "next_week": _next_week(target),
        "summary": week["suggested_actual"],
    }
    drift = _drift(conn, monday)
    if drift is not None:
        card["plan_drift"] = drift
    return {"card": card, "progress": status, "week": week, "next_week_target": target}


def _drift(conn, monday: date) -> list[dict] | None:
    """With a rolling plan, the drift patterns of the 6 weeks to this one."""
    if not conn.execute("SELECT 1 FROM plan_versions LIMIT 1").fetchone():
        return None
    from garmin_ai.coaching import rolling
    try:
        start = (monday - timedelta(weeks=5)).isoformat()
        return rolling.drift(conn, start, monday.isoformat(), today=monday + timedelta(days=7))["patterns"]
    except Exception as error:
        log.warning("Plan drift failed: %s", error)
        return None


def _summary(card: dict) -> str:
    verdict = card["verdict"] or "no season plan"
    first = next((f["message"] for f in card["flags"] if f["level"] != "ok"), None)
    text = f"{SUMMARY_PREFIX} {card['week_start']}: {verdict}."
    return text + (f" {first}" if first else "")


def _planned_text(card: dict) -> str | None:
    nxt = card["next_week"]
    if not nxt:
        return None
    parts = [f"Next week ({nxt['phase']}{', lighter' if nxt['light'] else ''}): {nxt['run_km']:g} km run"]
    if nxt.get("long_run_km"):
        parts.append(f"long run {nxt['long_run_km']:g} km")
    parts += nxt["quality"]
    if nxt.get("swims"):
        parts.append(f"{nxt['swims']} swims")
    return "; ".join(parts)


def _existing(conn, week_start: str) -> list[int]:
    rows = conn.execute("SELECT id, details_json FROM coach_log WHERE kind = 'review' AND week_start = ?",
                        (week_start,)).fetchall()
    out = []
    for row in rows:
        try:
            if (json.loads(row[1] or "null") or {}).get("auto"):
                out.append(row[0])
        except (ValueError, AttributeError):
            continue
    return out


def run(conn, week_start: str, today: date | None = None, synced_at: str | None = None) -> dict:
    """Compute and store the review for one week, replacing an earlier automatic one."""
    week_start = week_start_of(week_start)
    from garmin_ai.coaching import execution
    try:  # sessions that went badly hold their progression step (the plan only gets more cautious)
        week_end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
        execution.apply_holds(conn, execution.reviews(conn, week_start, week_end, today=today))
    except Exception:
        pass
    payload = build(conn, week_start, today)
    card = payload["card"]
    readiness = card["readiness"]["end"]
    details = {"auto": True, "generated_at": datetime.now().isoformat(timespec="seconds"),
               "synced_at": synced_at, **payload}
    for row_id in _existing(conn, week_start):
        conn.execute("DELETE FROM coach_log WHERE id = ?", (row_id,))
    entry = history.add_entry(conn, "review", _summary(card)[:history.MAX_TEXT], week_start=week_start,
                              readiness=readiness if readiness in history.READINESS else None,
                              planned=_planned_text(card), actual=card["summary"], details=details)
    conn.commit()
    roll_forward(conn, week_start, today, payload)
    return entry


def roll_forward(conn, week_start: str, today: date | None, payload: dict) -> dict | None:
    """With a rolling plan, propose next week's changes from this review, then approve and
    publish them only as far as the rules' rolling.auto_publish policy allows."""
    from garmin_ai.coaching import rolling
    if not conn.execute("SELECT 1 FROM plan_versions LIMIT 1").fetchone():
        return None
    next_monday = (date.fromisoformat(week_start) + timedelta(days=7)).isoformat()
    rolling.refresh_hr_zones(conn)
    try:
        from garmin_ai import coach
        paces = coach.current_paces()
        rolling.on_fitness(conn, paces.get("vdot") if paces else None)
    except Exception as error:
        log.warning("Rolling plan fitness update failed: %s", error)
    try:
        result = rolling.roll_forward(conn, next_monday, today or date.today(), review=payload, save=True)
        result["auto"] = rolling.auto_apply(conn, result)
        return result
    except Exception as error:  # a broken plan must not stop the review
        log.warning("Rolling plan roll forward failed: %s", error)
        conn.rollback()
        return None


def has_review(conn, week_start: str) -> bool:
    return bool(_existing(conn, week_start_of(week_start)))


def latest(conn, week: str | None = None) -> dict | None:
    """The stored automatic review (newest, or for the week containing `week`)."""
    sql = "SELECT * FROM coach_log WHERE kind = 'review'"
    args: list = []
    if week:
        sql += " AND week_start = ?"
        args.append(week_start_of(week))
    sql += " ORDER BY week_start DESC, logged_at DESC, id DESC"
    for row in conn.execute(sql, args):
        details = json.loads(row["details_json"] or "null") or {}
        if isinstance(details, dict) and details.get("auto"):
            return {"id": row["id"], "week_start": row["week_start"], "logged_at": row["logged_at"],
                    "generated_at": details.get("generated_at"), "synced_at": details.get("synced_at"),
                    "summary": row["summary"], "planned": row["planned"], "actual": row["actual"],
                    "card": details.get("card"), "details": {k: details.get(k) for k in
                                                             ("progress", "week", "next_week_target")}}
    return None


def card_summary(entry: dict | None) -> dict | None:
    """The small version for payloads the app already loads: the card plus when it was made."""
    if not entry:
        return None
    return {"week_start": entry["week_start"], "generated_at": entry["generated_at"], **(entry["card"] or {})}


def compact(entry: dict) -> dict:
    """A log entry with an automatic review's details cut to its card (for coaching_history)."""
    details = entry.get("details")
    if isinstance(details, dict) and details.get("auto"):
        return {**entry, "details": {"auto": True, "generated_at": details.get("generated_at"),
                                     "card": details.get("card")}}
    return entry
