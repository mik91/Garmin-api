import asyncio
import copy
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch

from garmin_ai import coach, config, db, mcp_server


def week(name="Easy", minutes=40):
    return {"sport": "running", "name": name,
            "steps": [{"type": "interval", "duration": {"time_s": minutes * 60}, "target": {"hr_zone": 2}}]}


class FakeGarmin:
    """In-memory Garmin Connect: the library methods coach.py uses, plus a Runna workout."""
    garmin_workouts = "/workout-service"

    def __init__(self):
        self.workouts = {500: {"workoutId": 500, "workoutName": "W2 Tue Intervals", "workoutSegments": []}}
        self.schedules = {9500: (500, "2026-11-17")}
        self.next_id, self.calls = 1000, []
        self.mangle = None
        self.client = self

    def upload_workout(self, payload):
        self.next_id += 1
        stored = copy.deepcopy(payload)
        if self.mangle:
            self.mangle(stored)
        self.workouts[self.next_id] = {**stored, "workoutId": self.next_id, "ownerId": 7}
        self.calls.append(("upload", self.next_id))
        return {"workoutId": self.next_id}

    def get_workout_by_id(self, workout_id):
        return copy.deepcopy(self.workouts[int(workout_id)])

    def put(self, _domain, path, json=None, api=True):
        workout_id = int(path.rsplit("/", 1)[1])
        self.calls.append(("put", workout_id))
        self.workouts[workout_id] = copy.deepcopy(json)

    def delete_workout(self, workout_id):
        self.calls.append(("delete", int(workout_id)))
        del self.workouts[int(workout_id)]

    def schedule_workout(self, workout_id, day):
        self.next_id += 1
        self.schedules[self.next_id] = (int(workout_id), day)
        self.calls.append(("schedule", int(workout_id), day))
        return {"workoutScheduleId": self.next_id, "calendarDate": day}

    def unschedule_workout(self, schedule_id):
        self.calls.append(("unschedule", int(schedule_id)))
        del self.schedules[int(schedule_id)]

    def get_scheduled_workouts(self, year, month):
        # The real calendar shape: schedule id in "id", activities mixed in.
        items = [{"itemType": "activity", "id": 1, "title": "Morning Run", "date": f"{year}-{month:02d}-01"}]
        items += [{"itemType": "workout", "id": sid, "workoutId": wid, "date": day, "sportTypeKey": "running",
                   "title": self.workouts.get(wid, {}).get("workoutName")}
                  for sid, (wid, day) in self.schedules.items() if day.startswith(f"{year}-{month:02d}")]
        return {"year": year, "month": month - 1, "calendarItems": items}


class CoachTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        self.api = FakeGarmin()
        self.stack = ExitStack()
        for target in (config, db):
            self.stack.enter_context(patch.object(target, "DB_PATH", self.path))
        self.stack.enter_context(patch.object(coach, "client", lambda: self.api))
        with db.get_connection():
            pass

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def rows(self, sql):
        with closing(sqlite3.connect(self.path)) as conn:
            return conn.execute(sql).fetchall()

    def test_dry_run_never_calls_garmin(self):
        with patch.object(coach, "client", side_effect=AssertionError("network")):
            result = coach.create_workout("running", "Test", week()["steps"], dry_run=True)
            plan = coach.upload_plan([{"date": "2026-11-18", "workout": week()}], dry_run=True)
        self.assertEqual(result["garmin_payload"]["workoutName"], "[CP] Test")
        self.assertEqual(plan["items"][0]["action"], "create")
        self.assertEqual(self.rows("SELECT COUNT(*) FROM coach_workouts"), [(0,)])

    def test_create_schedule_list_and_delete(self):
        result = coach.create_workout("running", "Test", week()["steps"], date_="2026-11-18")
        workout_id = result["workout_id"]
        self.assertEqual(self.api.workouts[workout_id]["workoutName"], "[CP] Test")
        self.assertEqual(self.rows("SELECT workout_id, date FROM coach_schedules"), [(str(workout_id), "2026-11-18")])
        planned = coach.list_planned("2026-11-01", "2026-11-30")
        self.assertEqual((planned[0]["name"], planned[0]["schedule_id"]), ("[CP] Test", result["schedule_id"]))
        calendar = json.loads(mcp_server.training_calendar("2026-11-01", "2026-11-30"))
        flags = {plan["name"]: plan["created_by_coach"] for plan in calendar}
        self.assertEqual(flags, {"[CP] Test": True, "W2 Tue Intervals": False})

        deleted = coach.delete_workout(workout_id)
        self.assertEqual(deleted["unscheduled_dates"], ["2026-11-18"])
        self.assertNotIn(workout_id, self.api.workouts)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM coach_workouts"), [(0,)])
        self.assertIn(500, self.api.workouts)  # Runna untouched

    def test_plan_payload_lists_coach_workouts_with_steps(self):
        steps = [{"type": "warmup", "duration": {"time_s": 900}, "target": {"hr_bpm": [105, 148]}},
                 {"type": "repeat", "repeat": {"count": 4, "steps": [
                     {"type": "interval", "duration": {"time_s": 300}, "target": {"pace_min_per_km": ["4:12", "4:22"]},
                      "notes": "Controlled"},
                     {"type": "recovery", "duration": {"time_s": 60}}]}},
                 {"type": "cooldown", "duration": {"time_s": 600}}]
        coach.create_workout("running", "Threshold", steps, date_="2026-11-17")
        coach.create_workout("running", "Long", week(minutes=70)["steps"], date_="2026-11-21")
        coach.create_workout("lap_swimming", "Swim", [{"type": "interval", "duration": {"distance_m": 400},
                                                       "swim": {"stroke": "free"}}], date_="2026-11-19")
        payload = coach.plan_payload({"start": ["2026-11-01"], "end": ["2026-11-30"]})
        items = {item["name"]: item for item in payload["workouts"]}
        self.assertEqual(set(items), {"Threshold", "Long", "Swim"})  # prefix stripped, Runna left out
        self.assertEqual(payload["plan_start"], "2026-11-17")
        self.assertEqual((items["Long"]["category"], items["Swim"]["category"]), ("long", "swim"))
        threshold = items["Threshold"]
        self.assertEqual(threshold["duration_s"], 49 * 60)
        self.assertEqual([s["kind"] for s in threshold["steps"]], ["warmup", "repeat", "cooldown"])
        rep = threshold["steps"][1]
        self.assertEqual((rep["count"], rep["steps"][0]["seconds"], rep["steps"][0]["notes"]), (4, 300, "Controlled"))
        self.assertEqual(rep["steps"][0]["pace_s_per_km"], [252, 262])
        with self.assertRaises(ValueError):
            coach.plan_payload({"start": ["2026-12-01"], "end": ["2026-11-01"]})

    def test_refuses_workouts_it_did_not_create(self):
        for operation in (lambda: coach.delete_workout(500), lambda: coach.schedule_workout(500, "2026-11-20"),
                          lambda: coach.update_workout(500, name="x"), lambda: coach.unschedule(schedule_id=9500)):
            with self.assertRaisesRegex(coach.CoachError, "not (created|scheduled) by these tools"):
                operation()
        # A recorded id whose Garmin name lost the prefix is refused too.
        created = coach.create_workout("running", "Mine", week()["steps"])
        self.api.workouts[created["workout_id"]]["workoutName"] = "Renamed by hand"
        with self.assertRaisesRegex(coach.CoachError, "without the\n?.*prefix"):
            coach.delete_workout(created["workout_id"])
        self.assertEqual(self.api.schedules[9500], (500, "2026-11-17"))
        self.assertEqual([c for c in self.api.calls if c[0] in ("delete", "unschedule")], [])

    def test_unschedule_by_date_only_removes_own_workouts(self):
        coach.create_workout("running", "Mine", week()["steps"], date_="2026-11-17")
        removed = coach.unschedule(day="2026-11-17")["removed"]
        self.assertEqual(len(removed), 1)
        self.assertIn(9500, self.api.schedules)  # Runna's workout on the same day stays
        self.assertIn("never touched", coach.unschedule(day="2026-11-17")["note"])

    def test_move_reschedules_then_removes_the_old_entry_and_logs_it(self):
        from datetime import date, timedelta
        first, later = ((date.today() + timedelta(days=n)).isoformat() for n in (3, 5))
        created = coach.create_workout("running", "Long", week(minutes=90)["steps"], date_=first)
        moved = coach.move(created["schedule_id"], later)
        self.assertEqual((moved["from"], moved["date"]), (first, later))
        self.assertNotEqual(moved["schedule_id"], created["schedule_id"])
        self.assertEqual(self.rows("SELECT schedule_id, date FROM coach_schedules"), [(moved["schedule_id"], later)])
        self.assertNotIn(int(created["schedule_id"]), self.api.schedules)
        self.assertEqual([c[0] for c in self.api.calls[-2:]], ["schedule", "unschedule"])  # new day first
        log = self.rows("SELECT kind, summary FROM coach_log")
        self.assertEqual(log, [("adjustment", f"The athlete moved Long from {first} to {later} in the app.")])

        with self.assertRaisesRegex(coach.CoachError, "today or a later"):
            coach.move(moved["schedule_id"], (date.today() - timedelta(days=1)).isoformat())
        with self.assertRaisesRegex(coach.CoachError, "not scheduled by these tools"):
            coach.move("9500", later)  # Runna
        with self.assertRaisesRegex(ValueError, "schedule_id"):
            coach.move_request({"date": later})

    def test_upload_plan_is_idempotent_and_updates_changes(self):
        items = [{"date": "2026-11-18", "workout": week("Easy")}, {"date": "2026-11-20", "workout": week("Long", 90)}]
        first = coach.upload_plan(items)
        self.assertEqual([i["action"] for i in first["items"]], ["create", "create"])
        again = coach.upload_plan(items)
        self.assertEqual([i["action"] for i in again["items"]], ["unchanged", "unchanged"])
        items[1]["workout"] = week("Long", 100)
        changed = coach.upload_plan(items)
        self.assertEqual([i["action"] for i in changed["items"]], ["unchanged", "update"])
        long_id = changed["items"][1]["workout_id"]
        step = self.api.workouts[long_id]["workoutSegments"][0]["workoutSteps"][0]
        self.assertEqual(step["endConditionValue"], 6000.0)
        self.assertEqual(self.rows("SELECT COUNT(*) FROM coach_workouts"), [(2,)])
        self.assertEqual(len([c for c in self.api.calls if c[0] == "upload"]), 2)

    def test_upload_plan_validates_everything_first(self):
        bad = {"sport": "running", "name": "Bad", "steps": [{"type": "interval", "duration": {"reps": 3}}]}
        with self.assertRaisesRegex(coach.CoachError, "Item 2"):
            coach.upload_plan([{"date": "2026-11-18", "workout": week()}, {"date": "2026-11-19", "workout": bad}])
        self.assertEqual(self.api.calls, [])

    def test_read_back_mismatch_is_an_error_and_cleaned_up(self):
        def mangle(stored):
            stored["workoutSegments"][0]["workoutSteps"][0]["endConditionValue"] = 60.0
        self.api.mangle = mangle
        with self.assertRaisesRegex(coach.CoachError, "differently.*deleted again"):
            coach.create_workout("running", "Test", week()["steps"])
        self.assertEqual(self.rows("SELECT COUNT(*) FROM coach_workouts"), [(0,)])
        self.assertEqual([c[0] for c in self.api.calls], ["upload", "delete"])

    def test_tool_errors_are_readable(self):
        text = mcp_server.delete_workout(500)
        self.assertTrue(text.startswith("Error: Workout 500 was not created by these tools"))
        text = mcp_server.create_workout("running", "x", [{"type": "jog"}], dry_run=True)
        self.assertTrue(text.startswith("Error: Unknown step type"))


class RegistrationTests(unittest.TestCase):
    def names(self, server):
        return {tool.name: tool for tool in asyncio.run(server.list_tools())}

    def test_write_tools_are_hosted_only(self):
        local = self.names(mcp_server.build_server())
        self.assertIn("training_paces", local)
        self.assertNotIn("create_workout", local)
        self.assertNotIn("list_planned_workouts", local)
        with patch.object(config, "PUBLIC_URL", "https://example.test"):
            hosted = self.names(mcp_server.build_server(hosted=True))
        for name in ("create_workout", "schedule_workout", "upload_plan", "update_workout",
                     "unschedule_workout", "delete_workout", "list_planned_workouts"):
            self.assertIn(name, hosted)
        self.assertTrue(hosted["delete_workout"].annotations.destructiveHint)
        self.assertTrue(hosted["list_planned_workouts"].annotations.readOnlyHint)
        self.assertTrue(local["training_calendar"].annotations.readOnlyHint)
        self.assertIn("pace_min_per_km", hosted["create_workout"].description)


if __name__ == "__main__":
    unittest.main()
