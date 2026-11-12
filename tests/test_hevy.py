import io
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

from starlette.testclient import TestClient

from garmin_ai import config, db, hevy, mcp_server, sync, web_dashboard
from garmin_ai.web_server import create_app


def workout(workout_id, start, end, sets, title="Push", updated="2026-10-20T18:30:00Z"):
    return {"id": workout_id, "title": title, "description": None, "start_time": start, "end_time": end,
            "updated_at": updated, "created_at": start,
            "exercises": [{"index": 0, "title": "Bench Press (Barbell)", "notes": "", "exercise_template_id": "79D0BB3A",
                           "superset_id": None, "sets": sets}]}


SETS = [
    {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "distance_meters": None, "duration_seconds": None, "rpe": None},
    {"index": 1, "type": "normal", "weight_kg": 80, "reps": 8, "distance_meters": None, "duration_seconds": None, "rpe": 8},
    {"index": 2, "type": "failure", "weight_kg": 80, "reps": 6, "distance_meters": None, "duration_seconds": None, "rpe": 10},
]
TEMPLATES = [{"id": "79D0BB3A", "title": "Bench Press (Barbell)", "type": "weight_reps", "primary_muscle_group": "chest",
              "secondary_muscle_groups": ["triceps", "shoulders"], "is_custom": False, "equipment": "barbell",
              "thumbnail_url": "https://example.com/bench.jpg"}]


class FakeHevy:
    """Serves canned pages the way the Hevy API does (page_count, 404 past the last page)."""

    def __init__(self, workouts=(), events=(), templates=TEMPLATES):
        self.data = {"/workouts": ("workouts", list(workouts)), "/workouts/events": ("events", list(events)),
                     "/exercise_templates": ("exercise_templates", list(templates))}
        self.calls = []

    def get(self, path, page=1, pageSize=10, **params):
        self.calls.append((path, page, pageSize, params))
        key, items = self.data[path]
        page_count = (len(items) + pageSize - 1) // pageSize
        if page > max(page_count, 1):
            raise hevy.HevyError("Hevy API error 404.", 404)
        return {"page": page, "page_count": page_count, key: items[(page - 1) * pageSize:page * pageSize]}

    pages = hevy.HevyClient.pages


def garmin_activity(conn, activity_id, kind, local, gmt, duration_s, **extra):
    conn.execute(
        "INSERT INTO activities(activity_id, start_time, type, duration_s, avg_hr, max_hr, aerobic_te, anaerobic_te, "
        "calories, raw_json) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (activity_id, local, kind, duration_s, extra.get("avg_hr"), extra.get("max_hr"), extra.get("aerobic_te"),
         extra.get("anaerobic_te"), extra.get("calories"),
         json.dumps({"activityName": extra.get("name"), "startTimeGMT": gmt, "activityTrainingLoad": 55.5})))


class HevySyncTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.conn.close()

    def count(self, table):
        return self.conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]

    def test_backfill_pages_through_workouts_and_stores_sets(self):
        workouts = [workout(f"w{i}", f"2026-10-{i + 1:02d}T17:00:00Z", f"2026-10-{i + 1:02d}T18:00:00Z", SETS)
                    for i in range(12)]
        client = FakeHevy(workouts)
        self.assertEqual(hevy.sync_workouts(client, self.conn), 12)

        workout_pages = [call for call in client.calls if call[0] == "/workouts"]
        self.assertEqual([call[1] for call in workout_pages], [1, 2])
        self.assertTrue(all(call[2] == 10 for call in workout_pages))
        self.assertEqual(self.count("hevy_workouts"), 12)
        self.assertEqual(self.count("hevy_sets"), 36)
        row = self.conn.execute("SELECT * FROM hevy_sets WHERE workout_id = 'w0' AND set_index = 1").fetchone()
        self.assertEqual((row["set_type"], row["weight_kg"], row["reps"], row["rpe"]), ("normal", 80, 8, 8))
        template = self.conn.execute("SELECT * FROM hevy_exercise_templates").fetchone()
        self.assertEqual(template["primary_muscle_group"], "chest")
        state = db.get_sync_state(self.conn)
        self.assertTrue(state["hevy_events_since"].endswith("Z"))
        self.assertEqual(state["hevy_error"], "")

    def test_incremental_sync_applies_updates_and_deletes(self):
        hevy.sync_workouts(FakeHevy([workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS),
                                     workout("b", "2026-10-21T17:00:00Z", "2026-10-21T18:00:00Z", SETS)]), self.conn)
        since = db.get_sync_state(self.conn)["hevy_events_since"]
        changed = workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS[1:2], title="Push v2",
                          updated="2026-10-22T08:00:00Z")
        client = FakeHevy(events=[{"type": "deleted", "id": "b", "deleted_at": "2026-10-22T09:00:00Z"},
                                  {"type": "updated", "workout": changed}])

        self.assertEqual(hevy.sync_workouts(client, self.conn), 2)
        self.assertEqual(client.calls[0], ("/workouts/events", 1, 10, {"since": since}))
        self.assertNotIn("/workouts", [call[0] for call in client.calls])
        self.assertEqual(self.conn.execute("SELECT title FROM hevy_workouts").fetchall()[0][0], "Push v2")
        self.assertEqual(self.count("hevy_workouts"), 1)
        self.assertEqual(self.count("hevy_sets"), 1)
        # Known templates are not fetched again.
        self.assertNotIn("/exercise_templates", [call[0] for call in client.calls])

    def test_links_overlapping_strength_activity_and_local_time(self):
        # Garmin local time is UTC+2; the strength session starts 5 min before Hevy's.
        garmin_activity(self.conn, 1, "strength_training", "2026-10-20 18:55:00", "2026-10-20 16:55:00", 3900)
        garmin_activity(self.conn, 2, "running", "2026-10-20 19:00:00", "2026-10-20 17:00:00", 3600)
        garmin_activity(self.conn, 3, "running", "2026-10-25 09:00:00", "2026-10-25 07:00:00", 3600)
        hevy.sync_workouts(FakeHevy([workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS),
                                     workout("b", "2026-10-26T06:30:00Z", "2026-10-26T07:15:00Z", SETS)]), self.conn)

        rows = {r["workout_id"]: r for r in self.conn.execute("SELECT * FROM hevy_workouts")}
        self.assertEqual(rows["a"]["garmin_activity_id"], 1)
        self.assertEqual(rows["a"]["start_local"], "2026-10-20T19:00:00")
        # No strength activity overlaps b; its local time uses the nearest Garmin activity's zone.
        self.assertIsNone(rows["b"]["garmin_activity_id"])
        self.assertEqual(rows["b"]["start_local"], "2026-10-26T08:30:00")

    def test_failures_are_recorded_and_garmin_failure_still_syncs_hevy(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = str(Path(directory) / "garmin.db")
            stack.enter_context(patch.object(config, "DB_PATH", path))
            stack.enter_context(patch.object(db, "DB_PATH", path))
            stack.enter_context(patch.object(config, "HEVY_API_KEY", "test-key"))
            stack.enter_context(patch.object(hevy, "HevyClient", lambda key: FakeHevy(
                [workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS)])))
            stack.enter_context(patch.object(sync, "login", side_effect=RuntimeError("Garmin down")))
            with self.assertRaises(RuntimeError):
                sync.sync(1)
            with db.get_connection() as conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM hevy_workouts").fetchone()[0], 1)

            def broken(key):
                raise hevy.HevyError("Hevy rejected the API key. Check HEVY_API_KEY (needs Hevy Pro).", 401)
            with patch.object(hevy, "HevyClient", broken):
                self.assertIsNone(hevy.sync_if_configured())
            with db.get_connection() as conn:
                self.assertIn("rejected", db.get_sync_state(conn)["hevy_error"])

    def test_old_template_rows_are_migrated_and_refetched(self):
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            path = str(Path(directory) / "garmin.db")
            with closing(sqlite3.connect(path)) as conn, conn:
                # The first release's table, without equipment, thumbnail_url or fetched_at.
                conn.execute("CREATE TABLE hevy_exercise_templates (template_id TEXT PRIMARY KEY, title TEXT, type TEXT, "
                             "primary_muscle_group TEXT, secondary_muscle_groups_json TEXT, is_custom INTEGER)")
                conn.execute("INSERT INTO hevy_exercise_templates (template_id, title) VALUES ('79D0BB3A', 'Bench')")
            stack.enter_context(patch.object(config, "DB_PATH", path))
            stack.enter_context(patch.object(db, "DB_PATH", path))
            with db.get_connection() as conn:
                hevy.store_workout(conn, workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS))
                now = hevy.datetime.now(hevy.timezone.utc)
                self.assertTrue(hevy._templates_stale(conn, now))
                client = FakeHevy()
                hevy._sync_templates(client, conn)
                row = conn.execute("SELECT * FROM hevy_exercise_templates").fetchone()
                self.assertEqual((row["title"], row["equipment"], row["thumbnail_url"]),
                                 ("Bench Press (Barbell)", "barbell", "https://example.com/bench.jpg"))
                self.assertFalse(hevy._templates_stale(conn, now))
                self.assertTrue(hevy._templates_stale(conn, now + hevy.TEMPLATE_MAX_AGE + hevy.timedelta(hours=1)))

    def test_sync_is_skipped_without_api_key(self):
        with patch.object(config, "HEVY_API_KEY", ""), patch.object(hevy, "HevyClient") as client:
            self.assertIsNone(hevy.sync_if_configured())
        client.assert_not_called()


