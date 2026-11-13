"""Garmin Connect writes for the AI coach: create, schedule, update and delete workouts.

Safety rules:
- Only workouts recorded in coach_workouts AND named with the "[TP] " prefix (checked
  again on Garmin before any change) are ever updated, unscheduled or deleted. Workouts
  from Runna, Garmin Coach or made by hand are never touched.
- Every upload is read back from Garmin and compared with what was sent.

Uses the same garminconnect sign-in token as the sync; there are no new credentials.
"""
import calendar as month_calendar
import json
from datetime import date, datetime, timedelta

from garmin_ai import calendar as training_calendar
from garmin_ai import db, workouts
from garmin_ai.coaching import history
from garmin_ai.workouts import WorkoutError, is_owned_name, strip_prefix


class CoachError(Exception):
    """A user-facing reason a write was refused or failed."""


# --- Garmin client ----------------------------------------------------------------------

def _no_mfa():
    raise CoachError("Garmin asked for a new sign-in with an MFA code. Sign in again on the dashboard's "
                     "Connections page, then retry.")


def client():
    from garmin_ai import sync
    try:
        return sync.login(prompt_mfa=_no_mfa)
    except CoachError:
        raise
    except Exception as error:
        raise CoachError(f"Could not sign in to Garmin Connect: {sync.friendly_error(error)}") from error


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _date(value: str) -> str:
    try:
        return date.fromisoformat(str(value)).isoformat()
    except ValueError:
        raise CoachError(f"Date {value!r} must be YYYY-MM-DD.") from None


# --- ownership --------------------------------------------------------------------------

def _owned(conn, workout_id) -> dict:
    row = conn.execute("SELECT * FROM coach_workouts WHERE workout_id = ?", (str(workout_id),)).fetchone()
    if row is None or not is_owned_name(row["name"]):
        raise CoachError(f"Workout {workout_id} was not created by these tools, so it will not be changed. "
                         "Only [TP] workouts from list_planned_workouts can be updated, unscheduled or deleted.")
    return dict(row)


def _check_on_garmin(api, workout_id) -> dict:
    stored = api.get_workout_by_id(workout_id)
    if not is_owned_name(stored.get("workoutName")):
        raise CoachError(f"Workout {workout_id} on Garmin is named {stored.get('workoutName')!r}, without the "
                         "[TP] prefix, so it will not be changed.")
    return stored


def _record(conn, workout_id, payload: dict):
    now = _now()
    conn.execute(
        """INSERT INTO coach_workouts VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT(workout_id) DO UPDATE SET name=excluded.name, sport=excluded.sport,
               payload_hash=excluded.payload_hash, payload_json=excluded.payload_json, updated_at=excluded.updated_at""",
        (str(workout_id), payload["workoutName"], payload["sportType"]["sportTypeKey"],
         workouts.payload_hash(payload), json.dumps(payload), now, now))


def _schedule_id(response) -> str | None:
    if isinstance(response, dict):
        value = training_calendar._first(response, "workoutScheduleId", "scheduleId", "id")
        return str(value) if value is not None else None
    return None


def _refresh_calendar(api, conn, days: list[str]):
    """Re-cache the Garmin calendar for the touched months, so training_calendar shows the
    change right away. Best effort: the next sync fixes it anyway."""
    from garmin_ai import sync
    for year, month in sorted({(int(d[:4]), int(d[5:7])) for d in days}):
        first = date(year, month, 1)
        last = date(year, month, month_calendar.monthrange(year, month)[1])
        try:
            sync._sync_scheduled_workouts(api, conn, first, last)
            conn.commit()
        except Exception:
            pass


# --- operations -------------------------------------------------------------------------

def _upload(api, conn, payload: dict) -> dict:
    response = api.upload_workout(payload)
    workout_id = response.get("workoutId") if isinstance(response, dict) else None
    if not workout_id:
        raise CoachError(f"Garmin did not return a workout id: {str(response)[:300]}")
    _record(conn, workout_id, payload)
    conn.commit()
    problems = workouts.compare(payload, api.get_workout_by_id(workout_id))
    if problems:
        try:
            api.delete_workout(workout_id)
            conn.execute("DELETE FROM coach_workouts WHERE workout_id = ?", (str(workout_id),))
            conn.commit()
            note = "It was deleted again."
        except Exception:
            note = f"Deleting it failed; remove it with delete_workout({workout_id})."
        raise CoachError("Garmin stored the workout differently from what was sent: " + "; ".join(problems)
                         + f". {note}")
    return {"workout_id": int(workout_id), "name": payload["workoutName"]}


