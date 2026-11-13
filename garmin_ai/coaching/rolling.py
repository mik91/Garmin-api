"""Rolling plan storage (docs/rolling-plan.md): every day's workout to race day as drafts.

regenerate() materializes the season plan (season.generate + materialize.week) for every week
after the last week that is already on Garmin, and stores the result in plan_drafts. Rows are
never edited: a workout that changes gets a new row pointing at the old one (parent_id), and
the old one is marked superseded, so the first draft, every change and the final version stay
in the table. Nothing here talks to Garmin; publishing a week comes later (step 3).
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone

from garmin_ai import workouts
from garmin_ai.coaching import history, materialize, season, swim

log = logging.getLogger(__name__)
SCHEDULE_SECTION = "plan_schedule"
TRIGGERS = ("start", "manual", "css_test", "race_result", "vdot", "profile", "rules", "hr_zones")
LIVE = ("draft", "proposed", "proposed_drop", "published", "done", "skipped")  # not superseded or rejected


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")  # proposals group by it


def _monday(value: date) -> date:
    return value - timedelta(days=value.weekday())


def _hash(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()[:16]


def schedule(conn) -> dict:
    """The weekly template: materialize.DEFAULT_SCHEDULE with the profile's plan_schedule
    section (JSON, e.g. {"long": "sat", "quality": ["tue", "fri"]}) on top."""
    row = conn.execute("SELECT content FROM coach_profile WHERE section = ?", (SCHEDULE_SECTION,)).fetchone()
    if not row:
        return dict(materialize.DEFAULT_SCHEDULE)
    try:
        custom = json.loads(row["content"])
    except ValueError:
        raise history.HistoryError(f"Profile section {SCHEDULE_SECTION} must be JSON.") from None
    if not isinstance(custom, dict) or set(custom) - set(materialize.DEFAULT_SCHEDULE):
        raise history.HistoryError(f"{SCHEDULE_SECTION} keys: {', '.join(materialize.DEFAULT_SCHEDULE)}.")
    for key, value in custom.items():
        days = value if isinstance(value, list) else [value]
        listed = isinstance(materialize.DEFAULT_SCHEDULE[key], list)
        if (not days and key != "midday_swims") or any(d not in materialize.DAYS for d in days) or \
                isinstance(value, list) != listed:
            raise history.HistoryError(f"{SCHEDULE_SECTION}.{key}: use day names like "
                                       f"{materialize.DEFAULT_SCHEDULE[key]!r}.")
    return {**materialize.DEFAULT_SCHEDULE, **custom}


def first_open_week(conn, today: date | None = None) -> date:
    """The first Monday the rolling plan may (re)write: after this week, and after the last
    week that has workouts on Garmin (published drafts or other [TP] workouts)."""
    today = today or date.today()
    first = _monday(today) + timedelta(days=7)
    last = conn.execute(
        """SELECT MAX(d) FROM (SELECT MAX(date) AS d FROM plan_drafts WHERE status = 'published'
                              UNION ALL SELECT MAX(date) FROM coach_schedules WHERE date >= ?)""",
        (today.isoformat(),)).fetchone()[0]
    if last:
        first = max(first, _monday(date.fromisoformat(last)) + timedelta(days=7))
    return first


def _inputs(conn, vdot: float | None):
    race, saved = season.a_race(conn), season.anchor(conn)
    if not race:
        raise history.HistoryError("No upcoming A race in the profile races section.")
    if not saved:
        raise history.HistoryError("No season plan yet; create one with start_season_plan.")
    goal = season.goal_pace(race)
    meters = season.DISTANCE_M[season.distance_key(race)]
    return {"race": race, "anchor": saved, "rules": history.rules(conn), "css": swim.latest_css(conn),
            "vdot": round(float(vdot or saved.get("start_vdot") or 45), 1),
            "goal_s_per_km": round(goal["seconds"] / meters * 1000, 1) if goal else None,
            "schedule": schedule(conn), "hr": materialize.hr_targets(hr_zones(conn)),
            "swim_pace": own_paces(conn)[1]}


def build(conn, vdot: float | None = None, today: date | None = None) -> dict:
    """Materialized weeks from the first open week to race week (nothing is stored). Paces
    come from vdot, else the latest plan version's, else the season anchor's."""
    if vdot is None:
        latest = conn.execute("SELECT anchor_json FROM plan_versions ORDER BY id DESC LIMIT 1").fetchone()
        vdot = json.loads(latest["anchor_json"]).get("vdot") if latest else None
    data = _inputs(conn, vdot)
    first = first_open_week(conn, today).isoformat()
    weeks = []
    for target in season.generate(data["anchor"], data["race"], data["rules"], data["css"], with_steps=True):
        if target["week_start"] < first:
            continue
        items = materialize.week(target, data["vdot"], data["goal_s_per_km"], data["race"], data["schedule"],
                                 data["rules"], data["hr"], data["swim_pace"])
        weeks.append({"target": target, "items": items})
    return {**data, "first_week": first, "weeks": weeks}


PROPOSAL = ("proposed", "proposed_drop")


def _current(conn, first: str, last: str | None = None) -> dict[str, list]:
    """Draft and proposal rows per week from first (to last)."""
    rows = conn.execute("SELECT * FROM plan_drafts WHERE week_start >= ? AND week_start <= ? "
                        "AND status IN ('draft', 'proposed', 'proposed_drop') ORDER BY date, name",
                        (first, last or "9999-12-31")).fetchall()
    out: dict[str, list] = {}
    for row in rows:
        out.setdefault(row["week_start"], []).append(dict(row))
    return out


def _brief(item: dict) -> str:
    return f"{item['date']} {item['name']}"


def _same(row: dict, item: dict) -> bool:
    return row["spec_json"] == json.dumps(item["spec"], sort_keys=True) and \
        json.loads(row["session_json"] or "{}") == item["session"]


def _diff(old: list[dict], items: list[dict]) -> dict:
    """What turns the stored rows `old` into `items`: added and changed items (with the row they
    replace), removed rows, and how many stay as they are."""
    by_key = {(r["date"], r["name"]): r for r in old}
    by_kind: dict = {}
    for r in old:
        by_kind.setdefault((r["date"], r["kind"]), r)
    added, changed, unchanged, used = [], [], 0, set()
    for item in items:
        same = by_key.get((item["date"], item["name"]))
        if same and _same(same, item):
            unchanged += 1
            used.add(same["id"])
            continue
        parent = same or by_kind.get((item["date"], item["kind"]))
        if parent and parent["id"] in used:
            parent = None
        if parent:
            used.add(parent["id"])
        (changed if parent else added).append((item, parent))
    removed = [r for r in old if r["id"] not in used]
    return {"added": added, "changed": changed, "removed": removed, "unchanged": unchanged}


def _report(diff: dict) -> dict:
    return {"unchanged": diff["unchanged"], "added": [_brief(i) for i, _ in diff["added"]],
            "changed": [f"{_brief(p)} -> {i['name']}" if p["name"] != i["name"] else _brief(i)
                        for i, p in diff["changed"]],
            "removed": [_brief(r) for r in diff["removed"]]}


def _insert(conn, version, monday: str, item: dict, status: str, origin: str, reason: str | None,
            parent: dict | None, now: str) -> int:
    return conn.execute(
        """INSERT INTO plan_drafts (plan_id, week_start, date, slot, sport, name, kind, spec_json,
           session_json, status, origin, reason, parent_id, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (version, monday, item["date"], item["session"].get("slot"), item["sport"], item["name"], item["kind"],
         json.dumps(item["spec"], sort_keys=True), json.dumps(item["session"], sort_keys=True), status, origin,
         reason, parent["id"] if parent else None, now, now)).lastrowid


def _set_status(conn, ids, status: str, now: str) -> None:
    conn.executemany("UPDATE plan_drafts SET status = ?, updated_at = ? WHERE id = ?",
                     [(status, now, i) for i in ids])


