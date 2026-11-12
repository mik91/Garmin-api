"""Hevy lifting log: sync from the Hevy API and the read-only /api/strength payload.

The first sync pages through GET /workouts; later syncs ask GET /workouts/events for the
workouts updated or deleted since the previous one. Every workout is then linked to the
Garmin strength activity it overlaps, which supplies heart rate and training effect.
Needs HEVY_API_KEY (Hevy Pro); without it the sync is skipped.
"""
import bisect
import json
import sqlite3
from contextlib import closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from garmin_ai import config, db

PAGE_SIZE = 10  # Hevy's maximum for workouts and workout events
TEMPLATE_PAGE_SIZE = 100
# Templates are fetched again after this long, so new fields and renamed exercises arrive.
TEMPLATE_MAX_AGE = timedelta(days=7)
# Ask again for a few minutes before the last sync so clock skew cannot drop an event.
EVENT_OVERLAP = timedelta(minutes=5)
# Watches are often started a little before, or stopped after, the Hevy workout.
MATCH_SLACK = timedelta(minutes=15)
# Warm-up sets are kept and returned, but left out of the set counts and volume.
UNCOUNTED_SET_TYPES = {"warmup"}
MAX_RANGE_DAYS = 731
# Template types whose weight is the load lifted (assisted exercises log the assistance instead).
WEIGHTED_TYPES = {"weight_reps", "bodyweight_weighted"}
E1RM_MAX_REPS = 12
KG_PER_LB = 0.45359237


class NotFound(Exception):
    """A requested record does not exist (served as 404)."""


class HevyError(Exception):
    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


class HevyClient:
    def __init__(self, api_key: str, base_url: str | None = None, timeout: int = 30):
        self.api_key = api_key
        self.base_url = (base_url or config.HEVY_BASE_URL).rstrip("/")
        self.timeout = timeout

    def get(self, path: str, **params) -> dict:
        url = self.base_url + path + ("?" + urlencode(params) if params else "")
        request = Request(url, headers={"api-key": self.api_key, "Accept": "application/json",
                                        "User-Agent": "garmin-insights"})
        try:
            with urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read() or b"{}")
        except HTTPError as error:
            if error.code in (401, 403):
                raise HevyError("Hevy rejected the API key. Check HEVY_API_KEY (needs Hevy Pro).",
                                error.code) from error
            raise HevyError(f"Hevy API error {error.code}.", error.code) from error
        except (URLError, TimeoutError) as error:
            raise HevyError("Hevy could not be reached.") from error

    def pages(self, path: str, key: str, page_size: int = PAGE_SIZE, **params):
        """Yield the items of every page. Hevy answers 404 for a page past the last one."""
        page = 1
        while True:
            try:
                data = self.get(path, page=page, pageSize=page_size, **params)
            except HevyError as error:
                if error.status == 404:
                    return
                raise
            items = data.get(key) or []
            yield from items
            if not items or page >= (data.get("page_count") or 0):
                return
            page += 1


def _utc(value) -> datetime | None:
    """Parse an ISO time; naive values (Garmin's startTimeGMT) are taken as UTC."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else None


def delete_workout(conn, workout_id: str):
    for table in ("hevy_sets", "hevy_exercises", "hevy_workouts"):
        conn.execute(f"DELETE FROM {table} WHERE workout_id = ?", (workout_id,))


def store_workout(conn, workout: dict):
    """Replace one workout with its exercises and sets."""
    workout_id = workout.get("id")
    if not workout_id:
        return
    delete_workout(conn, workout_id)
    conn.execute(
        """
        INSERT INTO hevy_workouts (workout_id, title, description, start_time, end_time, updated_at, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (workout_id, workout.get("title"), workout.get("description"), _iso(_utc(workout.get("start_time"))),
         _iso(_utc(workout.get("end_time"))), workout.get("updated_at"), db.dumps(workout)),
    )
    for exercise_index, exercise in enumerate(workout.get("exercises") or []):
        conn.execute(
            """
            INSERT INTO hevy_exercises (workout_id, exercise_index, title, template_id, superset_id, notes)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (workout_id, exercise_index, exercise.get("title"), exercise.get("exercise_template_id"),
             exercise.get("superset_id"), exercise.get("notes")),
        )
        conn.executemany(
            """
            INSERT INTO hevy_sets (workout_id, exercise_index, set_index, set_type, weight_kg, reps, rpe,
                                   distance_m, duration_s)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [(workout_id, exercise_index, set_index, s.get("type"), s.get("weight_kg"), s.get("reps"), s.get("rpe"),
              s.get("distance_meters"), s.get("duration_seconds"))
             for set_index, s in enumerate(exercise.get("sets") or [])],
        )