def _schedule(api, conn, workout_id, day: str) -> str:
    response = api.schedule_workout(workout_id, day)
    schedule_id = _schedule_id(response)
    if not schedule_id:
        raise CoachError(f"Garmin did not return a schedule id: {str(response)[:300]}")
    conn.execute("INSERT OR REPLACE INTO coach_schedules VALUES (?, ?, ?, ?)",
                 (schedule_id, str(workout_id), day, _now()))
    conn.commit()
    return schedule_id


def create_workout(sport, name, steps, description=None, pool_length_m=None, date_=None, dry_run=False) -> dict:
    payload = workouts.build_workout(sport, name, steps, description, pool_length_m)
    day = _date(date_) if date_ else None
    result = {"summary": workouts.summary(payload)}
    if dry_run:
        return {**result, "dry_run": True, "garmin_payload": payload}
    api = client()
    with db.get_connection() as conn:
        result.update(_upload(api, conn, payload))
        if day:
            result["date"], result["schedule_id"] = day, _schedule(api, conn, result["workout_id"], day)
            _refresh_calendar(api, conn, [day])
    return result


def schedule_workout(workout_id, day) -> dict:
    day = _date(day)
    with db.get_connection() as conn:
        _owned(conn, workout_id)
        api = client()
        _check_on_garmin(api, workout_id)
        schedule_id = _schedule(api, conn, workout_id, day)
        _refresh_calendar(api, conn, [day])
    return {"workout_id": int(workout_id), "date": day, "schedule_id": schedule_id}


def _updated_payload(row: dict, sport=None, name=None, steps=None, description=None, pool_length_m=None) -> dict:
    """The new full payload: given fields replace the stored ones, the rest are kept."""
    old = json.loads(row["payload_json"])
    if steps is not None:
        return workouts.build_workout(
            sport or row["sport"], name or row["name"], steps,
            description if description is not None else old.get("description"),
            pool_length_m if pool_length_m is not None else old.get("poolLength"))
    if sport and workouts.sport_key(sport) != row["sport"]:
        raise CoachError("Changing the sport needs new steps as well.")
    payload = dict(old)
    if name:
        payload["workoutName"] = workouts.with_prefix(name)
    if description is not None:
        payload["description"] = description or None
    if pool_length_m is not None:
        if row["sport"] != "swimming":
            raise CoachError("pool_length_m is only for swim workouts.")
        payload["poolLength"] = float(pool_length_m)
    return payload


def _put(api, conn, workout_id, payload: dict):
    stored = _check_on_garmin(api, workout_id)
    body = {**payload, "workoutId": int(workout_id)}
    for field in ("ownerId", "author", "createdDate"):
        if stored.get(field) is not None:
            body[field] = stored[field]
    api.client.put("connectapi", f"{api.garmin_workouts}/workout/{int(workout_id)}", json=body, api=True)
    _record(conn, workout_id, payload)
    conn.commit()
    problems = workouts.compare(payload, api.get_workout_by_id(workout_id))
    if problems:
        raise CoachError("Garmin stored the update differently from what was sent: " + "; ".join(problems))


def update_workout(workout_id, sport=None, name=None, steps=None, description=None, pool_length_m=None,
                   dry_run=False) -> dict:
    with db.get_connection() as conn:
        payload = _updated_payload(_owned(conn, workout_id), sport, name, steps, description, pool_length_m)
        result = {"workout_id": int(workout_id), "summary": workouts.summary(payload)}
        if dry_run:
            return {**result, "dry_run": True, "garmin_payload": payload}
        api = client()
        _put(api, conn, workout_id, payload)
        days = [r[0] for r in conn.execute("SELECT date FROM coach_schedules WHERE workout_id = ?", (str(workout_id),))]
        _refresh_calendar(api, conn, days)
    return {**result, "updated": True}


