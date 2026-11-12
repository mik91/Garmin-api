"""MCP server for Claude. Run locally with python -m garmin_ai.mcp_server (read-only).

The hosted server at /mcp also offers the workout write tools (create_workout,
upload_plan, ...), which change Garmin Connect; see garmin_ai/coach.py.

Two kinds of read tools:
- History tools read data/garmin.db (opened read-only), including metrics the official API
  lacks (training readiness and status, VO2 max, race predictions, the training calendar).
  Keep it fresh with the web app's auto-sync or `python cli.py sync`.
- garmin_* tools call Garmin's official Personal API live, so they are always current.
"""
import json
import sqlite3
from contextlib import closing
from datetime import date, timedelta
from pathlib import Path

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations

from garmin_ai import coach, config, db, hevy, load_summary, personal_api, web_dashboard, workouts
from garmin_ai.coaching import (assess_readiness, efficiency, history, load_rules, progress, review, season,
                                swim,
                                validate_week)


TOOLS = []
# Tools that change Garmin Connect. Only the hosted server offers them, so there is one
# record (on the server) of which workouts the coach created.
WRITE_TOOLS = []


def tool(function):
    """Register a tool function; servers are built from TOOLS by build_server()."""
    TOOLS.append(function)
    return function


def write_tool(destructive: bool = False, idempotent: bool = False, read_only: bool = False,
               open_world: bool = True):
    """Register a hosted-only tool (the coach's workout and coaching-history tools)."""
    def register(function):
        annotations = (ToolAnnotations(readOnlyHint=True) if read_only else
                       ToolAnnotations(readOnlyHint=False, destructiveHint=destructive,
                                       idempotentHint=idempotent, openWorldHint=open_world))
        WRITE_TOOLS.append((function, annotations))
        return function
    return register


INSTRUCTIONS = """Garmin training data and an endurance coach. Method (docs/coaching-method.md):
Weekly review: season_progress (flags and verdict), then week_review of the week.
To plan a week: 1) athlete_profile (races, season phase, cautions), coaching_history and
the week's target from season_plan (volume, long run, quality sessions, swim);
2) week_review of last week (planned vs done), easy_pace_trend and swim_trend; 3) weekly_load_summary;
4) check_readiness and apply its actions; 5) fresh training_paces and garmin_heart_rate_zones;
6) draft all sports for the phase; 7) upload_plan(dry_run=true): it validates every week against
the coaching rules; fix all errors, show warnings and a day/sport/session/purpose table;
8) upload only after the athlete says OK; 9) log_coaching_decision (plan, readiness, why).
Mid-week change (sick, sore, missed): do not make up missed sessions; adjust the rest of the
week, dry-run, show the diff, upload after OK, log an adjustment. Ask before adding a
session, changing a goal, skipping a lighter week or moving the long run. Easy runs get an
HR range only, no pace. A flexible slot (rules structure.flex) is planned as optional "(opt)"
in the sport the validator prefers for the phase; the athlete decides on the day.
Swim sets use zones from swim_paces (CSS); after a CSS test, save it with record_css_test.
Only [CP] workouts are ever changed. Keep reasons to 2-3 sentences.
Workout writes, the coaching history and the profile are on the hosted connector only."""


def build_server(hosted: bool = False) -> FastMCP:
    server = (_hosted_server() if hosted else
              FastMCP("garmin-insights", instructions=INSTRUCTIONS))  # local: stdio for Claude Desktop
    for function in TOOLS:
        server.add_tool(function, annotations=ToolAnnotations(readOnlyHint=True))
    if hosted:
        for function, annotations in WRITE_TOOLS:
            server.add_tool(function, annotations=annotations)
    return server


def _hosted_server() -> FastMCP:
    # Hosted: served over HTTPS at /mcp by the web server, with its own OAuth endpoints.
    from urllib.parse import urlsplit

    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions, RevocationOptions
    from mcp.server.transport_security import TransportSecuritySettings

    from garmin_ai import auth
    host = urlsplit(config.PUBLIC_URL).netloc
    return FastMCP(
        "garmin-insights",
        instructions=INSTRUCTIONS,
        auth_server_provider=auth.OAuthProvider(),
        auth=AuthSettings(
            issuer_url=config.PUBLIC_URL,
            resource_server_url=f"{config.PUBLIC_URL}/mcp",
            client_registration_options=ClientRegistrationOptions(enabled=True),
            revocation_options=RevocationOptions(enabled=True),
            validate_token_resource=False,
        ),
        host="0.0.0.0",
        stateless_http=True,
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=True, allowed_hosts=[host], allowed_origins=[config.PUBLIC_URL]),
    )


MAX_ROWS = 500
MAX_LIVE_CHARS = 80_000  # keep large Garmin payloads from flooding the context
ACTIVITY_FIELDS = ("activity_id", "start_time", "name", "type", "category", "duration_s", "distance_m",
                   "avg_hr", "max_hr", "elevation_gain_m", "training_load", "aerobic_te", "anaerobic_te")