def regenerate(conn, trigger: str = "manual", vdot: float | None = None, dry_run: bool = True,
               today: date | None = None, note: str | None = None) -> dict:
    """Rebuild every open week from the season plan. Weeks with a proposed change waiting for
    approval are left alone. Returns per week what was added, changed and removed."""
    if trigger not in TRIGGERS:
        raise history.HistoryError(f"trigger must be one of {', '.join(TRIGGERS)}.")
    built = build(conn, vdot, today)
    watch = policy(built["rules"])["watch_sports"]
    current = _current(conn, built["first_week"])
    report, writes = [], []
    for week in built["weeks"]:
        monday = week["target"]["week_start"]
        old = current.get(monday, [])
        if any(r["status"] in PROPOSAL for r in old):
            report.append({"week_start": monday, "skipped": "a proposed change is waiting for approval"})
            continue
        diff = _diff(old, week["items"])
        report.append({"week_start": monday, "phase": week["target"]["phase"], "light": week["target"]["light"],
                       "run_km": week["target"]["run_km"], **_report(diff),
                       "plan_only": [_brief(i) for i in week["items"] if i["sport"] not in watch]})
        writes.append((monday, diff))

    counts = {k: sum(len(w.get(k, [])) for w in report) for k in ("added", "changed", "removed")}
    result = {"dry_run": dry_run, "trigger": trigger, "first_week": built["first_week"],
              "race": built["race"]["name"], "vdot": built["vdot"], "counts": counts,
              "sent_to_watch": watch, "note": "Only sent_to_watch sports go to Garmin when a week is published; "
              "plan_only workouts stay in the plan (e.g. gym logged in Hevy).", "weeks": report}
    has_version = conn.execute("SELECT 1 FROM plan_versions LIMIT 1").fetchone()
    if dry_run or (not any(counts.values()) and has_version):
        return result

    now = _now()
    version = _new_version(conn, built, trigger, note, now)
    reason = f"Regenerated ({trigger})" + (f": {note}" if note else "")
    for monday, diff in writes:
        _set_status(conn, [p["id"] for _, p in diff["changed"]] + [r["id"] for r in diff["removed"]],
                    "superseded", now)
        for item, parent in diff["added"] + diff["changed"]:
            _insert(conn, version, monday, item, "draft", "generator", reason if parent else None, parent, now)
    history.add_entry(conn, "plan", f"Rolling plan {'created' if not has_version else 'regenerated'} ({trigger}) "
                      f"from {built['first_week']}: {counts['added']} added, {counts['changed']} changed, "
                      f"{counts['removed']} removed.", week_start=built["first_week"],
                      details={"version": version, "vdot": built["vdot"], "counts": counts})
    conn.commit()
    return {**result, "version": version}


def _new_version(conn, built: dict, trigger: str, note: str | None, now: str) -> int:
    inputs = {k: built[k] for k in ("anchor", "vdot", "goal_s_per_km", "css", "schedule", "hr")}
    return conn.execute(
        "INSERT INTO plan_versions (created_at, trigger, anchor_json, rules_hash, note) VALUES (?, ?, ?, ?, ?)",
        (now, trigger, json.dumps(inputs, sort_keys=True), _hash(built["rules"]), note)).lastrowid


def plan(conn, start: str | None = None, weeks: int | None = None) -> dict:
    """The stored plan by week: every live workout (draft, proposed, proposed_drop, published,
    done, skipped) with its status, origin and why it changed, plus the week's season target.
    A week with a proposal shows proposed_run_km and the reason; proposed rows replace their
    parent_id, proposed_drop rows would be removed."""
    version = _latest_version(conn)
    if not version:
        return {"weeks": [], "note": "No rolling plan yet; create it with regenerate_plan."}
    inputs = json.loads(version["anchor_json"])
    paces = materialize.paces_for(inputs["vdot"])
    easy, swim_pace = own_paces(conn)
    watch = policy(history.rules(conn))["watch_sports"]
    start = _monday(date.fromisoformat(start)).isoformat() if start else _monday(date.today()).isoformat()
    rows = conn.execute(f"""SELECT * FROM plan_drafts WHERE week_start >= ?
                            AND status IN ({','.join('?' * len(LIVE))}) ORDER BY date, id""",
                        (start, *LIVE)).fetchall()
    try:
        targets = {w["week_start"]: w for w in season.plan(conn).get("weeks", [])}
    except Exception:
        targets = {}
    by_week: dict[str, list] = {}
    for row in rows:
        spec = json.loads(row["spec_json"])
        payload = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                         spec.get("pool_length_m"))
        hints = json.loads(row["session_json"] or "{}")
        estimate = workouts.to_session(row["date"], payload, paces, hints, swim_pace, easy)
        by_week.setdefault(row["week_start"], []).append({
            "id": row["id"], "date": row["date"], "sport": row["sport"], "kind": row["kind"], "name": row["name"],
            "status": row["status"], "origin": row["origin"], "reason": row["reason"],
            "parent_id": row["parent_id"], "workout_id": row["workout_id"],
            "minutes": estimate["minutes"], "km": estimate.get("km"), "intensity": estimate["intensity"],
            "description": spec.get("description"), "summary": workouts.summary(payload), "spec": spec,
            "on_watch": row["sport"] in watch})
    out = []
    for monday in sorted(by_week)[: weeks or None]:
        items = by_week[monday]
        target = targets.get(monday) or {}
        replaced = {i["parent_id"] for i in items if i["status"] == "proposed"}
        current = [i for i in items if i["status"] != "proposed"]
        proposed = [i for i in items if i["status"] != "proposed_drop" and i["id"] not in replaced]
        km = lambda rows: round(sum(i["km"] or 0 for i in rows if i["sport"] == "run"), 1)
        week = {"week_start": monday, "phase": target.get("phase"), "light": target.get("light"),
                "target_run_km": target.get("run_km"), "planned_run_km": km(current),
                "proposal": len(proposed) != len(items) or bool(replaced), "items": items}
        if week["proposal"]:
            week["proposed_run_km"] = km(proposed)
            week["proposal_reason"] = next((i["reason"] for i in items if i["status"] == "proposed"), None)
        out.append(week)
    return {"version": {"id": version["id"], "created_at": version["created_at"], "trigger": version["trigger"],
                        "vdot": inputs["vdot"]}, "weeks": out}


# --- the weekly roll forward (step 3) ---------------------------------------------------------

SPORT_TYPES = {"run": "Running", "swim": "Swimming", "bike": "Cycling"}


def own_paces(conn) -> tuple[float | None, float]:
    """The athlete's own recent easy run pace (s/km, None with too few easy runs) and swim pace
    (s/100 m), so time estimates match how they actually run and swim."""
    today = date.today().isoformat()
    swim_pace = swim.recent_pace(conn, today) or \
        history.rules(conn).get("swim", {}).get("default_pace_s_per_100m", workouts.SWIM_S_PER_100M)
    return history.recent_easy_pace(conn, today), swim_pace


def _ramp_limit(conn, monday: str, rules: dict) -> tuple[float, float] | None:
    """(most km the validator allows for the week starting monday, last week's km) from the km
    actually run, or None without a last week to compare with."""
    used = history.plan_history(conn, monday, rules["light_week"]["reduction_pct_min"],
                                rules["ramp"]["peak_lookback_weeks"])
    weekly = used.get("weekly_run_km") or []
    prev = weekly[-1] if weekly else 0
    if prev <= 0:
        return None
    peak = max(weekly)
    r = rules["ramp"]
    cap = max(min(prev * (1 + r["rebuild_max_pct"] / 100), peak), prev * (1 + r["above_peak_max_pct"] / 100))
    return math.floor(cap * 10 + 1e-6) / 10, prev


def _latest_version(conn):
    return conn.execute("SELECT * FROM plan_versions ORDER BY id DESC LIMIT 1").fetchone()


def _activities_on(conn, day: str, sport: str) -> list[str]:
    from garmin_ai.analysis import activity_category
    rows = conn.execute("SELECT activity_id, type FROM activities WHERE substr(start_time, 1, 10) = ? "
                        "ORDER BY start_time", (day,)).fetchall()
    if sport == "gym":
        found = [str(r["activity_id"]) for r in rows if "strength" in str(r["type"] or "").lower()]
        hevy = conn.execute("SELECT workout_id FROM hevy_workouts WHERE substr(COALESCE(start_local, start_time), "
                            "1, 10) = ?", (day,)).fetchall()
        return found + [f"hevy:{r['workout_id']}" for r in hevy]
    return [str(r["activity_id"]) for r in rows if activity_category(r["type"]) == SPORT_TYPES.get(sport)]