def _templates_stale(conn, now: datetime) -> bool:
    """True when a workout uses an unknown template, or any stored template was never fetched
    with the current fields (fetched_at NULL) or was fetched more than TEMPLATE_MAX_AGE ago."""
    unknown = conn.execute(
        "SELECT 1 FROM hevy_exercises WHERE template_id IS NOT NULL AND template_id NOT IN "
        "(SELECT template_id FROM hevy_exercise_templates) LIMIT 1").fetchone()
    if unknown:
        return True
    count, never, oldest = conn.execute(
        "SELECT COUNT(*), COUNT(*) - COUNT(fetched_at), MIN(fetched_at) FROM hevy_exercise_templates").fetchone()
    return bool(count) and (never > 0 or _utc(oldest) is None or now - _utc(oldest) > TEMPLATE_MAX_AGE)


def _sync_templates(client: HevyClient, conn):
    """Fetch exercise names, muscle groups, equipment and thumbnails when they are missing or stale."""
    now = datetime.now(timezone.utc)
    if not _templates_stale(conn, now):
        return
    fetched_at = _iso(now)
    seen = set()
    for template in client.pages("/exercise_templates", "exercise_templates", TEMPLATE_PAGE_SIZE):
        conn.execute(
            """
            INSERT INTO hevy_exercise_templates
                (template_id, title, type, primary_muscle_group, secondary_muscle_groups_json, is_custom,
                 equipment, thumbnail_url, fetched_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(template_id) DO UPDATE SET
                title=excluded.title,
                type=excluded.type,
                primary_muscle_group=excluded.primary_muscle_group,
                secondary_muscle_groups_json=excluded.secondary_muscle_groups_json,
                is_custom=excluded.is_custom,
                equipment=excluded.equipment,
                thumbnail_url=excluded.thumbnail_url,
                fetched_at=excluded.fetched_at
            """,
            (template.get("id"), template.get("title"), template.get("type"), template.get("primary_muscle_group"),
             db.dumps(template.get("secondary_muscle_groups") or []), int(bool(template.get("is_custom"))),
             template.get("equipment"), template.get("thumbnail_url"), fetched_at),
        )
        seen.add(template.get("id"))
    # Templates Hevy no longer lists (deleted custom exercises) keep what was stored, and are
    # marked fetched so they do not force a refetch on every sync.
    used = {row[0] for row in conn.execute("SELECT DISTINCT template_id FROM hevy_exercises WHERE template_id IS NOT NULL")}
    conn.executemany("INSERT OR IGNORE INTO hevy_exercise_templates (template_id) VALUES (?)", [(t,) for t in used - seen])
    conn.execute("UPDATE hevy_exercise_templates SET fetched_at = ?", (fetched_at,))


def link_garmin_activities(conn):
    """Link each workout to the Garmin strength activity it overlaps most, and set its local
    start time from that activity's time zone (or the nearest Garmin activity's)."""
    garmin = []
    for row in conn.execute("SELECT activity_id, type, start_time, duration_s, raw_json FROM activities"):
        try:
            gmt = (json.loads(row["raw_json"] or "{}") or {}).get("startTimeGMT")
            local = datetime.fromisoformat(str(row["start_time"]))
        except (ValueError, TypeError, AttributeError):
            continue
        start = _utc(gmt)
        if start is None:
            continue
        garmin.append((start, start + timedelta(seconds=row["duration_s"] or 0), row["activity_id"],
                       "strength" in (row["type"] or ""), local - start.replace(tzinfo=None)))
    garmin.sort(key=lambda item: item[0])
    starts = [item[0] for item in garmin]
    strength = [item for item in garmin if item[3]]

    for workout in conn.execute("SELECT workout_id, start_time, end_time FROM hevy_workouts").fetchall():
        start = _utc(workout["start_time"])
        if start is None:
            continue
        end = max(_utc(workout["end_time"]) or start, start + timedelta(minutes=1))
        best, best_overlap = None, timedelta(0)
        for activity in strength:
            overlap = min(end, activity[1] + MATCH_SLACK) - max(start, activity[0] - MATCH_SLACK)
            if overlap > best_overlap:
                best, best_overlap = activity, overlap
        reference = best
        if reference is None and garmin:
            position = bisect.bisect_left(starts, start)
            nearby = garmin[max(0, position - 1):position + 1]
            reference = min(nearby, key=lambda item: abs(item[0] - start))
        offset = reference[4] if reference else timedelta(0)
        conn.execute(
            "UPDATE hevy_workouts SET garmin_activity_id = ?, start_local = ? WHERE workout_id = ?",
            (best[2] if best else None, (start.replace(tzinfo=None) + offset).isoformat(timespec="seconds"),
             workout["workout_id"]),
        )