def _connect():
    return closing(sqlite3.connect(Path(config.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True))


def _range(start: str | None, end: str | None, default_days: int = 28) -> dict:
    end = end or date.today().isoformat()
    start = start or (date.fromisoformat(end) - timedelta(days=default_days - 1)).isoformat()
    return {"start": [start], "end": [end]}


def _dump(value) -> str:
    return json.dumps(value, allow_nan=False, default=str)


@tool
def sync_status() -> str:
    """When the local history was last synced from Garmin, and the date range it covers."""
    with _connect() as conn:
        state = dict(conn.execute("SELECT key, value FROM sync_state").fetchall())
        first, last = conn.execute("SELECT MIN(date), MAX(date) FROM daily_stats").fetchone()
    return _dump({"today": date.today().isoformat(), "first_day": first, "last_day": last, **state})


@tool
def coaching_summary(start: str | None = None, end: str | None = None) -> str:
    """The same summary the dashboard's AI coach receives: rule-based insights, personal
    goals, race-prediction model and recent daily/sleep trends. Dates are YYYY-MM-DD;
    defaults to the last 28 days."""
    query = _range(start, end)
    return _dump(web_dashboard.chat_summary({"start": query["start"][0], "end": query["end"][0]}))


@tool
def list_activities(start: str | None = None, end: str | None = None,
                    activity_type: str | None = None, limit: int = 50) -> str:
    """From the local synced history: activities in a date range (default last 28 days), newest first. activity_type matches
    Garmin's type key (e.g. running, trail_running, cycling, strength_training)."""
    activities = web_dashboard.payload(_range(start, end))["activities"]
    if activity_type:
        activities = [a for a in activities if a.get("type") == activity_type]
    rows = [{key: a.get(key) for key in ACTIVITY_FIELDS} for a in reversed(activities)]
    return _dump(rows[:max(1, min(limit, MAX_ROWS))])


@tool
def get_activity(activity_id: int) -> str:
    """From the local synced history: full stored Garmin payload for one activity (from list_activities)."""
    return _dump(web_dashboard.activity_raw({"id": [str(activity_id)]}))


@tool
def daily_metrics(start: str | None = None, end: str | None = None) -> str:
    """From the local synced history: per-day wellness (steps, resting HR, stress, Body Battery, HRV, training readiness and
    status, VO2 max, recovery time) and sleep (stages, score) for a date range."""
    data = web_dashboard.payload(_range(start, end))
    return _dump({"daily_stats": data["daily_stats"], "sleep": data["sleep"],
                  "body_composition": data["body_composition"]})


@tool
def weekly_load_summary(weeks: int = 12) -> str:
    """From the local synced history: training load per Monday-Sunday week for the last
    `weeks` complete weeks (default 12, max 52, oldest first), plus the current week so far.
    Per week: running distance_km, runs, longest_run_km, longest_run_share_pct,
    change_vs_previous_week_pct; hours, sessions and distance per sport (running, cycling,
    swimming, strength, other); quality_sessions (Garmin training-effect label TEMPO,
    LACTATE_THRESHOLD, VO2MAX, ANAEROBIC_CAPACITY or SPEED) with a count per label;
    minutes in HR zones 4-5; Garmin training load. The summary has 12-week averages and the
    last 4 weeks vs the 4 before. Use this instead of query_database for volume and load
    questions and before planning a week."""
    with _connect() as conn:
        return _dump(load_summary.weekly_load(conn, weeks))


@tool
def race_predictions() -> str:
    """Garmin's latest race predictions alongside the app's independent model."""
    return _dump(web_dashboard.payload(_range(None, None, 1))["race_model"])


@tool
def training_calendar(start: str | None = None, end: str | None = None) -> str:
    """From the local synced history: planned workouts with completion status (completed, missed, skipped...) in a date range.
    Defaults to two weeks back through four weeks ahead. created_by_coach marks [CP] workouts
    from the coach's write tools; the others come from Runna, Garmin Coach or manual planning."""
    today = date.today()
    start = start or (today - timedelta(days=14)).isoformat()
    end = end or (today + timedelta(days=28)).isoformat()
    plans = web_dashboard.payload(_range(start, end))["calendar"]
    plans = [p for p in plans if start <= str(p.get("date", ""))[:10] <= end]
    coach_schedules = _coach_schedule_ids()
    for plan in plans:
        plan["created_by_coach"] = str(plan.get("scheduled_workout_id")) in coach_schedules
    return _dump(plans)


def _coach_schedule_ids() -> set[str]:
    with _connect() as conn:
        try:
            return {row[0] for row in conn.execute("SELECT schedule_id FROM coach_schedules")}
        except sqlite3.Error:  # an older database without the coach tables
            return set()


@tool
def strength_workouts(start: str | None = None, end: str | None = None) -> str:
    """From the local synced history: Hevy lifting workouts in a date range (default last 28
    days), newest first, with exercises (muscle groups) and every set (weight_kg, reps, RPE,
    type), the overlapping Garmin strength activity (avg/max HR, training effect), and weekly
    totals. Warm-up sets are listed but not counted in sets or volume_kg (weight x reps)."""
    return _dump(hevy.payload(_range(start, end)))


@tool
def exercise_progress(template_id: str) -> str:
    """From the local synced history: every Hevy session of one exercise across all history,
    oldest first, with working-set totals, best set, estimated 1RM (Epley, 1-12 reps), max reps
    and duration, plus all-time records. template_id comes from strength_workouts
    (exercises[].template_id). Weights are kg; weight_unit is the unit the user logs in."""
    try:
        return _dump(hevy.exercise_progress({"template_id": [template_id]}))
    except hevy.NotFound as error:
        return str(error)


@tool
def query_database(sql: str) -> str:
    """Run one read-only SQL SELECT against the local history (SQLite). Tables: activities,
    daily_stats, sleep, body_composition, race_predictions, scheduled_workouts,
    adherence_overrides, goals, sync_state, coach_workouts, coach_schedules,
    coach_log, coach_profile, hevy_workouts (garmin_activity_id links to
    activities), hevy_exercises, hevy_sets, hevy_exercise_templates. Avoid selecting raw_json
    unless needed.
    Returns at most 500 rows."""
    with _connect() as conn:
        conn.execute("PRAGMA query_only = ON")
        try:
            cursor = conn.execute(sql)
        except sqlite3.Error as error:
            return f"SQL error: {error}"
        columns = [c[0] for c in cursor.description or ()]
        rows = cursor.fetchmany(MAX_ROWS + 1)
    result = {"columns": columns, "rows": rows[:MAX_ROWS]}
    if len(rows) > MAX_ROWS:
        result["truncated"] = True
    return _dump(result)


def _live(operation) -> str:
    try:
        text = _dump(personal_api.call(operation))
    except personal_api.PersonalApiUnavailable as error:
        return str(error)
    if len(text) > MAX_LIVE_CHARS:
        text = text[:MAX_LIVE_CHARS] + f"... [truncated from {len(text)} characters; use a narrower date range]"
    return text


def _days(start: str | None, end: str | None, default_days: int = 7) -> tuple[str, str]:
    query = _range(start, end, default_days)
    return query["start"][0], query["end"][0]


# Account, privacy, location and bookkeeping fields that say nothing about the training.
_DROPPED_ACTIVITY_FIELDS = {
    "activityUUID", "beginTimestamp", "startTimeGMT", "endTimeGMT", "eventType", "privacy",
    "manufacturer", "sportTypeId", "summarizedDiveInfo", "startLatitude", "startLongitude",
    "endLatitude", "endLongitude", "aerobicTrainingEffectMessage", "anaerobicTrainingEffectMessage",
    "minActivityLapDuration", "qualifyingDive", "userRoles", "deviceId",
}
_SPLIT_FIELDS = ("splitType", "noOfSplits", "distance", "duration", "averageSpeed", "totalAscent")


def _round(value):
    return round(value, 2) if isinstance(value, float) else value


def _pace(speed_ms) -> str | None:
    if not isinstance(speed_ms, (int, float)) or speed_ms <= 0:
        return None
    seconds = round(1000 / speed_ms)
    return f"{seconds // 60}:{seconds % 60:02d}"


def compact_activity(activity: dict) -> dict:
    """Keep every training metric (running dynamics, power, zones, splits) but drop
    account/location noise, false flags and float noise, so many more activities fit."""
    result = {}
    for key, value in activity.items():
        if key in _DROPPED_ACTIVITY_FIELDS or key.startswith("owner") or value is None:
            continue
        if isinstance(value, bool):
            if value and key in ("pr", "isPR", "manualActivity", "isManualActivity"):
                result[key] = True
            continue
        if key == "activityType" and isinstance(value, dict):
            result["type"] = value.get("typeKey")
        elif key == "splitSummaries" and isinstance(value, list):
            result[key] = [{field: _round(split.get(field)) for field in _SPLIT_FIELDS if split.get(field) is not None}
                           for split in value if isinstance(split, dict)]
        elif not isinstance(value, (dict, list)):
            result[key] = _round(value)
    for source, name in (("averageSpeed", "pace_per_km"), ("avgGradeAdjustedSpeed", "grade_adjusted_pace_per_km")):
        pace = _pace(activity.get(source))
        if pace:
            result[name] = pace
    return result


@tool
def garmin_activities(start: str | None = None, end: str | None = None,
                      activity_type: str | None = None, limit: int = 20) -> str:
    """Live from Garmin: activity summaries in a date range (default last 28 days), newest
    first, including running dynamics (stride length cm, vertical oscillation cm, vertical
    ratio %, ground contact time ms, cadence spm), power, HR/power time in zones (seconds),
    fastest splits (seconds) and training effect/load. Units: distance m, duration s,
    speed m/s; pace_per_km is min:sec. activity_type is a Garmin type key such as running,
    trail_running, lap_swimming or cycling."""
    start, end = _days(start, end, 28)

    def operation(client):
        activities = client.search_activities(start_date=start, end_date=end, activity_type=activity_type,
                                              limit=max(1, min(limit, 100)))
        kept, size = [], 0
        compacted = [compact_activity(a) for a in activities or [] if isinstance(a, dict)]
        for activity in compacted:
            size += len(_dump(activity)) + 2
            if size > MAX_LIVE_CHARS - 500:
                break
            kept.append(activity)
        if len(kept) == len(compacted):
            return kept
        return {"activities": kept, "omitted_older_activities": len(compacted) - len(kept),
                "note": "Size limit reached; request the omitted period with an earlier end date."}

    return _live(operation)


@tool
def garmin_activity(activity_id: int) -> str:
    """Live from Garmin: full details for one activity."""
    return _live(lambda c: c.get_activity(activity_id))


@tool
def garmin_activity_workout(activity_id: int) -> str:
    """Live from Garmin: the structured workout(s) linked to an activity."""
    return _live(lambda c: c.get_activity_workouts(activity_id))


@tool
def garmin_daily_summaries(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: daily summaries (steps, calories, intensity minutes, resting HR,
    stress, Body Battery) for a date range (default last 7 days)."""
    start, end = _days(start, end)
    return _live(lambda c: c.get_daily_summaries(start, end))


@tool
def garmin_sleep(date: str | None = None) -> str:
    """Live from Garmin: detailed sleep (stages, score, overnight HR/HRV) for the night ending
    on date (default today)."""
    return _live(lambda c: c.get_daily_sleep(date or _date_today()))


@tool
def garmin_sleep_summaries(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: per-night sleep summaries for a date range (default last 7 days)."""
    start, end = _days(start, end)
    return _live(lambda c: c.get_daily_sleep_summaries(start, end))


@tool
def garmin_hrv(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: daily HRV summaries (nightly average, baseline, status) for a date
    range (default last 7 days)."""
    start, end = _days(start, end)
    return _live(lambda c: c.get_daily_hrv_summaries(start, end))


@tool
def garmin_body_battery(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: Body Battery for a date range (default last 7 days)."""
    start, end = _days(start, end)
    return _live(lambda c: c.get_daily_body_battery(start, end))


@tool
def garmin_stress(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: daily stress statistics for a date range (default last 7 days)."""
    start, end = _days(start, end)
    return _live(lambda c: c.get_daily_stress(start, end))


@tool
def garmin_heart_rate(date: str | None = None) -> str:
    """Live from Garmin: heart rate for one day (resting, min/max, samples). Default today."""
    return _live(lambda c: c.get_daily_heart_rate(date or _date_today()))


@tool
def garmin_heart_rate_zones() -> str:
    """Live from Garmin: current heart rate zones."""
    return _live(lambda c: c.get_heart_rate_zones())


@tool
def garmin_spo2(date: str | None = None) -> str:
    """Live from Garmin: SpO2 and acclimation for one day (default today)."""
    return _live(lambda c: c.get_daily_spo2(date or _date_today()))


@tool
def garmin_weight(start: str | None = None, end: str | None = None) -> str:
    """Live from Garmin: weight and body composition for a date range (default last 90 days)."""
    start, end = _days(start, end, 90)
    return _live(lambda c: c.get_weight_measurements(start, end))


@tool
def garmin_fitness_age(date: str | None = None) -> str:
    """Live from Garmin: fitness age for one day (default today)."""
    return _live(lambda c: c.get_fitness_age(date or _date_today()))


def _date_today() -> str:
    return date.today().isoformat()


# --- coach: training paces and workout writes -------------------------------------------

def _seconds(value) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    parts = [int(p) for p in str(value).strip().split(":")]
    if not 2 <= len(parts) <= 3:
        raise ValueError(f"Race time {value!r} must be seconds, 'mm:ss' or 'h:mm:ss'.")
    return float(sum(p * 60 ** i for i, p in enumerate(reversed(parts))))


@tool
def training_paces(race_distance_m: float | None = None, race_time: str | None = None) -> str:
    """Daniels-style (VDOT) training paces in min:sec per km: easy, marathon, threshold,
    interval and repetition. By default they come from the app's current 5K prediction
    (race_predictions); pass a recent race to use that instead, e.g.
    race_distance_m=10000, race_time="41:30". Use these paces for pace targets in
    create_workout/upload_plan; heart-rate zones come from garmin_heart_rate_zones."""
    if race_distance_m and race_time:
        try:
            result = workouts.training_paces(float(race_distance_m), _seconds(race_time))
        except ValueError as error:
            return str(error)
        return _dump({**result, "based_on": f"{race_distance_m:g} m in {race_time}"})
    paces = coach.current_paces()
    return _dump(paces) if paces else "No 5K prediction is available yet; pass race_distance_m and race_time."


STEP_SCHEMA = """
Step schema (one object per step; units are always metric):
  {"type": "warmup" | "interval" | "recovery" | "rest" | "cooldown" | "repeat",
   "duration": {"time_s": 600} | {"distance_m": 1000} | {"lap_button": true} | {"reps": 12},
   "target": {"pace_min_per_km": ["4:50", "5:05"]} | {"hr_zone": 2} | {"hr_bpm": [135, 150]}
             | {"power_w": [180, 210]} | {"cadence": [85, 95]} | "none",
   "repeat": {"count": 4, "steps": [...], "skip_last_rest": false},   # only for type "repeat"
   "swim": {"stroke": "free", "drill": "kick", "equipment": "kickboard"},  # swim steps only
   "notes": "shown on the watch"}
Rules:
- duration has exactly one key. reps is strength only. distance_m is not for strength.
- pace_min_per_km is running only, as ["m:ss", "m:ss"] in either order (fast and slow end).
- power_w is cycling only (absolute watts, e.g. on the indoor trainer). hr_zone is 1-5
  (the watch's own zones); hr_bpm is a custom range. Omit target or use "none" for no target.
- rest steps have no target. Repeats cannot be nested.
- Swimming: pool_length_m defaults to 25. Swim distances must be whole pool lengths. Rest is
  {"time_s": 20} (fixed rest) or {"lap_button": true}. stroke: free, backstroke, breaststroke,
  fly, individual_medley, mixed, drill, any_stroke. drill: kick, pull, drill. equipment: fins,
  kickboard, paddles, pull_buoy, snorkel. skip_last_rest drops the rest after the last rep.
- Strength: use time_s, reps or lap_button, and put the exercise and weight in notes.
- Names get the "[CP] " prefix automatically.
Examples:
  Run intervals: [{"type": "warmup", "duration": {"time_s": 600}, "target": {"hr_zone": 2}},
    {"type": "repeat", "repeat": {"count": 4, "steps": [
      {"type": "interval", "duration": {"time_s": 60}, "target": {"pace_min_per_km": ["3:50", "4:00"]}},
      {"type": "recovery", "duration": {"time_s": 90}}]}},
    {"type": "cooldown", "duration": {"distance_m": 1000}}]
  Swim (sport lap_swimming): [{"type": "warmup", "duration": {"distance_m": 200}, "swim": {"stroke": "free"}},
    {"type": "repeat", "repeat": {"count": 4, "skip_last_rest": true, "steps": [
      {"type": "interval", "duration": {"distance_m": 50}, "swim": {"stroke": "free"}},
      {"type": "rest", "duration": {"time_s": 20}}]}},
    {"type": "cooldown", "duration": {"distance_m": 100}, "swim": {"stroke": "any_stroke"}}]
  Trainer ride (sport cycling): [{"type": "warmup", "duration": {"time_s": 600}, "target": {"power_w": [120, 150]}},
    {"type": "interval", "duration": {"time_s": 1200}, "target": {"power_w": [200, 220]}},
    {"type": "cooldown", "duration": {"time_s": 300}, "target": {"hr_zone": 1}}]
  Gym (sport strength): [{"type": "interval", "duration": {"reps": 8}, "notes": "Back squat 3x8 @ 80 kg"},
    {"type": "rest", "duration": {"time_s": 120}}]
"""


def _coach_call(operation) -> str:
    try:
        return _dump(operation())
    except (coach.CoachError, workouts.WorkoutError) as error:
        return f"Error: {error}"
    except Exception as error:  # Garmin HTTP errors and the like
        return f"Error: Garmin Connect request failed: {error}"


@write_tool()
def create_workout(sport: str, name: str, steps: list[dict], description: str | None = None,
                   pool_length_m: float | None = None, date: str | None = None, dry_run: bool = False) -> str:
    """Create a structured workout in Garmin Connect (it syncs to the watch). sport is
    running, cycling, lap_swimming or strength. Optionally pass date (YYYY-MM-DD) to also
    schedule it. dry_run=true returns the Garmin payload and a readable summary without
    uploading; use it first when unsure. After upload the workout is read back from Garmin
    and any difference is returned as an error. Returns workout_id (and schedule_id)."""
    return _coach_call(lambda: coach.create_workout(sport, name, steps, description, pool_length_m, date, dry_run))


@write_tool(idempotent=False)
def schedule_workout(workout_id: int, date: str) -> str:
    """Put a [CP] workout (from create_workout or list_planned_workouts) on the Garmin
    calendar on date (YYYY-MM-DD). Only workouts these tools created can be scheduled."""
    return _coach_call(lambda: coach.schedule_workout(workout_id, date))


@write_tool(idempotent=True)
def upload_plan(items: list[dict], dry_run: bool = False, allow_errors: bool = False) -> str:
    """Create and schedule several workouts (typically a week) in one call.
    items: [{"date": "YYYY-MM-DD", "workout": {"sport", "name", "steps", "description"?,
    "pool_length_m"?}, "session"?: {...}}]. Safe to repeat: a [CP] workout already on that date
    with the same name is left alone if unchanged, or updated in place if it changed.
    Every week the items touch is checked with the coaching rules (same checks as
    validate_week_plan), together with the [CP] workouts already scheduled that week. Minutes,
    run km and intensity are estimated from the steps (hard = HR zone 4+ or near threshold
    pace; a run named "long", else the longest 60+ min easy run, is the long run; names with
    race / time trial / all-out are all-out; set session all_out for a CSS or 5K test; swim
    distance uses rules swim.default_pace_s_per_100m). "session" corrects the estimate or adds what the
    steps cannot show: slot ("midday"), kind ("long"), intensity, all_out, hard_minutes, km,
    minutes. Rule errors block a real upload; fix them, or pass allow_errors=true only after
    the athlete agreed. Warnings (and easy steps paced faster than marathon pace) never
    block. Use dry_run=true first and show items, errors and warnings to the athlete."""
    return _coach_call(lambda: coach.upload_plan(items, dry_run, allow_errors))


@write_tool(idempotent=True)
def update_workout(workout_id: int, sport: str | None = None, name: str | None = None,
                   steps: list[dict] | None = None, description: str | None = None,
                   pool_length_m: float | None = None, dry_run: bool = False) -> str:
    """Change a [CP] workout these tools created. Fields left out keep their current value;
    steps, when given, replace all steps. Scheduled dates stay. Refuses any other workout."""
    return _coach_call(lambda: coach.update_workout(workout_id, sport, name, steps, description,
                                                    pool_length_m, dry_run))


@write_tool(destructive=True, idempotent=True)
def unschedule_workout(date: str | None = None, schedule_id: str | None = None) -> str:
    """Remove [CP] workouts from the Garmin calendar, by date (YYYY-MM-DD, removes every [CP]
    workout that day) or by one schedule_id. The workout itself stays in the library.
    Workouts from other sources (Runna, manual) on that date are never touched."""
    return _coach_call(lambda: coach.unschedule(date, schedule_id))


@write_tool(destructive=True, idempotent=True)
def delete_workout(workout_id: int) -> str:
    """Delete a [CP] workout these tools created, and its calendar entries. Refuses any
    workout these tools did not create."""
    return _coach_call(lambda: coach.delete_workout(workout_id))


for _function in (create_workout, update_workout):
    _function.__doc__ += "\n" + STEP_SCHEMA
upload_plan.__doc__ += "\nEach workout's steps use the same schema as create_workout:\n" + STEP_SCHEMA


@write_tool(read_only=True)
def list_planned_workouts(start: str | None = None, end: str | None = None) -> str:
    """Only the [CP] workouts the coach tools created, with date, schedule_id, workout_id,
    a readable summary and completion status (planned, completed, missed...). Defaults to
    two weeks back through six weeks ahead. Other planned workouts are in training_calendar."""
    return _coach_call(lambda: coach.list_planned(start, end))


# --- coaching guardrails and history (method: docs/coaching-method.md) ------------------

def _rules(conn) -> dict:
    try:
        return history.rules(conn)
    except sqlite3.Error:  # an older database without the coaching tables
        return load_rules()


@tool
def check_readiness(as_of: str | None = None, reported: list[str] | None = None) -> str:
    """Readiness gate before planning or changing a week: green, yellow, red or unknown, from
    the local synced history (overnight HRV and resting HR, 7-day median vs the prior 21 days;
    last 3 nights' sleep; latest Garmin training readiness), with the signals, points and the
    fixed actions to apply to the plan. as_of defaults to today. reported: ["sick"], ["pain"]
    or ["injury"] when the athlete says so; any of them forces red. Other feelings (tired,
    heavy legs) are the athlete's call: ask how they want to adjust. Status unknown means too
    little data: plan conservatively and say so."""
    as_of = as_of or _date_today()
    try:
        date.fromisoformat(as_of)
    except ValueError:
        return "as_of must be YYYY-MM-DD."
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rows = history.daily_rows(conn, as_of)
        result = assess_readiness(rows, as_of, reported or [], _rules(conn))
    return _dump({"as_of": as_of, "days_of_data": len(rows), **result})


@tool
def validate_week_plan(plan: dict, history_override: dict | None = None) -> str:
    """Check a drafted week against the coaching rules before showing or uploading it.
    plan = {"week_start": "YYYY-MM-DD" (Monday), "sessions": [{"date", "sport": run|swim|bike|gym,
    "minutes", "km" (runs, required), "intensity": easy|quality|strides|race,
    "kind": "long" (the long run), "slot": "midday" (optional), "hard_minutes" (optional),
    "pace_target": true when an easy run has a pace target, "all_out": true for a time trial}]}.
    History (weekly run km for 12 weeks, longest run in 30 days, weeks since a lighter week,
    last quality and race dates) is filled from the synced activities; pass history_override
    to correct any of it, e.g. {"weeks_since_light": 1}. Checks: weekly km ramp vs last week
    and the 12-week peak, lighter week every 4 weeks, max 2 quality sessions and none on
    consecutive days, 4+ weeks between all-out efforts, 80% easy time, long-run share and
    length, midday swim length, no pace target on easy runs, default sessions per sport.
    Fix every error; show warnings to the athlete. Rules come from rules.json plus the
    athlete profile's rule_overrides section. upload_plan runs the same checks on the real
    workouts, so use this tool for drafts that are not workouts yet."""
    try:
        week_start = date.fromisoformat(str(plan.get("week_start"))[:10]).isoformat()
    except (AttributeError, ValueError):
        return 'plan needs "week_start" as YYYY-MM-DD and "sessions".'
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        rules = _rules(conn)
        filled = history.plan_history(conn, week_start, rules["light_week"]["reduction_pct_min"],
                                      rules["ramp"]["peak_lookback_weeks"])
        try:
            phase = review.season(conn, week_start, rules)
        except sqlite3.Error:
            phase = None
    if phase:
        filled["season_phase"] = phase.get("phase")
    filled.update(history_override or {})
    try:
        result = validate_week(plan, filled, rules)
    except (KeyError, TypeError, ValueError) as error:
        return f"Error: the plan is malformed ({type(error).__name__}: {error}). See the schema above."
    return _dump({**result, "history_used": filled, "season": phase})


def _history_call(operation) -> str:
    try:
        with db.get_connection() as conn:
            return _dump(operation(conn))
    except history.HistoryError as error:
        return f"Error: {error}"


@tool
def easy_pace_trend(weeks: int = 12, hr_low: float | None = None, hr_high: float | None = None) -> str:
    """From the local synced history: pace of easy runs (3 km+, Garmin label recovery or aerobic
    base) inside one heart-rate band, per Monday-Sunday week for the last `weeks` (default 12).
    Faster at the same HR = fitter; slower = fatigue, heat or illness. The band defaults to the
    median HR of the period's easy runs +/- 4 bpm; pass hr_low/hr_high to fix it (e.g. zone 2)
    so weeks compare like for like. The summary compares the first and second half.
    `corrected` is the better measure: pace at a reference HR in cool weather on the flat, per
    3-week window with a margin of error, fitted on steady per-km laps (warm-up, pickups and
    long-run drift left out; heat from the activity's dew point). Trust a change only when
    `corrected.summary.significant` is true."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        raw = review.easy_pace_trend(conn, weeks, hr_low, hr_high)
        try:
            corrected = efficiency.trend(conn, max(weeks, 24))
        except sqlite3.Error:  # an older database without the weather column
            corrected = {"available": False}
        return _dump({**raw, "corrected": corrected})


@tool
def readiness_calibration(weeks: int = 16) -> str:
    """Check the readiness thresholds against what happened. For each of the last `weeks`
    weeks (default 16): readiness the day before it started, planned sessions (all sources)
    completed vs missed, run km change, and whether an adjustment was logged; plus averages
    per status. If yellow/red weeks go as well as green ones the thresholds are too strict;
    if green weeks often fall apart they are too loose. Suggest new values to the athlete;
    change them with update_athlete_profile section rule_overrides."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        return _dump(review.readiness_calibration(conn, weeks))


@write_tool(read_only=True)
def week_review(week_start: str | None = None) -> str:
    """Planned vs done for one Monday-Sunday week (default: last complete week). Planned:
    every calendar workout (all sources) with status completed / missed / skipped, the [CP]
    plan's estimated run km and quality sessions. Actual: run km and change vs the week
    before, runs, longest run, quality sessions, hours and sessions per sport. Also readiness
    at the start and end of the week, that week's coaching-history entries, and a
    suggested_actual line for log_coaching_decision. Start every weekly planning with this."""
    return _history_call(lambda conn: review.week_review(conn, week_start))


@tool
def swim_trend(weeks: int = 12) -> str:
    """From the local synced history: swimming per Monday week for the last `weeks` (default
    12): swims, distance, pace per 100 m (moving time, drills included), distance-weighted
    SWOLF (strokes + seconds per length; lower = more efficient) and longest swim, plus the
    change between the first and second half. Use it to see whether swimming is getting
    easier: pace and SWOLF down at the same effort."""
    with _connect() as conn:
        conn.row_factory = sqlite3.Row
        return _dump(swim.trend(conn, weeks))


@write_tool(read_only=True)
def swim_paces() -> str:
    """The athlete's CSS (critical swim speed, the swim threshold pace per 100 m) from the
    latest test and the zones built on it: recovery, endurance, steady, css, fast. Plus every
    test so far. Use the zones for swim set targets in notes (the watch has no pace targets
    for pool swims)."""
    return _history_call(swim.paces)


@write_tool(idempotent=True, open_world=False)
def record_css_test(t400: str, t200: str, date: str | None = None, note: str | None = None) -> str:
    """Save a CSS test: the 400 m and 200 m all-out times ("7:20" or seconds) from the same
    session. CSS per 100 m = (T400 - T200) / 2. Returns CSS, the change since the last test and
    the new zones. Read the times from the swim's laps (garmin_activity) or ask the athlete.
    The season plan's swim sets use the latest CSS."""
    return _history_call(lambda conn: swim.record_css(conn, t400, t200, date, note))


@write_tool(read_only=True)
def season_progress() -> str:
    """Is the plan working? For the weekly review or each lighter week: run km of the last 4
    weeks vs their season targets, [CP] sessions completed vs missed, current VDOT vs the
    straight line from the starting VDOT to the goal, the checkpoint time trials (expected
    result, and the actual one found in the synced runs), easy pace at the same HR, swim pace
    and SWOLF, days since the last CSS test, readiness. Each finding is a flag (ok, watch,
    act) and there is an overall verdict. Discuss 'act' flags with the athlete and log the
    decision."""
    return _history_call(progress.progress)


@write_tool(read_only=True)
def season_plan(weeks: int | None = None, start: str | None = None) -> str:
    """The season plan to the next A race, generated in code from the saved anchors (see
    start_season_plan): per Monday week the phase (base, build, peak, taper, race_week),
    lighter weeks, target run km and long run, the quality sessions from fixed progressions
    (threshold, VO2, goal race pace, sharpening; paces come from training_paces), extras
    (hills, strides), the week's three swims (weekly volume, continuous distance, CSS sets in
    build and peak with paces from the latest CSS test, CSS test weeks), gym minimum and the
    flexible slot's sport. Plus the goal time as pace and VDOT. start (YYYY-MM-DD) and weeks
    narrow the list. Plan each week from its target; upload_plan compares the week with it."""
    return _history_call(lambda conn: season.plan(conn, weeks, start))


@write_tool(idempotent=True, open_world=False)
def start_season_plan(peak_run_km: float | None = None, start_continuous_swim_m: float | None = None,
                      dry_run: bool = True) -> str:
    """Create or reset the season plan for the next A race in the profile. Anchors come from
    the synced history: next Monday, this week's run km (done + still planned), the longest
    run in 30 days and the 12-week peak (the default peak target is 5% above it), the average
    weekly swim distance of the last 4 weeks. Pass peak_run_km, or start_continuous_swim_m (how
    far the athlete swims nonstop today; default 400 m), to set them. dry_run=true (default) returns the anchors
    and the whole season without saving; show it to the athlete and save (dry_run=false) only
    after they agree. Resetting keeps the old anchors in coaching_history."""
    def run(conn):
        anchor = season.propose_anchor(conn, peak_run_km=peak_run_km,
                                       start_continuous_swim_m=start_continuous_swim_m)
        race = season.a_race(conn)
        if not race:
            raise history.HistoryError("No upcoming A race in the profile; add races first.")
        weeks = season.generate(anchor, race, history.rules(conn), swim.latest_css(conn))
        result = {"dry_run": dry_run, "race": race, "goal": season.goal_pace(race), "anchor": anchor, "weeks": weeks}
        if not dry_run:
            history.set_profile(conn, "season_plan", json.dumps(anchor),
                                f"Season plan for {race['name']} from {anchor['start_week']}: "
                                f"{anchor['start_run_km']} km/week to a {anchor['peak_run_km']} km peak.")
        return result
    return _history_call(run)


@write_tool(open_world=False)
def log_coaching_decision(summary: str, kind: str = "plan", week_start: str | None = None,
                          readiness: str | None = None, planned: str | None = None,
                          actual: str | None = None, details: dict | None = None) -> str:
    """Append one entry to the coaching history (the decision log). Call it after every
    planned week, mid-week change, weekly review or durable thing the athlete says.
    summary: 1-2 sentences, what changed and why. kind: plan, adjustment, review or note.
    week_start: the Monday of the week it concerns. readiness: green, yellow, red or unknown.
    planned / actual: short text, e.g. "34 km, 1 quality" / "30 km, missed Thu (sick)".
    details: optional JSON (validator result, readiness signals...). Uploads made with
    upload_plan are logged automatically as kind "upload"."""
    return _history_call(lambda conn: history.add_entry(conn, kind, summary, week_start, readiness,
                                                       planned, actual, details))


@write_tool(read_only=True)
def coaching_history(start: str | None = None, end: str | None = None, kind: str | None = None,
                     limit: int = 30) -> str:
    """The coaching history, newest first: weekly plans, changes and why, reviews, notes,
    automatic upload records and profile edits (with the previous text). start/end
    (YYYY-MM-DD) filter on the week; kind filters on plan, adjustment, review, note, upload
    or profile. Automatic weekly reviews show their card only (the app's /api/review has the
    full payload). Read it, with athlete_profile, before planning a week."""
    from garmin_ai.coaching import weekly
    return _history_call(lambda conn: [weekly.compact(e) for e in history.entries(conn, start, end, kind, limit)])


@write_tool(read_only=True)
def athlete_profile() -> str:
    """The athlete profile the coach keeps: races (with the season phase worked out from the
    next A race: base, build, peak, taper, race_week or recovery, and weeks to go), fitness
    anchors (latest race or test, VDOT, swim CSS), setup and constraints (sessions per sport,
    office days, equipment), cautions (injuries, niggles) and rule_overrides (JSON merged into
    the coaching rules). Weekly volume is not stored here; use weekly_load_summary. Lists
    sections that are still missing; ask the athlete rather than inventing them."""
    return _history_call(lambda conn: {**history.profile(conn), "season": review.season(conn)})


@write_tool(idempotent=True, open_world=False)
def update_athlete_profile(section: str, content: str, reason: str | None = None) -> str:
    """Replace one section of the athlete profile (empty content removes it). Sections:
    races (a JSON list: [{"name": "Valencia Marathon", "date": "2026-12-06", "priority": "A",
    "goal": "sub 3:30", "distance": "marathon"}]; priority A, B or C; the A race sets the
    season phase), fitness_anchors, setup_and_constraints, cautions, rule_overrides (a JSON
    object such as {"structure": {"runs": 4}, "swim": {"max_midday_minutes": 30}}), or
    another short name. Send the whole new section text, not a diff. The previous text
    is kept in coaching_history. reason: why it changed."""
    return _history_call(lambda conn: history.set_profile(conn, section, content, reason))


if __name__ == "__main__":
    build_server().run(transport="stdio")