def close_days(conn, today: date | None = None, save: bool = True) -> dict:
    """Mark every draft or published workout before today done (an activity of that sport on
    that day) or skipped."""
    today = today or date.today()
    rows = conn.execute("SELECT id, date, sport FROM plan_drafts WHERE status IN ('draft', 'published') "
                        "AND date < ? ORDER BY date, id", (today.isoformat(),)).fetchall()
    used: set = set()
    marks = []
    for row in rows:
        free = [a for a in _activities_on(conn, row["date"], row["sport"]) if a not in used]
        if free:
            used.add(free[0])
        marks.append((row["id"], "done" if free else "skipped", free[0] if free else None))
    if save and marks:
        now = _now()
        conn.executemany("UPDATE plan_drafts SET status = ?, activity_id = ?, updated_at = ? WHERE id = ?",
                         [(status, activity, now, row_id) for row_id, status, activity in marks])
    return {"done": sum(m[1] == "done" for m in marks), "skipped": sum(m[1] == "skipped" for m in marks)}


def _run_km(conn, monday: date) -> float:
    rows = history._runs(conn, monday, monday + timedelta(days=6))
    return round(sum((r["distance_m"] or 0) for r in rows) / 1000, 1)


def _readiness(conn, today: date, review: dict | None) -> str:
    if review:
        return ((review.get("card") or {}).get("readiness") or {}).get("end") or "unknown"
    from garmin_ai.coaching import assess_readiness
    as_of = (today - timedelta(days=1)).isoformat()
    try:
        return assess_readiness(history.daily_rows(conn, as_of), as_of).get("status") or "unknown"
    except Exception:
        return "unknown"


def _flags(conn, today: date, review: dict | None) -> list[dict]:
    """Flags the review raised that need the athlete or the coach (never changed in code)."""
    if review:
        status = review.get("progress") or {}
    else:
        from garmin_ai.coaching import progress
        try:
            status = progress.progress(conn, today=today)
        except Exception:
            status = {}
    return [f for f in status.get("flags") or [] if f.get("level") in ("act", "watch")]


def _live_rows(conn, week_start: str) -> list[dict]:
    """A week's current rows: drafts, a pending proposal, and what is on Garmin."""
    return [dict(r) for r in conn.execute(
        "SELECT * FROM plan_drafts WHERE week_start = ? AND status IN ('draft', 'proposed', 'proposed_drop', "
        "'published') ORDER BY date, name", (week_start,))]


def _restore(conn, rows: list[dict], now: str) -> None:
    """proposed_drop rows go back to what they were: published (on Garmin, or kept off the
    watch: workout_id "") or draft (workout_id NULL)."""
    drops = [r for r in rows if r["status"] == "proposed_drop"]
    _set_status(conn, [r["id"] for r in drops if r["workout_id"] is not None], "published", now)
    _set_status(conn, [r["id"] for r in drops if r["workout_id"] is None], "draft", now)


def _discard_proposal(conn, rows: list[dict], now: str) -> None:
    _set_status(conn, [r["id"] for r in rows if r["status"] == "proposed"], "superseded", now)
    _restore(conn, rows, now)


def _store_proposal(conn, monday: str, rows: list[dict], diff: dict, origin: str, reason: str) -> None:
    now = _now()
    _discard_proposal(conn, rows, now)
    version = (_latest_version(conn) or {"id": None})["id"]
    for item, parent in diff["added"] + diff["changed"]:
        _insert(conn, version, monday, item, "proposed", origin, reason, parent, now)
    _set_status(conn, [r["id"] for r in diff["removed"]], "proposed_drop", now)


def roll_forward(conn, monday: str | None = None, today: date | None = None, vdot: float | None = None,
                 review: dict | None = None, save: bool = False) -> dict:
    """Next week's suggested changes after the review of the week before it (rules, no LLM):
    the season week rebuilt (session-review holds, new CSS), capped at last week's run km grown
    at the plan's rate (missed sessions are not made up), quality swapped for easy running on
    red readiness, the optional session dropped on yellow. Flags (pace bias, cautions) are only
    reported. save=True marks past workouts done or skipped and stores the changes as a
    proposal: proposed rows (new or replacing a draft) and proposed_drop rows (to remove)."""
    today = today or date.today()
    version = _latest_version(conn)
    if not version:
        raise history.HistoryError("No rolling plan yet; create it with regenerate_plan.")
    next_monday = _monday(date.fromisoformat(monday)) if monday else _monday(today) + timedelta(days=7)
    week_start = next_monday.isoformat()
    published = conn.execute("SELECT 1 FROM plan_drafts WHERE week_start = ? AND status = 'published' LIMIT 1",
                             (week_start,)).fetchone()
    if published:
        raise history.HistoryError(f"Week {week_start} is already on Garmin.")
    if _outside_plan(conn, week_start):
        raise history.HistoryError(f"Week {week_start} already has workouts on Garmin planned outside the rolling "
                                   "plan; bring them in with adopt_week instead of planning the week again.")
    closed = close_days(conn, today, save)
    data = _inputs(conn, vdot or json.loads(version["anchor_json"]).get("vdot"))
    weeks = season.generate(data["anchor"], data["race"], data["rules"], data["css"], with_steps=True)
    target = next((w for w in weeks if w["week_start"] == week_start), None)
    if not target:
        raise history.HistoryError(f"Week {week_start} is not in the season plan.")
    target = dict(target)
    last_km = _run_km(conn, next_monday - timedelta(days=7))
    readiness = _readiness(conn, today, review)
    cfg = season.settings(data["rules"])
    reasons = []

    capped = season.targets_for(conn, week_start, last_km or None)
    if capped and capped.get("planned_run_km") and capped["run_km"] < target["run_km"]:
        cap, why = capped["run_km"], ""
        if _one_off(conn, next_monday, target["run_km"]):
            # A single short week after normal ones: come back as fast as the rules allow
            # (the validator's rebuild limit), with the long run at its largest allowed share.
            fastest = math.floor(last_km * (1 + data["rules"]["ramp"]["rebuild_max_pct"] / 100) * 10) / 10
            if fastest > cap:
                cap, why = min(fastest, target["run_km"]), " (a one-off short week: the fastest return the rules allow)"
        share = data["rules"]["long_run"]["max_share_of_weekly_km"] if why else cfg["long_share"]
        target["run_km"] = cap
        target["long_run_km"] = min(target["long_run_km"], math.floor(cap * share * 10) / 10)
        reasons.append(f"Capped at {cap:g} km{why}: last week was {last_km:g} km, and missed "
                       "sessions are not made up.")
    limit = _ramp_limit(conn, week_start, data["rules"]) if target["phase"] not in ("taper", "race_week") else None
    if limit and target["run_km"] > limit[0]:
        target["run_km"] = limit[0]
        target["long_run_km"] = min(target["long_run_km"], math.floor(limit[0] * cfg["long_share"] * 10) / 10)
        reasons.append(f"Held at {limit[0]:g} km, the most the ramp rule allows after {limit[1]:g} km last week.")
    if readiness == "red" and target["phase"] != "race_week":
        cut = cfg["long_light_cut_km"]
        target.update(quality=[], extras=[], flex=None, long_run="easy",
                      long_run_km=max(0.0, round(target["long_run_km"] - cut, 1)))
        target.pop("checkpoint", None)
        reasons.append(f"Readiness red: quality runs swapped for easy running and the long run {cut:g} km "
                       "shorter.")
    elif readiness == "yellow" and target.get("flex"):
        target["flex"] = None
        reasons.append("Readiness yellow: the optional session is dropped; keep the quality runs controlled.")

    items = materialize.week(target, data["vdot"], data["goal_s_per_km"], data["race"], data["schedule"],
                             data["rules"], data["hr"], data["swim_pace"])
    rows = _current(conn, week_start, week_start).get(week_start, [])
    drafts = [r for r in rows if r["status"] in ("draft", "proposed_drop")]
    diff = _diff(drafts, items)
    changes = bool(diff["added"] or diff["changed"] or diff["removed"])
    if changes and not reasons:
        reasons.append("The season plan changed since this week was drafted (session review holds, "
                       "CSS test or fitness).")
    reason = " ".join(reasons) or None
    result = {"dry_run": not save, "week_start": week_start, "phase": target["phase"], "readiness": readiness,
              "last_week_run_km": last_km, "run_km": target["run_km"], "reason": reason,
              "flags": _flags(conn, today, review), "closed": closed,
              "minor": changes and is_minor(diff, drafts, items, policy(data["rules"])["minor_km_pct"]),
              **_report(diff)}
    if save:
        if changes:
            _store_proposal(conn, week_start, rows, diff, "review", reason)
            history.add_entry(conn, "adjustment", f"Proposed changes for {week_start}: {reason}"[:history.MAX_TEXT],
                              week_start=week_start, readiness=readiness if readiness in history.READINESS else None,
                              details={k: result[k] for k in ("added", "changed", "removed", "run_km")})
        else:
            _discard_proposal(conn, rows, _now())
        conn.commit()
    return {**result, "proposal": changes}