def unschedule(day=None, schedule_id=None) -> dict:
    if bool(day) == bool(schedule_id):
        raise CoachError("Pass either date or schedule_id.")
    with db.get_connection() as conn:
        if schedule_id:
            rows = conn.execute("SELECT * FROM coach_schedules WHERE schedule_id = ?", (str(schedule_id),)).fetchall()
            if not rows:
                raise CoachError(f"Calendar item {schedule_id} was not scheduled by these tools, so it stays.")
        else:
            rows = conn.execute("SELECT * FROM coach_schedules WHERE date = ?", (_date(day),)).fetchall()
            if not rows:
                return {"removed": [], "note": "No [TP] workouts are scheduled on that date. Other workouts "
                                               "(for example Runna) are never touched."}
        api = client()
        removed = []
        for row in rows:
            _owned(conn, row["workout_id"])
            api.unschedule_workout(row["schedule_id"])
            conn.execute("DELETE FROM coach_schedules WHERE schedule_id = ?", (row["schedule_id"],))
            conn.commit()
            removed.append({"schedule_id": row["schedule_id"], "workout_id": int(row["workout_id"]), "date": row["date"]})
        _refresh_calendar(api, conn, [r["date"] for r in removed])
    return {"removed": removed}


def move(schedule_id, day) -> dict:
    """Move one scheduled [TP] workout to another day: schedule it on the new day first, then
    remove the old entry, so a failure never loses the workout. Logged in the coaching
    history as an adjustment, so the coach knows the athlete moved it."""
    day = _date(day)
    if day < date.today().isoformat():
        raise CoachError("Pick today or a later day.")
    with db.get_connection() as conn:
        row = conn.execute("SELECT * FROM coach_schedules WHERE schedule_id = ?", (str(schedule_id),)).fetchone()
        if row is None:
            raise CoachError(f"Calendar item {schedule_id} was not scheduled by these tools, so it stays.")
        old_day, workout_id = row["date"], row["workout_id"]
        if old_day == day:
            return {"schedule_id": str(schedule_id), "workout_id": int(workout_id), "from": old_day, "date": day}
        workout = _owned(conn, workout_id)
        api = client()
        _check_on_garmin(api, workout_id)
        new_id = _schedule(api, conn, workout_id, day)
        api.unschedule_workout(row["schedule_id"])
        conn.execute("DELETE FROM coach_schedules WHERE schedule_id = ?", (row["schedule_id"],))
        conn.commit()
        _refresh_calendar(api, conn, [old_day, day])
        name = strip_prefix(workout["name"])
        history.add_entry(conn, "adjustment", f"The athlete moved {name} from {old_day} to {day} in the app.",
                          week_start=day, details={"workout_id": int(workout_id), "from": old_day, "to": day,
                                                   "source": "app"})
        from garmin_ai.coaching import rolling
        rolling.record_move(conn, workout_id, old_day, day)
        conn.commit()
    return {"schedule_id": new_id, "workout_id": int(workout_id), "from": old_day, "date": day}


def move_request(body: dict) -> dict:
    """POST /api/plan/move {"schedule_id", "date"}: refusals become 400s. A rolling-plan draft
    ("draft:<id>") moves on the server only; a workout on Garmin moves there."""
    from garmin_ai.coaching import rolling
    schedule_id, day = body.get("schedule_id"), body.get("date")
    if not isinstance(schedule_id, str) or not schedule_id or not isinstance(day, str):
        raise ValueError("Pass schedule_id and date (YYYY-MM-DD).")
    try:
        if schedule_id.startswith(rolling.DRAFT_PREFIX):
            with db.get_connection() as conn:
                return rolling.move_draft(conn, schedule_id, day)
        return move(schedule_id, day)
    except (CoachError, history.HistoryError) as error:
        raise ValueError(str(error)) from None

def delete_workout(workout_id) -> dict:
    with db.get_connection() as conn:
        row = _owned(conn, workout_id)
        api = client()
        try:
            _check_on_garmin(api, workout_id)
            gone = False
        except CoachError:
            raise
        except Exception:
            gone = True  # already deleted on Garmin; just clean up the record
        schedules = conn.execute("SELECT schedule_id, date FROM coach_schedules WHERE workout_id = ?",
                                 (str(workout_id),)).fetchall()
        if not gone:
            for schedule in schedules:
                try:
                    api.unschedule_workout(schedule["schedule_id"])
                except Exception:
                    pass  # deleting the workout removes its calendar entries too
            api.delete_workout(workout_id)
        conn.execute("DELETE FROM coach_schedules WHERE workout_id = ?", (str(workout_id),))
        conn.execute("DELETE FROM coach_workouts WHERE workout_id = ?", (str(workout_id),))
        conn.commit()
        _refresh_calendar(api, conn, [s["date"] for s in schedules])
    return {"deleted": int(workout_id), "name": row["name"], "unscheduled_dates": [s["date"] for s in schedules]}


