import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from datetime import date
from pathlib import Path
from unittest.mock import patch

from garmin_ai import config, db, load_summary, mcp_server

TODAY = date(2026, 11, 11)  # a Wednesday; the current week starts Monday 2026-11-09


def activity(conn, ident, start, kind, minutes, km, label=None, z4=0, z5=0, load=0):
    raw = {"trainingEffectLabel": label, "hrTimeInZone_4": z4, "hrTimeInZone_5": z5, "activityTrainingLoad": load}
    conn.execute("INSERT INTO activities(activity_id, start_time, type, duration_s, distance_m, raw_json) "
                 "VALUES(?, ?, ?, ?, ?, ?)", (ident, start, kind, minutes * 60, km * 1000, json.dumps(raw)))


class WeeklyLoadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        with closing(sqlite3.connect(self.path)) as conn, conn:
            conn.executescript(db.SCHEMA)
            # Week of 2026-11-02: easy run, long run, tempo, a swim and a Garmin strength session.
            activity(conn, 1, "2026-11-03 07:00", "running", 40, 8, "AEROBIC_BASE", load=60)
            activity(conn, 2, "2026-11-05 07:00", "running", 45, 9, "TEMPO", z4=600, z5=120, load=110)
            activity(conn, 3, "2026-11-08 08:00", "trail_running", 90, 16, "AEROBIC_BASE", load=150)
            activity(conn, 4, "2026-11-04 18:00", "lap_swimming", 45, 1.8, "VO2MAX")
            activity(conn, 5, "2026-11-06 18:00", "strength_training", 50, 0)
            # Week of 2026-10-26: one run and one indoor ride.
            activity(conn, 6, "2026-10-28 07:00", "running", 50, 10)
            activity(conn, 7, "2026-10-29 18:00", "indoor_cycling", 60, 30, "LACTATE_THRESHOLD")
            # Current week so far.
            activity(conn, 8, "2026-11-10 07:00", "running", 30, 6, "VO2MAX")
            # Outside the 12-week window.
            activity(conn, 9, "2026-08-01 07:00", "running", 30, 6)
            # Hevy: one linked to the Garmin strength activity (not double counted), one gym-only.
            conn.execute("INSERT INTO hevy_workouts(workout_id, start_time, end_time, start_local, garmin_activity_id) "
                         "VALUES('a', '2026-11-06T22:00:00Z', '2026-11-06T22:50:00Z', '2026-11-06T18:00:00', 5)")
            conn.execute("INSERT INTO hevy_workouts(workout_id, start_time, end_time, start_local) "
                         "VALUES('b', '2026-10-31T13:00:00Z', '2026-10-31T13:45:00Z', '2026-10-31T09:00:00')")

    def tearDown(self):
        self.temp.cleanup()

    def load(self, weeks=12):
        with closing(sqlite3.connect(self.path)) as conn:
            return load_summary.weekly_load(conn, weeks, TODAY)

    def test_week_totals(self):
        result = self.load()
        self.assertEqual(len(result["weeks"]), 12)
        self.assertEqual((result["weeks"][0]["week_start"], result["weeks"][-1]["week_start"]),
                         ("2026-08-17", "2026-11-02"))
        week = result["weeks"][-1]
        self.assertEqual(week["running"]["distance_km"], 33.0)  # trail running counts as running
        self.assertEqual(week["running"]["runs"], 3)
        self.assertEqual(week["running"]["longest_run_km"], 16.0)
        self.assertEqual(week["running"]["longest_run_share_pct"], 48)
        self.assertEqual(week["running"]["quality_sessions"], 1)
        self.assertEqual(week["running"]["change_vs_previous_week_pct"], 230)
        self.assertEqual((week["quality_sessions"], week["quality_by_label"]), (2, {"TEMPO": 1, "VO2MAX": 1}))
        self.assertEqual(week["hours_by_sport"], {"running": 2.92, "cycling": 0.0, "swimming": 0.75,
                                                  "strength": 0.83, "other": 0.0})
        self.assertEqual(week["sessions_by_sport"]["strength"], 1)  # the linked Hevy workout is not added
        self.assertEqual(week["distance_km_by_sport"]["swimming"], 1.8)
        self.assertEqual((week["hr_zone_4_5_minutes"], week["training_load"]), (12, 320))
        self.assertEqual(week["total_hours"], 4.5)

    def test_cycling_hevy_and_current_week(self):
        result = self.load()
        before = result["weeks"][-2]
        self.assertEqual(before["hours_by_sport"]["cycling"], 1.0)
        self.assertEqual(before["quality_by_label"], {"LACTATE_THRESHOLD": 1})
        self.assertEqual((before["sessions_by_sport"]["strength"], before["hours_by_sport"]["strength"]), (1, 0.75))
        current = result["current_week"]
        self.assertEqual((current["week_start"], current["days_so_far"]), ("2026-11-09", 3))
        self.assertEqual(current["running"]["distance_km"], 6.0)
        self.assertNotIn("change_vs_previous_week_pct", current["running"])

    def test_summary_and_window(self):
        summary = self.load()["summary"]
        self.assertEqual(summary["avg_running_km"], round(43 / 12, 1))
        self.assertEqual((summary["max_running_km"], summary["longest_run_km"]), (33.0, 16.0))
        self.assertEqual(summary["last_4_weeks_avg_running_km"], round(43 / 4, 1))
        self.assertIsNone(summary["running_km_change_vs_prior_4_weeks_pct"])  # prior 4 weeks had no running
        self.assertEqual(len(self.load(weeks=2)["weeks"]), 2)
        self.assertEqual(len(self.load(weeks=500)["weeks"]), 52)

    def test_tool_is_read_only_and_local(self):
        with patch.object(config, "DB_PATH", self.path), patch.object(load_summary, "date") as fake_date:
            fake_date.today.return_value = TODAY
            fake_date.fromisoformat = date.fromisoformat
            result = json.loads(mcp_server.weekly_load_summary(4))
        self.assertEqual(len(result["weeks"]), 4)
        self.assertIn("weekly_load_summary", [f.__name__ for f in mcp_server.TOOLS])


if __name__ == "__main__":
    unittest.main()