class WeightUnitTests(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.executescript(db.SCHEMA)
        self.unit = patch.object(config, "HEVY_WEIGHT_UNIT", "")
        self.unit.start()

    def tearDown(self):
        self.unit.stop()
        self.conn.close()

    def log(self, *weights):
        self.conn.executemany("INSERT INTO hevy_sets(workout_id, exercise_index, set_index, weight_kg) VALUES('w', 0, ?, ?)",
                              list(enumerate(weights)))

    def test_detects_pounds_from_converted_weights(self):
        # Hevy's own conversions of 185, 135, 45 and 22.5 lb, plus a bodyweight set and one kg-like outlier.
        self.log(83.91468824559335, 135 * hevy.KG_PER_LB, 45 * hevy.KG_PER_LB, 22.5 * hevy.KG_PER_LB, 0, None, 20)
        self.assertEqual(hevy.weight_unit(self.conn), "lb")

    def test_detects_kilograms(self):
        self.log(60, 62.5, 81.25, 100, 22.5 * hevy.KG_PER_LB)
        self.assertEqual(hevy.weight_unit(self.conn), "kg")

    def test_no_weights_means_kg(self):
        self.log(0, None)
        self.assertEqual(hevy.weight_unit(self.conn), "kg")

    def test_config_overrides_detection(self):
        self.log(83.91468824559335, 135 * hevy.KG_PER_LB)
        with patch.object(config, "HEVY_WEIGHT_UNIT", "kg"):
            self.assertEqual(hevy.weight_unit(self.conn), "kg")
        self.conn.execute("DELETE FROM hevy_sets")
        self.log(60, 100)
        with patch.object(config, "HEVY_WEIGHT_UNIT", "lb"):
            self.assertEqual(hevy.weight_unit(self.conn), "lb")


class HevyClientTests(unittest.TestCase):
    def test_sends_api_key_header_and_maps_auth_errors(self):
        seen = []

        def fake_urlopen(request, timeout):
            seen.append(request)
            return io.BytesIO(b'{"page": 1, "page_count": 1, "workouts": []}')

        client = hevy.HevyClient("secret", base_url="https://api.hevyapp.com/v1")
        with patch.object(hevy, "urlopen", fake_urlopen):
            self.assertEqual(list(client.pages("/workouts", "workouts")), [])
        self.assertEqual(seen[0].get_header("Api-key"), "secret")
        self.assertEqual(seen[0].full_url, "https://api.hevyapp.com/v1/workouts?page=1&pageSize=10")

        def unauthorized(request, timeout):
            raise HTTPError(request.full_url, 401, "Unauthorized", {}, None)
        with patch.object(hevy, "urlopen", unauthorized), self.assertRaises(hevy.HevyError) as caught:
            client.get("/workouts")
        self.assertEqual(caught.exception.status, 401)
        self.assertNotIn("secret", str(caught.exception))


class StrengthPayloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)
            garmin_activity(conn, 1, "strength_training", "2026-10-20 19:00:00", "2026-10-20 17:00:00", 3600,
                            avg_hr=112, max_hr=151, aerobic_te=2.1, anaerobic_te=1.4, calories=320, name="Strength")
            hevy.sync_workouts(FakeHevy([
                workout("a", "2026-10-20T17:00:00Z", "2026-10-20T18:00:00Z", SETS),
                workout("b", "2026-10-22T17:00:00Z", "2026-10-22T17:45:00Z", SETS[1:2], title="Pull"),
                workout("old", "2026-09-01T17:00:00Z", "2026-09-01T18:00:00Z", SETS)]), conn)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, "DB_PATH", self.path))
        self.stack.enter_context(patch.object(config, "HEVY_API_KEY", "test-key"))
        self.stack.enter_context(patch.object(config, "HEVY_WEIGHT_UNIT", ""))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_payload_shape_and_totals(self):
        result = hevy.payload({"start": ["2026-10-19"], "end": ["2026-10-26"]})
        json.dumps(result, allow_nan=False)
        self.assertEqual([w["id"] for w in result["workouts"]], ["b", "a"])  # newest first
        push = result["workouts"][1]
        self.assertEqual((push["date"], push["start_local"], push["duration_s"]), ("2026-10-20", "2026-10-20T19:00:00", 3600))
        self.assertEqual((push["sets"], push["reps"], push["volume_kg"]), (2, 14, 1120.0))  # warm-up not counted
        exercise = push["exercises"][0]
        self.assertEqual(exercise["primary_muscle_group"], "chest")
        self.assertEqual(exercise["secondary_muscle_groups"], ["triceps", "shoulders"])
        self.assertEqual((exercise["equipment"], exercise["thumbnail_url"]), ("barbell", "https://example.com/bench.jpg"))
        self.assertEqual(len(exercise["sets"]), 3)
        self.assertEqual(exercise["sets"][0], {"index": 0, "type": "warmup", "weight_kg": 40, "reps": 10, "rpe": None,
                                               "distance_m": None, "duration_s": None})
        self.assertEqual(push["garmin"], {"activity_id": 1, "name": "Strength", "type": "strength_training",
                                          "start_time": "2026-10-20 19:00:00", "duration_s": 3600, "calories": 320,
                                          "avg_hr": 112, "max_hr": 151, "aerobic_te": 2.1, "anaerobic_te": 1.4,
                                          "training_load": 55.5})
        self.assertIsNone(result["workouts"][0]["garmin"])
        self.assertEqual(result["weekly"], [
            {"week_start": "2026-10-19", "sessions": 2, "sets": 3, "reps": 22, "volume_kg": 1760.0, "duration_s": 6300},
            {"week_start": "2026-10-26", "sessions": 0, "sets": 0, "reps": 0, "volume_kg": 0, "duration_s": 0}])
        self.assertEqual(result["sync"]["configured"], True)
        self.assertEqual(result["weight_unit"], "kg")
        self.assertNotIn("raw_json", json.dumps(result))

    def test_rejects_bad_ranges(self):
        for query in ({"start": ["2026-10-30"], "end": ["2026-10-01"]}, {"start": ["yesterday"]},
                      {"start": ["2020-01-01"], "end": ["2026-01-01"]}):
            with self.assertRaises(ValueError):
                hevy.payload(query)

    def test_mcp_tool_returns_same_data(self):
        result = json.loads(mcp_server.strength_workouts("2026-10-19", "2026-10-26"))
        self.assertEqual(len(result["workouts"]), 2)
        self.assertEqual(result["weight_unit"], "kg")

    def test_endpoint_accepts_read_only_bearer_token(self):
        from garmin_ai import auth
        folder = Path(self.temp.name)
        for name, value in {"HOSTED": True, "PUBLIC_URL": "https://garmin.example.com",
                            "APP_PASSWORD_HASH": auth.hash_password("pw"), "AUTH_DB_PATH": str(folder / "auth.db")}.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(web_dashboard, "start_auto_sync", return_value=None))
        client = self.stack.enter_context(TestClient(create_app(), base_url="https://garmin.example.com"))
        self.assertEqual(client.get("/api/strength").status_code, 401)
        with patch.object(auth, "bearer_valid", return_value=True):
            response = client.get("/api/strength?start=2026-10-19&end=2026-10-26", headers={"Authorization": "Bearer t"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["workouts"]), 2)
            bad = client.get("/api/strength?start=2026-10-30&end=2026-10-01", headers={"Authorization": "Bearer t"})
            self.assertEqual(bad.status_code, 400)