def list_planned(start=None, end=None) -> list[dict]:
    from garmin_ai import web_dashboard
    today = date.today()
    start = _date(start) if start else (today - timedelta(days=14)).isoformat()
    end = _date(end) if end else (today + timedelta(days=42)).isoformat()
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT s.schedule_id, s.date, w.workout_id, w.name, w.sport, w.payload_json
               FROM coach_schedules s JOIN coach_workouts w ON w.workout_id = s.workout_id
               WHERE s.date BETWEEN ? AND ? ORDER BY s.date, w.name""", (start, end)).fetchall()
        unscheduled = conn.execute(
            """SELECT workout_id, name, sport FROM coach_workouts
               WHERE workout_id NOT IN (SELECT workout_id FROM coach_schedules)""").fetchall()
    plans = {str(p.get("scheduled_workout_id")): p
             for p in web_dashboard.payload({"start": [start], "end": [end]})["calendar"]}
    result = []
    for row in rows:
        plan = plans.get(row["schedule_id"], {})
        result.append({"date": row["date"], "name": row["name"], "sport": row["sport"],
                       "workout_id": int(row["workout_id"]), "schedule_id": row["schedule_id"],
                       "status": plan.get("status") or ("planned" if row["date"] >= today.isoformat() else "unknown"),
                       "matched_activity_id": plan.get("matched_activity_id"),
                       "summary": workouts.summary(json.loads(row["payload_json"]))})
    if unscheduled:
        result.append({"unscheduled_workouts": [dict(r) for r in unscheduled]})
    return result


def plan_payload(query) -> dict:
    """GET /api/plan: the coach's scheduled workouts with their steps and completion status,
    plus training paces and goals. Defaults to four weeks back through twelve weeks ahead."""
    from garmin_ai import web_dashboard
    today = date.today()
    try:
        start = date.fromisoformat(query["start"][0]) if query.get("start") else today - timedelta(days=28)
        end = date.fromisoformat(query["end"][0]) if query.get("end") else today + timedelta(days=84)
    except ValueError:
        raise ValueError("Dates must be YYYY-MM-DD.") from None
    if start > end or (end - start).days > 400:
        raise ValueError("Choose a valid date range.")
    with db.get_connection() as conn:
        rows = conn.execute(
            """SELECT s.schedule_id, s.date, w.workout_id, w.name, w.sport, w.payload_json
               FROM coach_schedules s JOIN coach_workouts w ON w.workout_id = s.workout_id
               WHERE s.date BETWEEN ? AND ? ORDER BY s.date, w.name""",
            (start.isoformat(), end.isoformat())).fetchall()
        first = conn.execute("SELECT MIN(date) FROM coach_schedules").fetchone()[0]
    dashboard = web_dashboard.payload({"start": [start.isoformat()], "end": [end.isoformat()]})
    plans = {str(p.get("scheduled_workout_id")): p for p in dashboard["calendar"]}
    try:
        five_k = dashboard["race_model"]["predictions"]["5K"]["expected_seconds"]
        paces = {**workouts.training_paces(5000, five_k),
                 "based_on": f"5K prediction {workouts.format_duration(five_k)}"}
        threshold = paces["paces"]["threshold"]["seconds_per_km"][1]
    except (KeyError, TypeError):
        paces, threshold = None, None
    items = []
    for row in rows:
        workout = json.loads(row["payload_json"])
        plan = plans.get(row["schedule_id"], {})
        totals = workouts.estimate_totals(workout)
        items.append({
            "schedule_id": row["schedule_id"], "workout_id": int(row["workout_id"]), "date": row["date"],
            "name": strip_prefix(row["name"]), "sport": row["sport"],
            "category": workouts.category(workout, threshold),
            "status": plan.get("status") or ("planned" if row["date"] >= today.isoformat() else "unknown"),
            "matched_activity_id": plan.get("matched_activity_id"),
            "description": workout.get("description") or None,
            "duration_s": totals["duration_s"] or None, "distance_m": totals["distance_m"] or None,
            "duration_partial": totals["duration_partial"], "distance_partial": totals["distance_partial"],
            "pool_length_m": workout.get("poolLength"),
            "steps": workouts.structured_steps(workout)})
    from garmin_ai.coaching import rolling
    rolling_plan = None
    with db.get_connection() as conn:
        if (query.get("drafts") or ["1"])[0] != "0":
            items += rolling.app_drafts(conn, start, end, threshold)
            items.sort(key=lambda item: (item["date"], item["name"]))
        rolling_plan = rolling.app_summary(conn)
    return {"start": start.isoformat(), "end": end.isoformat(), "plan_start": first,
            "workouts": items, "paces": paces, "goals": dashboard["goals"],
            "races": dashboard["races"], "season": dashboard["season"],
            "weekly_review": dashboard["weekly_review"], "rolling": rolling_plan}


# --- whole plans ------------------------------------------------------------------------

def recent_weekly_run_km(conn, before: date, weeks: int = 4) -> list[float]:
    """Running km in each of the complete Monday-Sunday weeks before `before`."""
    monday = before - timedelta(days=before.weekday())
    result = []
    for index in range(weeks, 0, -1):
        first = monday - timedelta(days=7 * index)
        value = conn.execute(
            "SELECT COALESCE(SUM(distance_m), 0), COUNT(*) FROM activities WHERE type LIKE '%running%' "
            "AND substr(start_time, 1, 10) BETWEEN ? AND ?",
            (first.isoformat(), (first + timedelta(days=6)).isoformat())).fetchone()
        result.append(round((value[0] or 0) / 1000, 1))
    return result


def current_paces() -> dict | None:
    from garmin_ai import web_dashboard
    try:
        model = web_dashboard.payload({"start": [date.today().isoformat()], "end": [date.today().isoformat()]})["race_model"]
        five_k = model["predictions"]["5K"]["expected_seconds"]
    except Exception:
        return None
    return {**workouts.training_paces(5000, five_k), "based_on": f"5K prediction {workouts.format_duration(five_k)}"}


SESSION_FIELDS = ("slot", "kind", "intensity", "all_out", "hard_minutes", "km", "minutes")


def _monday(day: str) -> str:
    value = date.fromisoformat(day)
    return (value - timedelta(days=value.weekday())).isoformat()


def _chain(history_: dict, previous: dict, light_pct: float) -> dict:
    """History for a week right after a week planned in the same upload."""
    planned_km = previous["summary"]["planned_run_km"]
    weekly = list(history_["weekly_run_km"])
    weekly[-1] = planned_km  # that week has not happened yet; use the plan
    longest = max([history_.get("longest_run_km_30d") or 0]
                  + [x.get("km") or 0 for x in previous["sessions"] if x["sport"] == "run"])
    chained = {**history_, "weekly_run_km": weekly, "longest_run_km_30d": longest,
               "weeks_since_light": history.weeks_since_light(weekly, light_pct)}
    for key, test in (("last_quality_date", lambda x: x.get("intensity") in ("quality", "race")),
                      ("last_all_out_date", lambda x: x.get("all_out") or x.get("intensity") == "race")):
        days = [x["date"] for x in previous["sessions"] if test(x)]
        if days:
            chained[key] = max(days)
    return chained


def _scheduled_sessions(conn, first: str, last: str, paces, swim, easy, skip=()) -> list[dict]:
    rows = conn.execute(
        """SELECT s.date, w.name, w.payload_json FROM coach_schedules s
           JOIN coach_workouts w ON w.workout_id = s.workout_id WHERE s.date BETWEEN ? AND ?""",
        (first, last)).fetchall()
    return [workouts.to_session(row["date"], json.loads(row["payload_json"]), paces,
                                swim_s_per_100m=swim, easy_s_per_km=easy)
            for row in rows if (row["date"], row["name"]) not in skip]


def _with_rest_of_week(conn, used: dict, monday: str, today: date, paces, swim, easy, light_pct) -> dict:
    """The week before `monday` is still running: count what is done (already in `used`)
    plus the [TP] sessions still to come, so the ramp is not measured against half a week."""
    ran_today = conn.execute("SELECT 1 FROM activities WHERE type LIKE '%running%' "
                             "AND substr(start_time, 1, 10) = ?", (today.isoformat(),)).fetchone()
    first = today + timedelta(days=1) if ran_today else today
    rest = _scheduled_sessions(conn, first.isoformat(), _shift(monday, -1), paces, swim, easy)
    runs = [x for x in rest if x["sport"] == "run"]
    if not rest:
        return used
    weekly = list(used["weekly_run_km"])
    done_km = weekly[-1]
    weekly[-1] = round(done_km + sum(x.get("km") or 0 for x in runs), 1)
    updated = {**used, "weekly_run_km": weekly,
               "longest_run_km_30d": max([used.get("longest_run_km_30d") or 0] + [x.get("km") or 0 for x in runs]),
               "weeks_since_light": history.weeks_since_light(weekly, light_pct),
               "current_week": {"week_start": _shift(monday, -7), "run_km_done": done_km,
                                "run_km_still_planned": round(weekly[-1] - done_km, 1),
                                "planned_from": first.isoformat()}}
    for key, test in (("last_quality_date", lambda x: x.get("intensity") in ("quality", "race")),
                      ("last_all_out_date", lambda x: x.get("all_out") or x.get("intensity") == "race")):
        days = [x["date"] for x in rest if test(x)]
        if days:
            updated[key] = max(days)
    return updated


def validate_plan(conn, built: list[dict], paces: dict | None, today: date | None = None) -> list[dict]:
    """Run the coaching validator on every week the upload touches: the new items plus the
    [TP] workouts already scheduled that week. built: [{date, payload, session?}].
    Runs without a pace target are measured at the athlete's own recent easy pace."""
    from garmin_ai.coaching import validate_week
    today = today or date.today()
    rules = history.rules(conn)
    light_pct = rules["light_week"]["reduction_pct_min"]
    weeks: dict[str, list] = {}
    new = {(b["date"], b["payload"]["workoutName"]) for b in built}
    from garmin_ai.coaching import swim as swimming
    first_day = min(today.isoformat(), min(b["date"] for b in built))
    swim = swimming.recent_pace(conn, first_day) or \
        rules.get("swim", {}).get("default_pace_s_per_100m", workouts.SWIM_S_PER_100M)
    easy = history.recent_easy_pace(conn, first_day)
    for b in built:
        weeks.setdefault(_monday(b["date"]), []).append(
            workouts.to_session(b["date"], b["payload"], paces, b.get("session"), swim, easy))
    for monday, sessions in weeks.items():
        sessions.extend({**x, "already_scheduled": True}
                        for x in _scheduled_sessions(conn, monday, _shift(monday, 6), paces, swim, easy, new))
    results, previous = [], None
    for monday in sorted(weeks):
        sessions = sorted(weeks[monday], key=lambda x: x["date"])
        workouts.mark_long_runs(sessions)
        used = history.plan_history(conn, monday, light_pct, rules["ramp"]["peak_lookback_weeks"])
        if previous and previous["week_start"] == _shift(monday, -7):
            used = _chain(used, previous, light_pct)
        elif date.fromisoformat(_shift(monday, -7)) <= today < date.fromisoformat(monday):
            used = _with_rest_of_week(conn, used, monday, today, paces, swim, easy, light_pct)
        used["easy_pace_used"] = workouts.format_pace(easy) if easy else "VDOT easy range (too few easy runs)"
        used["swim_pace_used"] = f"{workouts.format_pace(swim)}/100 m"
        from garmin_ai.coaching import review
        used["season_phase"] = review.season(conn, monday, rules).get("phase")
        result = validate_week({"week_start": monday, "sessions": sessions}, used, rules)
        from garmin_ai.coaching import season
        target = season.targets_for(conn, monday, (used.get("weekly_run_km") or [None])[-1])
        if target:
            result["warnings"].extend(season.compare(target, result["summary"], sessions))
            result["season_target"] = target
        if any(x.get("estimate_partial") for x in sessions):
            result["warnings"].append({"rule": "estimate", "message": "Some runs have lap-button or "
                                       "untargeted steps, so their km and minutes are estimates."})
        previous = {"week_start": monday, **result, "sessions": sessions, "history_used": used}
        results.append(previous)
    return results