def _one_off(conn, next_monday: date, target_km: float) -> bool:
    """The 3 weeks before last week were all near this week's target: last week was a blip."""
    from garmin_ai.coach import recent_weekly_run_km
    before = recent_weekly_run_km(conn, next_monday - timedelta(days=7), 3)
    return bool(before) and min(before) >= 0.8 * target_km


def decide(conn, monday: str, approve: bool, note: str | None = None) -> dict:
    """Approve (the proposal becomes the week's drafts) or reject (the drafts stay) a proposal.
    In a week already on Garmin, approving also updates Garmin: changed workouts are uploaded
    (in place when the day and name stay) and removed ones are deleted."""
    week_start = _monday(date.fromisoformat(monday)).isoformat()
    rows = _live_rows(conn, week_start)
    proposed = [r for r in rows if r["status"] == "proposed"]
    drops = [r for r in rows if r["status"] == "proposed_drop"]
    if not proposed and not drops:
        raise history.HistoryError(f"No proposal for the week of {week_start}.")
    now = _now()
    by_id = {r["id"]: r for r in rows}
    parents = [by_id[r["parent_id"]] for r in proposed if r["parent_id"] in by_id]
    on_garmin = any(r["status"] == "published" for r in rows) or any(r["workout_id"] for r in parents + drops)
    if approve:
        _set_status(conn, [r["id"] for r in proposed], "draft", now)
        _set_status(conn, [r["id"] for r in parents + drops], "superseded", now)
    else:
        _set_status(conn, [r["id"] for r in proposed], "rejected", now)
        _restore(conn, drops, now)
    verb = "Approved" if approve else "Rejected"
    history.add_entry(conn, "adjustment" if approve else "note",
                      f"{verb} the proposed changes for {week_start}" + (f": {note}" if note else "."),
                      week_start=week_start)
    conn.commit()
    result = {"week_start": week_start, "approved": approve, "changed": len(proposed), "removed": len(drops)}
    if approve and on_garmin:
        result["garmin"] = _sync_garmin(conn, week_start, [r for r in parents + drops if r["workout_id"]])
    return result


def _sync_garmin(conn, week_start: str, gone: list[dict]) -> dict:
    """After an approved change to a week on Garmin: upload its drafts and delete the [TP]
    workouts of rows that were replaced or removed and not updated in place."""
    from garmin_ai import coach
    out = {"published": 0, "deleted": [], "errors": []}
    if any(r["status"] == "draft" for r in _live_rows(conn, week_start)):
        try:
            out["published"] = publish(conn, week_start, dry_run=False, allow_errors=True)["published"]
        except history.HistoryError as error:
            out["errors"].append(str(error))
    kept = {r["workout_id"] for r in _live_rows(conn, week_start) if r["status"] == "published"}
    for row in gone:
        if row["workout_id"] in kept:
            continue
        try:
            coach.delete_workout(row["workout_id"])
            out["deleted"].append(f"{row['date']} {row['name']}")
        except Exception as error:  # already gone, or Garmin refused: report, keep going
            out["errors"].append(f"{row['date']} {row['name']}: {error}")
    return out


def _kind(session: dict) -> str:
    if session["sport"] != "run":
        return session["sport"]
    if session.get("kind") == "long":
        return "long"
    return {"race": "race", "quality": "quality"}.get(session.get("intensity"), "easy")


def propose_week(conn, monday: str, items: list[dict], reason: str, dry_run: bool = True,
                 allow_errors: bool = False, today: date | None = None) -> dict:
    """A coach's (agent's) version of a week, in upload_plan's item format. It is checked with
    the coaching rules like an upload and stored as a proposal (origin agent) that replaces any
    earlier proposal for the week. For a week already on Garmin (a mid-week change), items
    cover today to Sunday: earlier days are history and stay as they are."""
    from garmin_ai import coach
    week_start = _monday(date.fromisoformat(monday)).isoformat()
    end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
    rows = _live_rows(conn, week_start)
    on_garmin = any(r["status"] == "published" or r["workout_id"] for r in rows)
    first = max(week_start, (today or date.today()).isoformat()) if on_garmin else week_start
    if first > end:
        raise history.HistoryError(f"The week of {week_start} is over.")
    if not reason or not str(reason).strip():
        raise history.HistoryError("Give the reason for the change (1-2 sentences).")
    if any(not isinstance(i, dict) or not first <= str(i.get("date")) <= end for i in items or [None]):
        raise history.HistoryError(f"Every item needs a date in {first}..{end}"
                                   + (" (the week is on Garmin: from today on)." if on_garmin else "."))
    try:
        check = coach.upload_plan(items, dry_run=True)
    except coach.CoachError as error:
        raise history.HistoryError(str(error)) from None
    sessions = {(s["date"], s["name"]): s for v in check["validation"] for s in v["sessions"]
                if not s.get("already_scheduled")}
    planned = []
    for item in items:
        spec = {k: v for k, v in item["workout"].items() if v is not None}
        name = workouts.strip_prefix(workouts.with_prefix(spec["name"]))
        spec["name"] = name
        session = sessions.get((item["date"], name)) or {"sport": "other"}
        planned.append({"date": item["date"], "sport": workouts.VALIDATOR_SPORTS[workouts.sport_key(spec["sport"])],
                        "kind": _kind(session), "name": name, "spec": spec, "session": item.get("session") or {}})
    base = [r for r in rows if r["status"] in ("draft", "published", "proposed_drop") and r["date"] >= first]
    diff = _diff(base, planned)
    result = {"dry_run": dry_run, "week_start": week_start, "from": first, "on_garmin": on_garmin,
              "errors": check["errors"], "warnings": check["warnings"], **_report(diff)}
    if dry_run:
        return result
    if check["errors"] and not allow_errors:
        raise history.HistoryError("The week breaks coaching rules; nothing was stored:\n- "
                                   + "\n- ".join(check["errors"]))
    _store_proposal(conn, week_start, rows, diff, "agent", str(reason).strip())
    history.add_entry(conn, "adjustment", f"Coach proposal for {week_start}: {reason}"[:history.MAX_TEXT],
                      week_start=week_start)
    conn.commit()
    return result


def _without_off_watch(warnings: list, sports: list[str]) -> list:
    """Drop the session-count warnings for sports kept off the watch: they are in the plan,
    so the count on Garmin is expected to be short (e.g. "0 gym sessions; at least 1")."""
    words = {"gym": ("gym session",), "bike": ("bike session", "No bike session")}
    hidden = [w for sport, ws in words.items() if sport not in sports for w in ws]
    return [w for w in warnings if not any(h in str(w) for h in hidden)]


