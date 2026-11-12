import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import MagicMock, patch

from garmin_personal_api import HttpError

from garmin_ai import config, db, mcp_server, personal_api


class McpServerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "history.db")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(db.SCHEMA)
            conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, raw_json) "
                         "VALUES(1, '2026-09-05 09:00', 'running', 10000, ?)",
                         (json.dumps({"activityName": "Tempo"}),))
            conn.execute("INSERT INTO daily_stats(date, resting_hr) VALUES('2026-09-05', 50)")
        self.patch = patch.object(config, "DB_PATH", self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        self.temp.cleanup()

    def test_list_activities_returns_trimmed_rows(self):
        rows = json.loads(mcp_server.list_activities("2026-09-01", "2026-09-09"))
        self.assertEqual(rows[0]["name"], "Tempo")
        self.assertNotIn("raw_json", rows[0])
        self.assertEqual(json.loads(mcp_server.list_activities("2026-09-01", "2026-09-09", "cycling")), [])

    def test_daily_metrics_and_summary_are_json(self):
        self.assertEqual(json.loads(mcp_server.daily_metrics("2026-09-05", "2026-09-05"))["daily_stats"][0]["resting_hr"], 50)
        json.loads(mcp_server.coaching_summary("2026-09-01", "2026-09-09"))
        json.loads(mcp_server.sync_status())

    def test_query_database_is_read_only(self):
        result = json.loads(mcp_server.query_database("SELECT COUNT(*) FROM activities"))
        self.assertEqual(result["rows"], [[1]])
        self.assertTrue(mcp_server.query_database("DELETE FROM activities").startswith("SQL error"))
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0], 1)


class LiveToolTests(unittest.TestCase):
    def test_live_tool_passes_dates_and_returns_json(self):
        client = MagicMock()
        client.get_daily_hrv_summaries.return_value = {"hrvSummaries": [{"lastNightAvg": 51}]}
        with patch.object(personal_api, "call", side_effect=lambda op: op(client)):
            result = json.loads(mcp_server.garmin_hrv("2026-10-01", "2026-10-07"))
        client.get_daily_hrv_summaries.assert_called_once_with("2026-10-01", "2026-10-07")
        self.assertEqual(result["hrvSummaries"][0]["lastNightAvg"], 51)

    def test_activities_are_compacted_without_losing_running_dynamics(self):
        raw = {"activityId": 1, "activityType": {"typeKey": "running"}, "averageSpeed": 2.359999895,
               "avgGradeAdjustedSpeed": 2.385, "avgStrideLength": 90.2, "avgVerticalOscillation": 8.8,
               "avgVerticalRatio": 9.770000457, "avgGroundContactTime": 299.899993, "hrTimeInZone_2": 260.0,
               "ownerFullName": "Name", "startLatitude": 45.5, "privacy": {"typeKey": "private"},
               "isFavorite": False, "pr": True, "maxHR": None,
               "splitSummaries": [{"splitType": "RWD_RUN", "distance": 5048.54, "maxElevationGain": 49.0}]}
        result = mcp_server.compact_activity(raw)
        self.assertEqual(result["type"], "running")
        self.assertEqual(result["pace_per_km"], "7:04")
        self.assertEqual(result["grade_adjusted_pace_per_km"], "6:59")
        self.assertEqual((result["avgStrideLength"], result["avgVerticalOscillation"], result["avgVerticalRatio"],
                          result["avgGroundContactTime"]), (90.2, 8.8, 9.77, 299.9))
        self.assertTrue(result["pr"])
        self.assertEqual(result["splitSummaries"], [{"splitType": "RWD_RUN", "distance": 5048.54}])
        for dropped in ("ownerFullName", "startLatitude", "privacy", "isFavorite", "maxHR"):
            self.assertNotIn(dropped, result)

    def test_activities_over_the_size_limit_drop_whole_older_activities(self):
        client = MagicMock()
        client.search_activities.return_value = [{"activityId": i, "activityName": "x" * 1000} for i in range(10)]
        with patch.object(personal_api, "call", side_effect=lambda op: op(client)), \
                patch.object(mcp_server, "MAX_LIVE_CHARS", 4000):
            result = json.loads(mcp_server.garmin_activities("2026-10-01", "2026-10-07"))
        self.assertEqual([a["activityId"] for a in result["activities"]], [0, 1, 2])
        self.assertEqual(result["omitted_older_activities"], 7)

    def test_live_tool_reports_missing_key_instead_of_failing(self):
        error = personal_api.PersonalApiUnavailable("No personal API key was provided.")
        with patch.object(personal_api, "call", side_effect=error):
            self.assertIn("No personal API key", mcp_server.garmin_sleep("2026-10-01"))

    def test_expired_token_is_refreshed_once(self):
        tokens = MagicMock()
        tokens.obtain_token.return_value.access_token = "token"
        calls = []

        def operation(client):
            calls.append(client)
            if len(calls) == 1:
                raise HttpError(status_code=401, method="GET", url="u", response_text="")
            return {"ok": True}

        with patch.object(personal_api, "resolve_api_key", return_value="key"), \
                patch.object(personal_api, "PersonalApiAuthenticator") as authenticator, \
                patch.object(personal_api, "GarminPersonalApiClient"):
            authenticator.return_value.__enter__.return_value = tokens
            self.assertEqual(personal_api.call(operation), {"ok": True})
        self.assertEqual([c.kwargs["force_refresh"] for c in tokens.obtain_token.call_args_list], [False, True])


if __name__ == "__main__":
    unittest.main()