def _event_time(event: dict) -> datetime:
    value = event.get("deleted_at") or (event.get("workout") or {}).get("updated_at")
    return _utc(value) or datetime.min.replace(tzinfo=timezone.utc)


def sync_workouts(client: HevyClient, conn) -> int:
    """Backfill on the first run, then apply update/delete events. Returns the changes applied."""
    started = datetime.now(timezone.utc)
    since = db.get_sync_state(conn).get("hevy_events_since")
    changed = 0
    if since:
        for event in sorted(client.pages("/workouts/events", "events", since=since), key=_event_time):
            if event.get("type") == "deleted" and event.get("id"):
                delete_workout(conn, event["id"])
            elif isinstance(event.get("workout"), dict):
                store_workout(conn, event["workout"])
            else:
                continue
            changed += 1
    else:
        for workout in client.pages("/workouts", "workouts"):
            store_workout(conn, workout)
            changed += 1
    _sync_templates(client, conn)
    link_garmin_activities(conn)
    db.set_sync_state(conn, "hevy_events_since", _iso(started - EVENT_OVERLAP))
    db.set_sync_state(conn, "hevy_last_synced_at", datetime.now().isoformat(timespec="seconds"))
    db.set_sync_state(conn, "hevy_error", "")
    return changed


def sync_if_configured() -> int | None:
    """Run the Hevy sync in its own transaction; failures are recorded, never raised."""
    if not config.HEVY_API_KEY:
        return None
    try:
        with db.get_connection() as conn:
            return sync_workouts(HevyClient(config.HEVY_API_KEY), conn)
    except Exception as error:
        message = str(error) if isinstance(error, HevyError) else "Hevy sync failed."
        with db.get_connection() as conn:
            db.set_sync_state(conn, "hevy_error", message)
        return None


# --- /api/strength --------------------------------------------------------------------------

def _bounds(query) -> tuple[date, date]:
    try:
        end = date.fromisoformat(query.get("end", [date.today().isoformat()])[0])
        start = date.fromisoformat(query.get("start", [(end - timedelta(days=27)).isoformat()])[0])
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.")
    if start > end or (end - start).days > MAX_RANGE_DAYS:
        raise ValueError("Choose a valid date range (at most two years).")
    return start, end


def _near_multiple(value: float, step: float, tolerance: float) -> bool:
    remainder = value % step
    return min(remainder, step - remainder) <= tolerance


def weight_unit(conn) -> str:
    """"lb" when most logged weights are whole half-pounds that Hevy converted to kg (185 lb
    arrives as 83.9146...), otherwise "kg". HEVY_WEIGHT_UNIT overrides the guess."""
    if config.HEVY_WEIGHT_UNIT in ("kg", "lb"):
        return config.HEVY_WEIGHT_UNIT
    weights = [row[0] for row in conn.execute("SELECT weight_kg FROM hevy_sets WHERE weight_kg > 0")]
    pounds = sum(1 for kg in weights
                 if _near_multiple(kg / KG_PER_LB, 0.5, 0.01) and not _near_multiple(kg, 0.25, 0.001))
    return "lb" if weights and pounds > len(weights) / 2 else "kg"


def _counted(sets) -> list[dict]:
    return [s for s in sets if s["type"] not in UNCOUNTED_SET_TYPES]


def _totals(sets) -> dict:
    counted = _counted(sets)
    return {"sets": len(counted), "reps": sum(s["reps"] or 0 for s in counted),
            "volume_kg": round(sum((s["weight_kg"] or 0) * (s["reps"] or 0) for s in counted), 1)}


def _garmin_summary(row) -> dict | None:
    if row["activity_id"] is None:
        return None
    try:
        raw = json.loads(row["raw_json"] or "{}") or {}
    except ValueError:
        raw = {}
    return {"activity_id": row["activity_id"], "name": raw.get("activityName"), "type": row["type"],
            "start_time": row["activity_start"], "duration_s": row["activity_duration"], "calories": row["calories"],
            "avg_hr": row["avg_hr"], "max_hr": row["max_hr"], "aerobic_te": row["aerobic_te"],
            "anaerobic_te": row["anaerobic_te"], "training_load": raw.get("activityTrainingLoad")}