def publish(conn, monday: str, dry_run: bool = True, allow_errors: bool = False) -> dict:
    """Send a week's drafts to Garmin with upload_plan and mark them published. Sports the
    rules keep off the watch (rolling.watch_sports, e.g. gym logged in Hevy) are published in
    the plan only: status published with workout_id "", never uploaded."""
    from garmin_ai import coach
    week_start = _monday(date.fromisoformat(monday)).isoformat()
    rows = _current(conn, week_start, week_start).get(week_start, [])
    if any(r["status"] in PROPOSAL for r in rows):
        raise history.HistoryError(f"Approve or reject the proposal for {week_start} first.")
    drafts = [r for r in rows if r["status"] == "draft"]
    if not drafts:
        raise history.HistoryError(f"No drafts to publish for the week of {week_start}.")
    sports = policy(history.rules(conn))["watch_sports"]
    to_watch = [r for r in drafts if r["sport"] in sports]
    off_watch = [r for r in drafts if r["sport"] not in sports]
    items = [{"date": r["date"], "workout": json.loads(r["spec_json"]),
              "session": json.loads(r["session_json"] or "{}")} for r in to_watch]
    conn.commit()  # upload_plan opens its own connection
    upload = {"items": [], "warnings": [], "errors": []}
    if items:
        try:
            upload = coach.upload_plan(items, dry_run=dry_run, allow_errors=allow_errors)
        except coach.CoachError as error:
            raise history.HistoryError(str(error)) from None
    published = 0
    if not dry_run:
        by_key = {(e["date"], workouts.strip_prefix(e["name"])): e for e in upload["items"]}
        now = _now()
        for row in to_watch:
            entry = by_key.get((row["date"], row["name"]))
            if entry and entry.get("action") in ("create", "update", "unchanged") and entry.get("workout_id"):
                conn.execute("UPDATE plan_drafts SET status = 'published', workout_id = ?, updated_at = ? "
                             "WHERE id = ?", (str(entry["workout_id"]), now, row["id"]))
                published += 1
        conn.executemany("UPDATE plan_drafts SET status = 'published', workout_id = '', updated_at = ? WHERE id = ?",
                         [(now, r["id"]) for r in off_watch])
        conn.commit()
    if dry_run:
        for entry in upload["items"]:
            entry.pop("garmin_payload", None)
    return {"dry_run": dry_run, "week_start": week_start, "workouts": len(to_watch), "published": published,
            "off_watch": [_brief(r) for r in off_watch],
            "errors": upload.get("errors") or upload.get("errors_allowed") or [],
            "warnings": _without_off_watch(upload["warnings"], sports), "items": upload["items"]}


# --- approval policy and the app (step 4) ------------------------------------------------------

POLICIES = ("never", "unchanged", "minor", "always")
MINOR_KINDS = {"easy", "swim", "bike", "gym", "flex"}


def policy(rules: dict) -> dict:
    """rules "rolling": auto_publish (never, unchanged, minor, always), minor_km_pct, and
    watch_sports (the sports sent to Garmin; the others stay in the plan only)."""
    cfg = {"auto_publish": "never", "minor_km_pct": 10, "watch_sports": ["run", "swim", "bike", "gym"],
           **(rules.get("rolling") or {})}
    if cfg["auto_publish"] not in POLICIES:
        cfg["auto_publish"] = "never"
    if not isinstance(cfg["watch_sports"], list):
        cfg["watch_sports"] = ["run", "swim", "bike", "gym"]
    return cfg


def _spec_km(spec: dict, paces: dict) -> float:
    return materialize.km_of(spec, paces) if workouts.sport_key(spec["sport"]) == "running" else 0.0


def is_minor(diff: dict, drafts: list[dict], items: list[dict], km_pct: float) -> bool:
    """A small change: only easy runs, swims, bike, gym or the optional session are added,
    changed or removed, nothing moves to another day, and the week's run km stays within
    km_pct of the drafts."""
    touched = ([i["kind"] for i, _ in diff["added"] + diff["changed"]]
               + [p["kind"] for _, p in diff["changed"]] + [r["kind"] for r in diff["removed"]])
    if any(kind not in MINOR_KINDS for kind in touched):
        return False
    if any(i["date"] != p["date"] for i, p in diff["changed"]):
        return False
    paces = materialize.paces_for(50)  # easy runs are measured by distance; the VDOT barely matters
    before = sum(_spec_km(json.loads(r["spec_json"]), paces) for r in drafts)
    after = sum(_spec_km(i["spec"], paces) for i in items)
    return not before or abs(after - before) / before * 100 <= km_pct + 1e-9


def auto_apply(conn, result: dict) -> dict | None:
    """After the Sunday roll forward: approve and publish next week when the athlete's policy
    allows it. Rule errors, Garmin errors and proposals the policy does not cover leave the
    week waiting for the athlete."""
    mode = policy(history.rules(conn))["auto_publish"]
    week = result["week_start"]
    if mode == "never":
        return None
    if result["proposal"]:
        if not (mode == "always" or (mode == "minor" and result.get("minor"))):
            return {"published": False, "waiting": "the proposal needs the athlete's OK"}
        decide(conn, week, True, f"approved automatically (auto_publish {mode})")
    try:
        published = publish(conn, week, dry_run=False)
    except history.HistoryError as error:
        conn.rollback()
        history.add_entry(conn, "note", f"Week {week} was not published automatically: {error}"[:history.MAX_TEXT],
                          week_start=week)
        conn.commit()
        return {"published": False, "error": str(error)}
    return {"published": True, "workouts": published["published"]}


# --- app endpoints (POST /api/rolling-plan/...) ------------------------------------------------

def _week_from(body: dict) -> str:
    week = body.get("week_start")
    try:
        return _monday(date.fromisoformat(str(week))).isoformat()
    except ValueError:
        raise ValueError("Pass week_start (YYYY-MM-DD).") from None


def _app_call(operation):
    from garmin_ai import db
    with db.get_connection() as conn:
        try:
            return operation(conn)
        except history.HistoryError as error:
            raise ValueError(str(error)) from None


def approve_request(body: dict) -> dict:
    """POST /api/rolling-plan/approve {"week_start", "publish"?: bool}: approve the week's
    proposal and, with publish, send the week to Garmin (publish alone works without one)."""
    week = _week_from(body)
    send = bool(body.get("publish"))

    def run(conn):
        rows = _current(conn, week, week).get(week, [])
        out = {"week_start": week}
        if any(r["status"] in PROPOSAL for r in rows):
            out["decision"] = decide(conn, week, True, "approved in the app")
        elif not send:
            raise history.HistoryError(f"No proposal for the week of {week}.")
        if send:
            out["publish"] = publish(conn, week, dry_run=False)
        return out
    return _app_call(run)


def reject_request(body: dict) -> dict:
    """POST /api/rolling-plan/reject {"week_start", "note"?}: keep the drafts."""
    week = _week_from(body)
    note = str(body["note"])[:300] if body.get("note") else None
    return _app_call(lambda conn: decide(conn, week, False, note))


def publish_request(body: dict) -> dict:
    """POST /api/rolling-plan/publish {"week_start"}: send a week without a proposal to Garmin."""
    week = _week_from(body)
    return _app_call(lambda conn: publish(conn, week, dry_run=False))


# --- plan drift: is the algorithm working? (step 5) ---------------------------------------------

# Which rule made a change, from the reason text the change was stored with.
RULES = (("cap", "Capped at"), ("readiness_red", "Readiness red"), ("readiness_yellow", "Readiness yellow"),
         ("season_update", "The season plan changed"), ("regenerate", "Regenerated"))
FINAL = ("published", "done", "skipped")
QUALITY = {"threshold", "vo2", "race_pace", "sharpen", "time_trial", "quality"}
DAY_NAMES = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def _rules_in(row: dict) -> list[str]:
    if row["origin"] == "agent":
        return ["coach"]
    found = [name for name, text in RULES if text in (row["reason"] or "")]
    return found or ["other"]


def _km(row: dict, paces: dict) -> float:
    return _spec_km(json.loads(row["spec_json"]), paces) if row["sport"] == "run" else 0.0


