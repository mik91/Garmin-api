import http.client
import json
from contextlib import closing
import sqlite3
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from garmin_ai import db, web_dashboard
from garmin_ai.web_server import Handler


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
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with patch.object(web_dashboard, "save_context", return_value={"message": "saved"}) as action:
                for origin, custom, expected in [("http://evil.example", True, 403), (None, False, 403),
                                                  ("http://127.0.0.1:8502", True, 200)]:
                    connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
                    headers = {"Host": "127.0.0.1:8502", "Content-Type": "application/json"}
                    if origin:
                        headers["Origin"] = origin
                    if custom:
                        headers["X-Garmin-Request"] = "local-web"
                    connection.request("POST", "/api/goals", "{}", headers)
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                    connection.close()
                action.assert_called_once()
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