def _shift(day: str, days: int) -> str:
    return (date.fromisoformat(day) + timedelta(days=days)).isoformat()


def upload_plan(items, dry_run=False, allow_errors=False) -> dict:
    if not isinstance(items, list) or not items:
        raise CoachError("items must be a non-empty list of {date, workout}.")
    built = []
    for index, item in enumerate(items, start=1):  # validate everything before uploading anything
        if not isinstance(item, dict) or not isinstance(item.get("workout"), dict):
            raise CoachError(f"Item {index} needs date and workout {{sport, name, steps, ...}}.")
        spec = item["workout"]
        try:
            payload = workouts.build_workout(spec.get("sport"), spec.get("name"), spec.get("steps"),
                                             spec.get("description"), spec.get("pool_length_m"))
        except WorkoutError as error:
            raise CoachError(f"Item {index} ({item.get('date')}): {error}") from None
        extra = item.get("session") or {}
        if not isinstance(extra, dict) or set(extra) - set(SESSION_FIELDS):
            raise CoachError(f"Item {index}: session may only set {', '.join(SESSION_FIELDS)}.")
        built.append({"date": _date(item.get("date")), "payload": payload, "session": extra})
    keys = [(b["date"], b["payload"]["workoutName"]) for b in built]
    if len(set(keys)) != len(keys):
        raise CoachError("Two items have the same date and name; give them different names.")

    with db.get_connection() as conn:
        earliest = date.fromisoformat(min(b["date"] for b in built))
        paces = current_paces()
        validation = validate_plan(conn, built, paces)
        errors = [f"week {v['week_start']}: {e['message']}" for v in validation for e in v["errors"]]
        warnings = ([f"week {v['week_start']}: {w['message']}" for v in validation for w in v["warnings"]]
                    + workouts.pace_warnings([{"date": b["date"], "workout": b["payload"]} for b in built], paces))
        if errors and not dry_run and not allow_errors:
            raise CoachError("The plan breaks coaching rules; nothing was uploaded:\n- " + "\n- ".join(errors)
                             + "\nFix them, or pass allow_errors=true once the athlete has agreed.")
        results, api = [], None
        for item in built:
            payload, day = item["payload"], item["date"]
            existing = conn.execute(
                """SELECT w.workout_id, w.payload_hash, s.schedule_id FROM coach_schedules s
                   JOIN coach_workouts w ON w.workout_id = s.workout_id WHERE s.date = ? AND w.name = ?""",
                (day, payload["workoutName"])).fetchone()
            entry = {"date": day, "name": payload["workoutName"], "summary": workouts.summary(payload)}
            if existing and existing["payload_hash"] == workouts.payload_hash(payload):
                entry.update(action="unchanged", workout_id=int(existing["workout_id"]),
                             schedule_id=existing["schedule_id"])
            elif existing:
                entry.update(action="update", workout_id=int(existing["workout_id"]),
                             schedule_id=existing["schedule_id"])
            else:
                entry["action"] = "create"
            if dry_run:
                entry["garmin_payload"] = payload
            results.append(entry)
        if dry_run:
            return {"dry_run": True, "errors": errors, "warnings": warnings, "items": results,
                    "validation": validation}

        for entry, item in zip(results, built):
            try:
                if entry["action"] == "create":
                    api = api or client()
                    entry.update(_upload(api, conn, item["payload"]))
                    entry["schedule_id"] = _schedule(api, conn, entry["workout_id"], item["date"])
                elif entry["action"] == "update":
                    api = api or client()
                    _owned(conn, entry["workout_id"])
                    _put(api, conn, entry["workout_id"], item["payload"])
            except (CoachError, WorkoutError) as error:
                entry.update(action="failed", error=str(error))
            except Exception as error:
                entry.update(action="failed", error=f"Garmin error: {error}")
        if api:
            _refresh_calendar(api, conn, [b["date"] for b in built])
        changed = [e for e in results if e["action"] in ("create", "update")]
        if changed:  # keep a coaching-history record of every real upload
            monday = earliest - timedelta(days=earliest.weekday())
            history.add_entry(conn, "upload", f"Uploaded {len(changed)} workout(s) "
                              f"{min(e['date'] for e in changed)}..{max(e['date'] for e in changed)}.",
                              week_start=monday.isoformat(),
                              planned="; ".join(f"{e['date']} {e['name']}" for e in results),
                              details={"items": [{k: e.get(k) for k in ("date", "name", "action", "workout_id",
                                                                        "summary", "error")} for e in results],
                                       "errors_allowed": errors, "warnings": warnings,
                                       "weeks": [{"week_start": v["week_start"], **v["summary"]} for v in validation]})
    return {"errors_allowed": errors, "warnings": warnings, "items": results, "validation": validation}