def drift(conn, start: str | None = None, end: str | None = None, today: date | None = None) -> dict:
    """How the plan moved from the first draft to what was run, from plan_drafts:
    - per week: first-draft km and quality, final (published) km and quality, km run,
      workouts done and skipped, the proposal and its outcome;
    - per rule (cap, readiness_red, readiness_yellow, season_update, regenerate, coach):
      proposals, approved, rejected, and the workouts it changed;
    - per kind: planned, done, skipped;
    - patterns that point to a rule worth changing.
    start/end (YYYY-MM-DD) default to the 12 weeks before today."""
    today = today or date.today()
    last = _monday(date.fromisoformat(end)) if end else _monday(today) - timedelta(days=7)
    first = _monday(date.fromisoformat(start)) if start else last - timedelta(weeks=11)
    if first > last:
        raise history.HistoryError("start must be before end.")
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM plan_drafts WHERE week_start BETWEEN ? AND ? ORDER BY week_start, date, id",
        (first.isoformat(), last.isoformat()))]
    paces = materialize.paces_for(50)
    notes = {r["week_start"]: r["summary"] for r in conn.execute(
        "SELECT week_start, summary FROM coach_log WHERE summary LIKE 'Rejected the proposed changes%' "
        "AND week_start BETWEEN ? AND ?", (first.isoformat(), last.isoformat()))}
    targets = {}
    try:
        targets = {w["week_start"]: w for w in season.plan(conn).get("weeks", [])}
    except Exception:
        pass

    weeks, rules, kinds = [], {}, {}
    skipped_days: dict[int, list] = {}
    for monday in sorted({r["week_start"] for r in rows}):
        week = [r for r in rows if r["week_start"] == monday]
        plan_ids = [r["plan_id"] for r in week if r["origin"] == "generator" and r["plan_id"] is not None]
        original = [r for r in week if r["origin"] == "generator" and not r["parent_id"]
                    and plan_ids and r["plan_id"] == min(plan_ids)]
        final = [r for r in week if r["status"] in FINAL]
        changed = [r for r in week if r["origin"] in ("review", "agent")]
        outcomes = []
        for stamp in sorted({r["created_at"] for r in changed}):  # one proposal per batch
            batch = [r for r in changed if r["created_at"] == stamp]
            statuses = {r["status"] for r in batch}
            outcome = ("pending" if "proposed" in statuses else "rejected" if statuses == {"rejected"} else
                       "approved" if statuses & {"draft", *FINAL} else "replaced")
            outcomes.append(outcome)
            for name in sorted({n for r in batch for n in _rules_in(r)}):
                stat = rules.setdefault(name, {"proposals": 0, "approved": 0, "rejected": 0, "workouts": 0,
                                               "kinds": {}})
                stat["proposals"] += 1
                if outcome in ("approved", "rejected"):
                    stat[outcome] += 1
                for r in batch:
                    if name in _rules_in(r):
                        stat["workouts"] += 1
                        stat["kinds"][r["kind"]] = stat["kinds"].get(r["kind"], 0) + 1
        for r in final:
            k = kinds.setdefault(r["kind"], {"planned": 0, "done": 0, "skipped": 0})
            k["planned"] += 1
            if r["status"] in ("done", "skipped"):
                k[r["status"]] += 1
            if r["status"] == "skipped":
                skipped_days.setdefault(date.fromisoformat(r["date"]).weekday(), []).append(r["kind"])
        run = _run_km(conn, date.fromisoformat(monday)) if date.fromisoformat(monday) <= _monday(today) else None
        entry = {"week_start": monday, "phase": (targets.get(monday) or {}).get("phase"),
                 "target_run_km": (targets.get(monday) or {}).get("run_km"),
                 "first_draft_run_km": round(sum(_km(r, paces) for r in original), 1) if original else None,
                 "first_draft_quality": sum(r["kind"] in QUALITY for r in original) if original else None,
                 "final_run_km": round(sum(_km(r, paces) for r in final), 1) if final else None,
                 "final_quality": sum(r["kind"] in QUALITY for r in final) if final else None,
                 "run_km": run, "done": sum(r["status"] == "done" for r in final),
                 "skipped": sum(r["status"] == "skipped" for r in final),
                 "proposals": outcomes, "proposal_rules": sorted({n for r in changed for n in _rules_in(r)})}
        if monday in notes:
            entry["rejected_note"] = notes[monday]
        weeks.append(entry)

    holds = [h for h in (season.anchor(conn) or {}).get("holds") or []
             if first.isoformat() <= h.get("date", "") <= (last + timedelta(days=6)).isoformat()]
    return {"start": first.isoformat(), "end": last.isoformat(), "weeks": weeks, "rules": rules, "kinds": kinds,
            "holds": holds, "patterns": _patterns(weeks, rules, kinds, skipped_days, holds)}


def _patterns(weeks, rules, kinds, skipped_days, holds) -> list[dict]:
    """Plain-language hints: what keeps happening, and the setting it points to."""
    out = []
    done_weeks = [w for w in weeks if w["final_run_km"] is not None]

    def add(area, message, setting):
        out.append({"area": area, "message": message, "setting": setting})

    for name, stat in sorted(rules.items()):
        if stat["rejected"] >= 2:
            add("rule", f"You rejected {stat['rejected']} of {stat['proposals']} '{name}' proposals.",
                {"cap": "season rebuild_pct / the cap in targets_for",
                 "readiness_red": "rules readiness thresholds",
                 "readiness_yellow": "rules readiness thresholds"}.get(name, "the rule behind it"))
    caps = rules.get("cap", {}).get("proposals", 0)
    if done_weeks and caps >= 3 and caps / len(done_weeks) >= 0.5:
        add("volume", f"Weeks were capped for missed km {caps} times in {len(done_weeks)} weeks: the planned "
                      "volume is more than the time you have.", "season peak_run_km or build_pct")
    under = [w for w in done_weeks if w["run_km"] is not None and w["final_run_km"]
             and w["run_km"] < 0.9 * w["final_run_km"]]
    if len(under) >= 3:
        add("volume", f"You ran under 90% of the published km in {len(under)} weeks.",
            "season peak_run_km or build_pct")
    for kind, stat in sorted(kinds.items()):
        closed = stat["done"] + stat["skipped"]
        if closed >= 3 and stat["skipped"] / closed > 0.3:
            add("sessions", f"{kind}: {stat['skipped']} of {closed} skipped.",
                "the session's place in the week (plan_schedule) or its dose")
    for day, skipped in sorted(skipped_days.items()):
        if len(skipped) >= 3:
            add("schedule", f"{DAY_NAMES[day]}: {len(skipped)} workouts skipped ({', '.join(sorted(set(skipped)))}).",
                "profile plan_schedule")
    longs = rules.get("cap", {}).get("kinds", {}).get("long", 0) + \
        rules.get("readiness_red", {}).get("kinds", {}).get("long", 0)
    if longs >= 3:
        add("long_run", f"The long run was cut {longs} times.", "season long_step_km or long_share")
    by_type: dict[str, int] = {}
    for hold in holds:
        by_type[hold["type"]] = by_type.get(hold["type"], 0) + 1
    for kind, count in sorted(by_type.items()):
        if count >= 2:
            add("progression", f"{kind} sessions were repeated {count} times (session review).",
                "sessions.py ADVANCE_EVERY or that type's paces")
    reds = rules.get("readiness_red", {}).get("proposals", 0)
    if reds >= 2:
        add("readiness", f"Readiness was red on {reds} Sundays: look at what the weeks before had in common.",
            "the dose of the sessions before them")
    return out


def drift_request(query) -> dict:
    """GET /api/rolling-plan/drift?start&end."""
    start, end = (query.get("start") or [None])[0], (query.get("end") or [None])[0]
    try:
        for value in (start, end):
            if value:
                date.fromisoformat(value)
    except ValueError:
        raise ValueError("start and end must be YYYY-MM-DD.") from None
    return _app_call(lambda conn: drift(conn, start, end))


# --- keeping the plan current on its own ----------------------------------------------------------

PROFILE_TRIGGERS = {"swim_css": "css_test", "races": "profile", "season_plan": "profile",
                    "plan_schedule": "profile", "rule_overrides": "rules", "hr_zones": "hr_zones"}
VDOT_STEP = 0.5  # regenerate when current fitness moved this much from the plan's paces


def on_profile_change(conn, section: str) -> dict | None:
    """After a profile section the plan depends on changes (a CSS test, races, the season plan
    and its holds, the schedule, rules): rebuild the open weeks. Only drafts change; weeks on
    Garmin and weeks with a proposal are left alone. Never raises."""
    trigger = PROFILE_TRIGGERS.get(section)
    if not trigger or not conn.execute("SELECT 1 FROM plan_versions LIMIT 1").fetchone():
        return None
    try:
        return regenerate(conn, trigger, dry_run=False, note=f"profile section {section} changed")
    except Exception as error:
        log.warning("Rolling plan regenerate after %s failed: %s", section, error)
        return None


