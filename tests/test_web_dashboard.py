import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from starlette.testclient import TestClient

from garmin_ai import config, db, web_dashboard
from garmin_ai.web_server import create_app


class WebDashboardTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "history.db")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(db.SCHEMA)

    def tearDown(self):
        self.temp.cleanup()

    def test_empty_dashboard_is_valid_json(self):
        result = web_dashboard.payload({"start": ["2026-09-01"], "end": ["2026-09-09"]}, self.path)
        self.assertEqual(result["activities"], [])
        self.assertEqual(result["calendar"], [])
        json.dumps(result, allow_nan=False)

    def test_profile_races_and_season_are_sent(self):
        races = [{"name": "Tri", "date": "2099-09-12", "priority": "B"},
                 {"name": "City 10K", "date": "2099-05-29", "priority": "a", "distance": "10K", "goal": "sub 38:30"}]
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO coach_profile(section, content, updated_at) VALUES('races', ?, '2026-11-12')",
                         (json.dumps(races),))
        result = web_dashboard.payload({"start": ["2026-09-01"], "end": ["2026-09-09"]}, self.path)
        self.assertEqual([r["name"] for r in result["races"]], ["City 10K", "Tri"])
        self.assertEqual(result["season"]["next_a_race"]["name"], "City 10K")
        self.assertEqual(result["season"]["phase"], "base")

    def test_no_races_send_an_empty_list(self):
        result = web_dashboard.payload({"start": ["2026-09-01"], "end": ["2026-09-09"]}, self.path)
        self.assertEqual(result["races"], [])
        self.assertIsNone(result["season"])

    def test_date_filter_does_not_hide_calendar_history_or_expose_raw(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO activities(activity_id, start_time, type, raw_json) VALUES(1, '2026-08-01 09:00', 'running', ?)",
                         (json.dumps({"activityName": "Easy run", "privateDeviceField": "hidden"}),))
        result = web_dashboard.payload({"start": ["2026-09-01"], "end": ["2026-09-09"]}, self.path)
        self.assertEqual(result["activities"], [])
        self.assertEqual(result["calendar_activities"][0]["name"], "Easy run")
        self.assertNotIn("privateDeviceField", json.dumps(result))

    def test_context_and_adherence_persist_in_existing_database(self):
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.execute("INSERT INTO scheduled_workouts(scheduled_workout_id,date,name) VALUES('test','2026-09-09','Run')")
        with patch.object(db, "DB_PATH", self.path):
            web_dashboard.save_context({"primary_goal": "Half marathon", "target_event": "Five weeks"})
            web_dashboard.save_adherence({"scheduled_workout_id": "test", "status": "skipped"})
            with closing(sqlite3.connect(self.path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT value FROM goals WHERE key='primary_goal'").fetchone()[0], "Half marathon")
                self.assertEqual(conn.execute("SELECT status FROM adherence_overrides").fetchone()[0], "skipped")
            web_dashboard.save_adherence({"scheduled_workout_id": "test", "status": "automatic"})
            with closing(sqlite3.connect(self.path)) as conn, conn:
                self.assertEqual(conn.execute("SELECT COUNT(*) FROM adherence_overrides").fetchone()[0], 0)

    def test_http_actions_require_local_headers_and_reject_cross_origin(self):
        client = TestClient(create_app(), base_url="http://127.0.0.1:8502")
        with patch.object(web_dashboard, "save_context", return_value={"message": "saved"}) as action,                 patch.object(config, "WEB_PORT", 8502):
            for origin, custom, expected in [("http://evil.example", True, 403), (None, False, 403),
                                              ("http://127.0.0.1:8502", True, 200)]:
                headers = {"Content-Type": "application/json"}
                if origin:
                    headers["Origin"] = origin
                if custom:
                    headers["X-Garmin-Request"] = "local-web"
                self.assertEqual(client.post("/api/goals", content="{}", headers=headers).status_code, expected)
            action.assert_called_once()
            self.assertEqual(client.get("/", headers={"Host": "evil.example:8502"}).status_code, 403)

    def test_static_assets_revalidate_by_etag(self):
        client = TestClient(create_app(), base_url="http://127.0.0.1:8502")
        with patch.object(config, "WEB_PORT", 8502):
            first = client.get("/plotly.js")
            self.assertEqual(first.status_code, 200)
            self.assertEqual(first.headers["cache-control"], "no-cache")
            self.assertEqual(first.headers["content-encoding"], "gzip")
            again = client.get("/plotly.js", headers={"If-None-Match": first.headers["etag"]})
            self.assertEqual(again.status_code, 304)
            self.assertEqual(again.content, b"")

if __name__ == "__main__":
    unittest.main()