def exercise(template_id, title, sets):
    return {"title": title, "exercise_template_id": template_id, "superset_id": None, "notes": "", "sets": sets}


def logged(set_type="normal", weight_kg=None, reps=None, duration_seconds=None):
    return {"type": set_type, "weight_kg": weight_kg, "reps": reps, "distance_meters": None,
            "duration_seconds": duration_seconds, "rpe": None}


PROGRESS_TEMPLATES = TEMPLATES + [
    {"id": "PULLUP", "title": "Pull Up", "type": "reps_only", "primary_muscle_group": "lats",
     "secondary_muscle_groups": [], "equipment": "none", "thumbnail_url": None},
    {"id": "PLANK", "title": "Plank", "type": "duration", "primary_muscle_group": "abdominals",
     "secondary_muscle_groups": [], "equipment": "none", "thumbnail_url": None},
    {"id": "UNUSED", "title": "Deadlift (Barbell)", "type": "weight_reps", "primary_muscle_group": "lower_back",
     "secondary_muscle_groups": [], "equipment": "barbell", "thumbnail_url": None}]


class ExerciseProgressTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        first = {**workout("w1", "2026-10-01T17:00:00Z", "2026-10-01T18:00:00Z", []), "exercises": [
            exercise("79D0BB3A", "Bench Press (Barbell)", [
                logged("warmup", 100, 1), logged("normal", 80, 8), logged("normal", 85, 5), logged("failure", 85, 6)]),
            exercise("PULLUP", "Pull Up", [logged(reps=10), logged(reps=12)]),
            exercise("PLANK", "Plank", [logged(duration_seconds=60), logged(duration_seconds=90)])]}
        second = {**workout("w2", "2026-10-08T17:00:00Z", "2026-10-08T18:00:00Z", [], title="Push B"), "exercises": [
            exercise("79D0BB3A", "Bench Press (Barbell)", [logged("normal", 90, 15), logged("dropset", 60, 12)])]}
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.row_factory = sqlite3.Row
            conn.executescript(db.SCHEMA)
            # Listed newest first, as Hevy does; sessions must still come back oldest first.
            hevy.sync_workouts(FakeHevy([second, first], templates=PROGRESS_TEMPLATES), conn)
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(config, "DB_PATH", self.path))
        self.stack.enter_context(patch.object(config, "HEVY_WEIGHT_UNIT", ""))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_weighted_exercise_sessions_and_records(self):
        result = hevy.exercise_progress({"template_id": ["79D0BB3A"]})
        json.dumps(result, allow_nan=False)
        self.assertEqual(result["template"], {"id": "79D0BB3A", "title": "Bench Press (Barbell)", "type": "weight_reps",
                                              "primary_muscle_group": "chest",
                                              "secondary_muscle_groups": ["triceps", "shoulders"],
                                              "equipment": "barbell", "thumbnail_url": "https://example.com/bench.jpg"})
        self.assertEqual(result["weight_unit"], "kg")
        self.assertEqual(result["sessions"], [
            # Warm-up 100 x 1 is ignored; 85 x 6 beats 85 x 5 on reps; e1RM 85 * (1 + 6/30) = 102.
            {"workout_id": "w1", "date": "2026-10-01", "title": "Push", "sets": 3, "reps": 19, "volume_kg": 1575.0,
             "best_set": {"weight_kg": 85, "reps": 6}, "e1rm_kg": 102.0, "max_reps": 8, "max_duration_s": None},
            # 15 reps is outside the e1RM range, so only 60 x 12 counts.
            {"workout_id": "w2", "date": "2026-10-08", "title": "Push B", "sets": 2, "reps": 27, "volume_kg": 2070.0,
             "best_set": {"weight_kg": 90, "reps": 15}, "e1rm_kg": 84.0, "max_reps": 15, "max_duration_s": None}])
        self.assertEqual(result["records"], {
            "heaviest_kg": {"value": 90, "date": "2026-10-08", "workout_id": "w2"},
            "best_e1rm_kg": {"value": 102.0, "date": "2026-10-01", "workout_id": "w1"},
            "most_reps": {"value": 15, "date": "2026-10-08", "workout_id": "w2"},
            "best_volume_kg": {"value": 2070.0, "date": "2026-10-08", "workout_id": "w2"},
            "longest_duration_s": None})

    def test_reps_only_and_duration_exercises(self):
        pull_up = hevy.exercise_progress({"template_id": ["PULLUP"]})
        self.assertEqual(pull_up["sessions"][0]["best_set"], None)
        self.assertEqual(pull_up["sessions"][0]["max_reps"], 12)
        self.assertEqual(pull_up["records"], {"heaviest_kg": None, "best_e1rm_kg": None,
                                              "most_reps": {"value": 12, "date": "2026-10-01", "workout_id": "w1"},
                                              "best_volume_kg": None, "longest_duration_s": None})
        plank = hevy.exercise_progress({"template_id": ["PLANK"]})
        self.assertEqual(plank["sessions"][0]["max_duration_s"], 90)
        self.assertEqual(plank["records"]["longest_duration_s"], {"value": 90, "date": "2026-10-01", "workout_id": "w1"})
        self.assertIsNone(plank["records"]["most_reps"])

    def test_known_template_without_sessions_and_unknown_template(self):
        unused = hevy.exercise_progress({"template_id": ["UNUSED"]})
        self.assertEqual((unused["template"]["title"], unused["sessions"]), ("Deadlift (Barbell)", []))
        self.assertTrue(all(record is None for record in unused["records"].values()))
        with self.assertRaises(hevy.NotFound):
            hevy.exercise_progress({"template_id": ["NOPE"]})
        with self.assertRaises(ValueError):
            hevy.exercise_progress({})

    def test_endpoint_and_mcp_tool(self):
        from garmin_ai import auth
        folder = Path(self.temp.name)
        for name, value in {"HOSTED": True, "PUBLIC_URL": "https://garmin.example.com",
                            "APP_PASSWORD_HASH": auth.hash_password("pw"), "AUTH_DB_PATH": str(folder / "auth.db")}.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(web_dashboard, "start_auto_sync", return_value=None))
        client = self.stack.enter_context(TestClient(create_app(), base_url="https://garmin.example.com"))
        self.assertEqual(client.get("/api/strength/exercise?template_id=79D0BB3A").status_code, 401)
        bearer = {"Authorization": "Bearer t"}
        with patch.object(auth, "bearer_valid", return_value=True):
            response = client.get("/api/strength/exercise?template_id=79D0BB3A", headers=bearer)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(response.json()["sessions"]), 2)
            missing = client.get("/api/strength/exercise?template_id=NOPE", headers=bearer)
            self.assertEqual((missing.status_code, missing.json()), (404, {"error": "Unknown exercise template."}))
            self.assertEqual(client.get("/api/strength/exercise", headers=bearer).status_code, 400)
        self.assertEqual(json.loads(mcp_server.exercise_progress("PLANK"))["template"]["title"], "Plank")
        self.assertEqual(mcp_server.exercise_progress("NOPE"), "Unknown exercise template.")


if __name__ == "__main__":
    unittest.main()