def on_fitness(conn, vdot: float | None) -> dict | None:
    """Rebuild the open weeks when current fitness (VDOT) moved VDOT_STEP or more from the
    plan's paces, e.g. after a race or time trial."""
    latest = _latest_version(conn)
    if not latest or not vdot:
        return None
    planned = json.loads(latest["anchor_json"]).get("vdot")
    if planned and abs(float(vdot) - float(planned)) < VDOT_STEP:
        return None
    return regenerate(conn, "vdot", round(float(vdot), 1), dry_run=False,
                      note=f"fitness moved from VDOT {planned} to {round(float(vdot), 1)}")


# --- the app's Plan screen (/api/plan and /api/plan/move) --------------------------------------

DRAFT_PREFIX = "draft:"


def app_drafts(conn, start: date, end: date, threshold: float | None, today: date | None = None) -> list[dict]:
    """Future drafts as /api/plan workouts (same fields as the ones on Garmin), so the Plan
    screen shows the whole plan. schedule_id is "draft:<id>", workout_id is -id; source "draft"
    tells them apart, and source "plan" marks a published workout kept off the watch (gym)."""
    today = today or date.today()
    easy, _ = own_paces(conn)
    rows = conn.execute("SELECT * FROM plan_drafts WHERE (status IN ('draft', 'proposed_drop') OR "
                        "(status = 'published' AND workout_id = '')) AND date BETWEEN ? AND ? ORDER BY date, name",
                        (max(start, today).isoformat(), end.isoformat())).fetchall()
    out = []
    for row in rows:
        spec = json.loads(row["spec_json"])
        payload = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                         spec.get("pool_length_m"))
        totals = workouts.estimate_totals(payload, 1000 / easy if easy and row["sport"] == "run" else None)
        out.append({
            # workout_id: negative and unique (the app keys rows by it), never a Garmin id
            "schedule_id": f"{DRAFT_PREFIX}{row['id']}", "workout_id": -row["id"], "date": row["date"],
            "name": row["name"],
            "sport": payload["sportType"]["sportTypeKey"], "category": workouts.category(payload, threshold),
            "status": "planned", "matched_activity_id": None, "description": payload.get("description"),
            "duration_s": totals["duration_s"] or None, "distance_m": totals["distance_m"] or None,
            "duration_partial": totals["duration_partial"], "distance_partial": totals["distance_partial"],
            "pool_length_m": payload.get("poolLength"), "steps": workouts.structured_steps(payload),
            "source": "plan" if row["workout_id"] == "" else "draft", "off_watch": row["workout_id"] == "",
            "draft_status": row["status"], "week_start": row["week_start"],
            "kind": row["kind"], "origin": row["origin"], "reason": row["reason"]})
    return out


def app_summary(conn, today: date | None = None) -> dict | None:
    """The rolling plan for the Plan screen: the auto-publish policy, the next week not yet on
    Garmin, and every pending proposal with its reason, km before and after, and the changes
    (for an approve / reject banner)."""
    if not _latest_version(conn):
        return None
    today = today or date.today()
    paces = materialize.paces_for(50)
    next_week = conn.execute(
        "SELECT MIN(week_start) FROM plan_drafts WHERE status = 'draft' AND week_start >= ? AND week_start NOT IN "
        "(SELECT week_start FROM plan_drafts WHERE status = 'published')", (_monday(today).isoformat(),)).fetchone()[0]
    weeks = [r[0] for r in conn.execute(
        "SELECT DISTINCT week_start FROM plan_drafts WHERE status IN ('proposed', 'proposed_drop') ORDER BY 1")]
    proposals = []
    for week in weeks:
        rows = _live_rows(conn, week)
        proposed = [r for r in rows if r["status"] == "proposed"]
        replaced = {r["parent_id"] for r in proposed}
        current = [r for r in rows if r["status"] != "proposed"]
        after = [r for r in rows if r["status"] != "proposed_drop" and r["id"] not in replaced]
        by_id = {r["id"]: r for r in rows}
        km = lambda items: round(sum(_km(r, paces) for r in items), 1)
        proposals.append({
            "week_start": week, "reason": next((r["reason"] for r in proposed if r["reason"]), None),
            "origin": next((r["origin"] for r in proposed), "review"), "run_km": km(current),
            "proposed_run_km": km(after), "on_garmin": any(r["status"] == "published" for r in rows),
            "added": [_brief(r) for r in proposed if r["parent_id"] not in by_id],
            "changed": [f"{_brief(by_id[r['parent_id']])} -> {r['name']}" if by_id[r["parent_id"]]["name"] != r["name"]
                        else _brief(r) for r in proposed if r["parent_id"] in by_id],
            "removed": [_brief(r) for r in rows if r["status"] == "proposed_drop"]})
    return {"auto_publish": policy(history.rules(conn))["auto_publish"], "next_week": next_week,
            "proposals": proposals}


def move_draft(conn, schedule_id: str, day: str, today: date | None = None) -> dict:
    """Move a draft (schedule_id "draft:<id>") to another day, today or later: a new row
    (origin athlete) replaces it, so the move is part of the plan's history."""
    today = today or date.today()
    try:
        row_id = int(str(schedule_id).removeprefix(DRAFT_PREFIX))
        new_day = date.fromisoformat(day)
    except ValueError:
        raise history.HistoryError("Pass a draft schedule_id and a date (YYYY-MM-DD).") from None
    row = conn.execute("SELECT * FROM plan_drafts WHERE id = ?", (row_id,)).fetchone()
    off_watch = row is not None and row["status"] == "published" and row["workout_id"] == ""
    if row is None or (row["status"] != "draft" and not off_watch):
        raise history.HistoryError("Only a draft that is not on Garmin or waiting in a proposal can be moved.")
    if new_day < today:
        raise history.HistoryError("Pick today or a later day.")
    old = row["date"]
    if old == new_day.isoformat():
        return {"schedule_id": schedule_id, "from": old, "date": old}
    week = _monday(new_day).isoformat()
    if not off_watch and conn.execute("SELECT 1 FROM plan_drafts WHERE week_start = ? AND status = 'published' "
                                      "LIMIT 1", (week,)).fetchone():
        raise history.HistoryError("That week is on Garmin; change it with a proposal.")
    if conn.execute("SELECT 1 FROM plan_drafts WHERE date = ? AND name = ? AND status IN ('draft', 'proposed', "
                    "'published')", (new_day.isoformat(), row["name"])).fetchone():
        raise history.HistoryError(f"{row['name']} is already planned on {new_day}.")
    now = _now()
    item = {"date": new_day.isoformat(), "sport": row["sport"], "kind": row["kind"], "name": row["name"],
            "spec": json.loads(row["spec_json"]), "session": json.loads(row["session_json"] or "{}")}
    new_id = _insert(conn, row["plan_id"], week, item, row["status"], "athlete", f"Moved in the app from {old}.",
                     dict(row), now)
    if off_watch:
        conn.execute("UPDATE plan_drafts SET workout_id = '' WHERE id = ?", (new_id,))
    _set_status(conn, [row_id], "superseded", now)
    history.add_entry(conn, "adjustment", f"The athlete moved the draft {row['name']} from {old} to {new_day} "
                      "in the app.", week_start=week, details={"draft": new_id, "from": old, "to": new_day.isoformat()})
    conn.commit()
    return {"schedule_id": f"{DRAFT_PREFIX}{new_id}", "from": old, "date": new_day.isoformat()}


def record_move(conn, workout_id, old_day: str, new_day: str) -> None:
    """A published workout was moved on Garmin (/api/plan/move): keep the plan's row in step."""
    row = conn.execute("SELECT * FROM plan_drafts WHERE workout_id = ? AND date = ? AND status = 'published'",
                       (str(workout_id), old_day)).fetchone()
    if row is None:
        return
    now = _now()
    item = {"date": new_day, "sport": row["sport"], "kind": row["kind"], "name": row["name"],
            "spec": json.loads(row["spec_json"]), "session": json.loads(row["session_json"] or "{}")}
    new_id = _insert(conn, row["plan_id"], _monday(date.fromisoformat(new_day)).isoformat(), item, "published",
                     "athlete", f"Moved in the app from {old_day}.", dict(row), now)
    conn.execute("UPDATE plan_drafts SET workout_id = ? WHERE id = ?", (row["workout_id"], new_id))
    _set_status(conn, [row["id"]], "superseded", now)