def payload(query, db_path=None) -> dict:
    start, end = _bounds(query)
    in_range = "substr(COALESCE({0}start_local, {0}start_time), 1, 10) BETWEEN ? AND ?"
    dates = (start.isoformat(), end.isoformat())
    with closing(sqlite3.connect(Path(db_path or config.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        workouts = conn.execute(
            f"""
            SELECT w.workout_id, w.title, w.description, w.start_time, w.end_time, w.start_local,
                   a.activity_id, a.type, a.start_time AS activity_start, a.duration_s AS activity_duration,
                   a.calories, a.avg_hr, a.max_hr, a.aerobic_te, a.anaerobic_te, a.raw_json
            FROM hevy_workouts w LEFT JOIN activities a ON a.activity_id = w.garmin_activity_id
            WHERE {in_range.format("w.")}
            ORDER BY COALESCE(w.start_local, w.start_time) DESC
            """, dates).fetchall()
        selected = f"SELECT workout_id FROM hevy_workouts WHERE {in_range.format('')}"
        exercises = conn.execute(
            f"""
            SELECT e.*, t.type AS template_type, t.primary_muscle_group, t.secondary_muscle_groups_json,
                   t.equipment, t.thumbnail_url
            FROM hevy_exercises e LEFT JOIN hevy_exercise_templates t ON t.template_id = e.template_id
            WHERE e.workout_id IN ({selected}) ORDER BY e.workout_id, e.exercise_index
            """, dates).fetchall()
        sets = conn.execute(
            f"SELECT * FROM hevy_sets WHERE workout_id IN ({selected}) "
            "ORDER BY workout_id, exercise_index, set_index", dates).fetchall()
        state = dict(conn.execute("SELECT key, value FROM sync_state WHERE key LIKE 'hevy_%'").fetchall())
        unit = weight_unit(conn)

    sets_by_exercise = {}
    for s in sets:
        sets_by_exercise.setdefault((s["workout_id"], s["exercise_index"]), []).append({
            "index": s["set_index"], "type": s["set_type"], "weight_kg": s["weight_kg"], "reps": s["reps"],
            "rpe": s["rpe"], "distance_m": s["distance_m"], "duration_s": s["duration_s"]})
    exercises_by_workout = {}
    for e in exercises:
        exercises_by_workout.setdefault(e["workout_id"], []).append({
            "index": e["exercise_index"], "title": e["title"], "template_id": e["template_id"],
            "type": e["template_type"], "primary_muscle_group": e["primary_muscle_group"],
            "secondary_muscle_groups": json.loads(e["secondary_muscle_groups_json"] or "[]"),
            "superset_id": e["superset_id"], "notes": e["notes"],
            "equipment": e["equipment"], "thumbnail_url": e["thumbnail_url"],
            "sets": sets_by_exercise.get((e["workout_id"], e["exercise_index"]), [])})

    result = []
    for w in workouts:
        exercise_list = exercises_by_workout.get(w["workout_id"], [])
        began, finished = _utc(w["start_time"]), _utc(w["end_time"])
        local = w["start_local"] or (w["start_time"] or "").rstrip("Z")
        result.append({
            "id": w["workout_id"], "title": w["title"], "description": w["description"],
            "start_time": w["start_time"], "end_time": w["end_time"], "start_local": local, "date": local[:10],
            "duration_s": int((finished - began).total_seconds()) if began and finished else None,
            **_totals([s for e in exercise_list for s in e["sets"]]),
            "exercises": exercise_list, "garmin": _garmin_summary(w)})

    weeks, monday = [], start - timedelta(days=start.weekday())
    while monday <= end:
        week_end = (monday + timedelta(days=6)).isoformat()
        in_week = [w for w in result if monday.isoformat() <= w["date"] <= week_end]
        weeks.append({"week_start": monday.isoformat(), "sessions": len(in_week),
                      "sets": sum(w["sets"] for w in in_week), "reps": sum(w["reps"] for w in in_week),
                      "volume_kg": round(sum(w["volume_kg"] for w in in_week), 1),
                      "duration_s": sum(w["duration_s"] or 0 for w in in_week)})
        monday += timedelta(days=7)

    return {"start": start.isoformat(), "end": end.isoformat(), "weight_unit": unit,
            "sync": {"configured": bool(config.HEVY_API_KEY), "last_synced_at": state.get("hevy_last_synced_at"),
                     "error": state.get("hevy_error") or None},
            "workouts": result, "weekly": weeks}


# --- /api/strength/exercise -----------------------------------------------------------------

def _e1rm(weight_kg, reps) -> float | None:
    """Epley estimate; only for 1 to E1RM_MAX_REPS reps, where it is reasonably accurate."""
    if not weight_kg or weight_kg <= 0 or not reps or not 1 <= reps <= E1RM_MAX_REPS:
        return None
    return weight_kg * (1 + reps / 30)


def _session_stats(sets, weighted: bool) -> dict:
    working = _counted(sets)
    loaded = [s for s in working if weighted and (s["weight_kg"] or 0) > 0]
    best = max(loaded, key=lambda s: (s["weight_kg"], s["reps"] or 0), default=None)
    e1rms = [value for s in loaded if (value := _e1rm(s["weight_kg"], s["reps"])) is not None]
    reps = [s["reps"] for s in working if s["reps"] is not None]
    durations = [s["duration_s"] for s in working if s["duration_s"] is not None]
    return {**_totals(sets),
            "best_set": {"weight_kg": best["weight_kg"], "reps": best["reps"]} if best else None,
            "e1rm_kg": round(max(e1rms), 2) if e1rms else None,
            "max_reps": max(reps) if reps else None,
            "max_duration_s": max(durations) if durations else None}


def _record(sessions, value) -> dict | None:
    """The first (oldest) session with the highest positive value, or None."""
    best = None
    for session in sessions:
        current = value(session)
        if current is not None and current > 0 and (best is None or current > best["value"]):
            best = {"value": current, "date": session["date"], "workout_id": session["workout_id"]}
    return best


def exercise_progress(query, db_path=None) -> dict:
    """Every session of one exercise across all history, oldest first, plus all-time records."""
    template_id = (query.get("template_id") or [""])[0].strip()
    if not template_id or len(template_id) > 64:
        raise ValueError("template_id is required.")
    with closing(sqlite3.connect(Path(db_path or config.DB_PATH).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        conn.row_factory = sqlite3.Row
        template = conn.execute("SELECT * FROM hevy_exercise_templates WHERE template_id = ?", (template_id,)).fetchone()
        rows = conn.execute(
            """
            SELECT w.workout_id, w.title AS workout_title, COALESCE(w.start_local, w.start_time) AS local,
                   e.title AS exercise_title, s.set_type, s.weight_kg, s.reps, s.duration_s
            FROM hevy_exercises e
            JOIN hevy_workouts w ON w.workout_id = e.workout_id
            LEFT JOIN hevy_sets s ON s.workout_id = e.workout_id AND s.exercise_index = e.exercise_index
            WHERE e.template_id = ?
            ORDER BY local, w.workout_id, e.exercise_index, s.set_index
            """, (template_id,)).fetchall()
        unit = weight_unit(conn)
    if template is None and not rows:
        raise NotFound("Unknown exercise template.")

    kind = template["type"] if template else None
    weighted = kind in WEIGHTED_TYPES
    workouts = {}
    for row in rows:
        workout = workouts.setdefault(row["workout_id"], {
            "workout_id": row["workout_id"], "date": (row["local"] or "")[:10], "title": row["workout_title"], "sets": []})
        if row["set_type"] is not None or row["weight_kg"] is not None or row["reps"] is not None:
            workout["sets"].append({"type": row["set_type"], "weight_kg": row["weight_kg"], "reps": row["reps"],
                                    "duration_s": row["duration_s"]})
    sessions = [{"workout_id": w["workout_id"], "date": w["date"], "title": w["title"],
                 **_session_stats(w["sets"], weighted)} for w in workouts.values()]

    def field(name, only_weighted=False):
        return lambda session: session[name] if weighted or not only_weighted else None

    stored = dict(template) if template else {}
    return {
        "template": {
            "id": template_id,
            "title": stored.get("title") or (rows[0]["exercise_title"] if rows else None),
            "type": kind,
            "primary_muscle_group": stored.get("primary_muscle_group"),
            "secondary_muscle_groups": json.loads(stored.get("secondary_muscle_groups_json") or "[]"),
            "equipment": stored.get("equipment"),
            "thumbnail_url": stored.get("thumbnail_url")},
        "weight_unit": unit,
        "sessions": sessions,
        "records": {
            "heaviest_kg": _record(sessions, lambda s: s["best_set"]["weight_kg"] if s["best_set"] else None),
            "best_e1rm_kg": _record(sessions, field("e1rm_kg")),
            "most_reps": _record(sessions, field("max_reps")),
            "best_volume_kg": _record(sessions, field("volume_kg", only_weighted=True)),
            "longest_duration_s": _record(sessions, field("max_duration_s"))}}