# --- heart rate zones from the watch -----------------------------------------------------------

HR_SECTION = "hr_zones"


def zones_from(raw) -> dict | None:
    """Garmin's heart rate zone settings (get_heart_rate_zones) -> {"method", "lthr", "max",
    "floors": [zone 1..5 floors]}, preferring the running zones over the default ones."""
    entries = [z for z in raw or [] if isinstance(z, dict)] if isinstance(raw, list) else [raw]
    pick = (next((z for z in entries if z.get("sport") == "RUNNING"), None)
            or next((z for z in entries if z.get("sport") == "DEFAULT"), None) or (entries[0] if entries else None))
    if not pick:
        return None
    floors = [pick.get(f"zone{i}Floor") for i in range(1, 6)]
    if not all(isinstance(f, (int, float)) and f > 0 for f in floors) or floors != sorted(floors):
        return None
    return {"method": pick.get("trainingMethod"), "lthr": pick.get("lactateThresholdHeartRateUsed"),
            "max": pick.get("maxHeartRateUsed"), "sport": pick.get("sport"), "floors": [int(f) for f in floors]}


def hr_zones(conn) -> dict | None:
    row = conn.execute("SELECT content FROM coach_profile WHERE section = ?", (HR_SECTION,)).fetchone()
    try:
        return json.loads(row["content"]) if row else None
    except ValueError:
        return None


def refresh_hr_zones(conn, fetch=None) -> dict | None:
    """Read the watch's heart rate zones from Garmin and keep them in the profile (hr_zones).
    A change rebuilds the plan's drafts, so easy runs follow the zones set on the watch (e.g.
    Garmin's lactate threshold zones). Never raises; returns the zones or None."""
    try:
        if fetch is None:
            from garmin_ai import personal_api
            fetch = lambda: personal_api.call(lambda client: client.get_heart_rate_zones())
        zones = zones_from(fetch())
    except Exception as error:
        log.warning("Could not read heart rate zones: %s", error)
        return None
    if zones and zones != hr_zones(conn):
        how = f"{zones['method'] or 'Garmin'} zones" + (f", LTHR {zones['lthr']}" if zones.get("lthr") else "")
        history.set_profile(conn, HR_SECTION, json.dumps(zones, sort_keys=True),
                            f"Heart rate zones from Garmin ({how}): floors {zones['floors']}.")
        conn.commit()
    return zones


# --- weeks planned before the rolling plan ----------------------------------------------------

def _outside_plan(conn, week_start: str) -> bool:
    """The week has [TP] workouts on Garmin and no rolling-plan rows (planned by hand)."""
    end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
    on_garmin = conn.execute("SELECT 1 FROM coach_schedules WHERE date BETWEEN ? AND ? LIMIT 1",
                             (week_start, end)).fetchone()
    in_plan = conn.execute("SELECT 1 FROM plan_drafts WHERE week_start = ? AND status != 'superseded' LIMIT 1",
                           (week_start,)).fetchone()
    return bool(on_garmin and not in_plan)


def adopt_week(conn, monday: str, dry_run: bool = True, today: date | None = None) -> dict:
    """Bring a week planned before the rolling plan (its [TP] workouts already on Garmin) into
    the plan as published rows (origin agent), so the Sunday review, mid-week changes, done /
    skipped and the drift report cover it. Nothing on Garmin changes. Sports kept off the watch
    (rolling.watch_sports) are listed so they can be taken off Garmin with keep_off_watch."""
    week_start = _monday(date.fromisoformat(monday)).isoformat()
    version = _latest_version(conn)
    if not version:
        raise history.HistoryError("No rolling plan yet; create it with regenerate_plan.")
    if not _outside_plan(conn, week_start):
        raise history.HistoryError(f"Week {week_start} has nothing to adopt: no [TP] workouts on Garmin, or it is "
                                   "already in the rolling plan.")
    end = (date.fromisoformat(week_start) + timedelta(days=6)).isoformat()
    rows = conn.execute(
        """SELECT s.schedule_id, s.date, w.workout_id, w.payload_json FROM coach_schedules s
           JOIN coach_workouts w ON w.workout_id = s.workout_id WHERE s.date BETWEEN ? AND ? ORDER BY s.date""",
        (week_start, end)).fetchall()
    paces = materialize.paces_for(json.loads(version["anchor_json"]).get("vdot") or 50)
    easy, swim_pace = own_paces(conn)
    watch = policy(history.rules(conn))["watch_sports"]
    items = []
    for row in rows:
        payload = json.loads(row["payload_json"])
        spec = workouts.spec_from_payload(payload)
        rebuilt = workouts.build_workout(spec["sport"], spec["name"], spec["steps"], spec.get("description"),
                                         spec.get("pool_length_m"))
        if workouts.canonical_steps(rebuilt) != workouts.canonical_steps(payload):
            raise history.HistoryError(f"{row['date']} {spec['name']}: could not read its steps back exactly.")
        session = workouts.to_session(row["date"], payload, paces, None, swim_pace, easy)
        sport = workouts.VALIDATOR_SPORTS.get(workouts.sport_key(spec["sport"]), "other")
        items.append({"date": row["date"], "sport": sport, "kind": _kind({**session, "sport": sport}),
                      "name": spec["name"], "spec": spec, "session": {}, "workout_id": str(row["workout_id"])})
    result = {"dry_run": dry_run, "week_start": week_start,
              "adopted": [f"{i['date']} {i['name']} ({i['kind']})" for i in items],
              "on_garmin_but_kept_off_the_watch": [f"{i['date']} {i['name']}" for i in items if i["sport"] not in watch]}
    if dry_run:
        return result
    now = _now()
    for item in items:
        new_id = _insert(conn, version["id"], week_start, item, "published", "agent",
                         "Planned before the rolling plan; adopted as it was on Garmin.", None, now)
        conn.execute("UPDATE plan_drafts SET workout_id = ? WHERE id = ?", (item["workout_id"], new_id))
    close_days(conn, today or date.today())
    history.add_entry(conn, "plan", f"Adopted the week of {week_start} into the rolling plan: {len(items)} workouts "
                      "already on Garmin, unchanged.", week_start=week_start)
    conn.commit()
    return result


def keep_off_watch(conn, monday: str, dry_run: bool = True, today: date | None = None) -> dict:
    """Take a week's workouts in sports kept off the watch (rolling.watch_sports) off Garmin
    from today on: the [TP] workout is deleted there and the plan keeps it as plan-only."""
    from garmin_ai import coach
    week_start = _monday(date.fromisoformat(monday)).isoformat()
    first = max(week_start, (today or date.today()).isoformat())
    watch = policy(history.rules(conn))["watch_sports"]
    rows = [r for r in _live_rows(conn, week_start)
            if r["status"] == "published" and r["workout_id"] and r["sport"] not in watch and r["date"] >= first]
    result = {"dry_run": dry_run, "week_start": week_start, "off_watch": [_brief(r) for r in rows], "errors": []}
    if dry_run:
        return result
    conn.commit()
    for row in rows:
        try:
            coach.delete_workout(row["workout_id"])
        except Exception as error:
            result["errors"].append(f"{_brief(row)}: {error}")
            continue
        now = _now()
        item = {"date": row["date"], "sport": row["sport"], "kind": row["kind"], "name": row["name"],
                "spec": json.loads(row["spec_json"]), "session": json.loads(row["session_json"] or "{}")}
        new_id = _insert(conn, row["plan_id"], week_start, item, "published", "athlete",
                         "Kept off the watch (plan only).", row, now)
        conn.execute("UPDATE plan_drafts SET workout_id = '' WHERE id = ?", (new_id,))
        _set_status(conn, [row["id"]], "superseded", now)
    if rows:
        history.add_entry(conn, "adjustment", f"Took {len(rows) - len(result['errors'])} workout(s) off the watch for "
                          f"the week of {week_start} (kept in the plan): " + ", ".join(result["off_watch"])[:300],
                          week_start=week_start)
    conn.commit()
    return result
