import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from garmin_ai import coach, config, db, mcp_server, workouts
from garmin_ai.coaching import review
from test_coach import FakeGarmin

PACES = workouts.training_paces(5000, 1200)  # 20:00 5K


def run(name, minutes, zone=2):
    return {"sport": "running", "name": name,
            "steps": [{"type": "interval", "duration": {"time_s": minutes * 60}, "target": {"hr_zone": zone}}]}


def week(monday, long_minutes=52):
    day = lambda i: (date.fromisoformat(monday) + timedelta(days=i)).isoformat()
    return [{"date": day(0), "workout": run("Easy Mon", 40)},
            {"date": day(1), "workout": run("Threshold", 40, zone=4)},
            {"date": day(3), "workout": run("Easy Thu", 40)},
            {"date": day(4), "workout": run("Easy Fri", 35)},
            {"date": day(5), "workout": run("Long run", long_minutes)}]


class Base(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        self.api = FakeGarmin()
        self.stack = ExitStack()
        for target in (config, db):
            self.stack.enter_context(patch.object(target, "DB_PATH", self.path))
        self.stack.enter_context(patch.object(coach, "client", lambda: self.api))
        self.stack.enter_context(patch.object(coach, "current_paces", lambda: PACES))
        with db.get_connection() as conn:
            # five weeks of ~40 km before 2026-11-16, 8 km runs at 5:30/km, HR 140
            for i in range(35):
                day = date(2026, 11, 15) - timedelta(days=i)
                if day.weekday() in (0, 1, 3, 4, 5):
                    conn.execute(
                        "INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, "
                        "moving_duration_s, avg_hr, raw_json) VALUES(?, ?, 'running', 8000, 2640, 2640, 140, ?)",
                        (i + 1, f"{day} 07:00", json.dumps({"trainingEffectLabel": "AEROBIC_BASE"})))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def conn(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection


class UploadValidationTests(Base):
    def test_dry_run_reports_validation_per_week(self):
        result = coach.upload_plan(week("2026-11-16"), dry_run=True)
        self.assertEqual(result["errors"], [])
        [checked] = result["validation"]
        self.assertEqual(checked["week_start"], "2026-11-16")
        self.assertEqual(checked["history_used"]["weekly_run_km"][-1], 40.0)
        self.assertEqual(checked["summary"]["quality_sessions"], 1)
        self.assertEqual([s.get("kind") for s in checked["sessions"]].count("long"), 1)

    def test_rule_errors_block_a_real_upload_unless_allowed(self):
        items = week("2026-11-16", long_minutes=150)  # long run far over 30 days' longest + 2 km
        dry = coach.upload_plan(items, dry_run=True)
        self.assertTrue(any("Long run" in e for e in dry["errors"]))
        with self.assertRaisesRegex(coach.CoachError, "nothing was uploaded"):
            coach.upload_plan(items)
        self.assertEqual(self.api.calls, [])
        text = mcp_server.upload_plan(items)
        self.assertTrue(text.startswith("Error: The plan breaks coaching rules"))
        allowed = coach.upload_plan(items, allow_errors=True)
        self.assertTrue(allowed["errors_allowed"])
        self.assertEqual({i["action"] for i in allowed["items"]}, {"create"})

    def test_session_hints_and_bad_hints(self):
        items = week("2026-11-16")
        items.append({"date": "2026-11-18", "session": {"slot": "midday"},
                      "workout": {"sport": "lap_swimming", "name": "Midday swim",
                                  "steps": [{"type": "interval", "duration": {"time_s": 1800}}]}})
        result = coach.upload_plan(items, dry_run=True)
        self.assertTrue(any("Midday swim" in e for e in result["errors"]))
        items[-1]["session"] = {"colour": "red"}
        with self.assertRaisesRegex(coach.CoachError, "session may only set"):
            coach.upload_plan(items, dry_run=True)

    def test_weeks_are_validated_together_with_already_scheduled_workouts(self):
        coach.upload_plan(week("2026-11-16"))
        extra = [{"date": "2026-11-18", "workout": run("Intervals", 40, zone=5)}]
        result = coach.upload_plan(extra, dry_run=True)
        [checked] = result["validation"]
        self.assertEqual(len(checked["sessions"]), 6)
        self.assertTrue(any("consecutive" in e or "quality" in e for e in result["errors"]), result["errors"])

    def test_second_week_uses_the_first_weeks_plan_as_history(self):
        items = week("2026-11-16") + week("2026-11-23")
        result = coach.upload_plan(items, dry_run=True)
        first, second = result["validation"]
        self.assertEqual(second["history_used"]["weekly_run_km"][-1], first["summary"]["planned_run_km"])
        self.assertEqual(second["history_used"]["last_quality_date"], "2026-11-17")

    def test_untargeted_runs_use_the_athletes_own_easy_pace(self):
        [checked] = coach.upload_plan(week("2026-11-16"), dry_run=True)["validation"]
        self.assertEqual(checked["history_used"]["easy_pace_used"], "5:30")  # fixture runs: 8 km in 44 min
        easy_mon = checked["sessions"][0]
        self.assertAlmostEqual(easy_mon["km"], 40 * 60 / 330, delta=0.1)

    def test_unfinished_week_counts_the_runs_still_planned(self):
        # Week of 2026-11-09 is in progress on Thursday 11-12 (already ran): Fri and Sat are still planned.
        with db.get_connection() as conn:
            conn.execute("DELETE FROM activities WHERE substr(start_time, 1, 10) > '2026-11-12'")
        coach.upload_plan([{"date": "2026-11-13", "workout": run("Easy Fri", 40)},
                           {"date": "2026-11-14", "workout": run("Long run", 70)},
                           {"date": "2026-11-11", "workout": run("Done Wed", 40)}], allow_errors=True)
        items = week("2026-11-16")
        with db.get_connection() as conn:
            built = [{"date": i["date"], "payload": workouts.build_workout(**i["workout"]), "session": {}}
                     for i in items]
            [checked] = coach.validate_plan(conn, built, PACES, today=date(2026, 11, 12))
        current = checked["history_used"]["current_week"]
        self.assertEqual((current["week_start"], current["planned_from"]), ("2026-11-09", "2026-11-13"))
        self.assertEqual(current["run_km_done"], 24.0)  # Mon, Tue, Thu at 8 km
        self.assertAlmostEqual(current["run_km_still_planned"], (40 + 70) * 60 / 330 / 1000 * 1000, delta=0.3)
        self.assertEqual(checked["history_used"]["weekly_run_km"][-1],
                         round(24.0 + current["run_km_still_planned"], 1))
        self.assertAlmostEqual(checked["history_used"]["longest_run_km_30d"], 70 * 60 / 330, delta=0.1)
        self.assertNotIn("ramp", {e["rule"] for e in checked["errors"]})
        with db.get_connection() as conn:  # the week after a finished week gets no current_week
            [later] = coach.validate_plan(conn, built, PACES, today=date(2026, 11, 16))
        self.assertNotIn("current_week", later["history_used"])


class ReviewTests(Base):
    def test_week_review_compares_plan_and_actual(self):
        coach.upload_plan(week("2026-11-09"), allow_errors=True)
        result = json.loads(mcp_server.week_review("2026-11-11"))
        self.assertEqual(result["week_start"], "2026-11-09")
        self.assertEqual(result["actual"]["runs"], 5)
        self.assertEqual(result["actual"]["run_km"], 40.0)
        self.assertEqual(result["planned"]["coach_quality_sessions"], 1)
        self.assertGreater(result["planned"]["coach_run_km_estimate"], 0)
        self.assertIn("40 km run in 5 runs", result["suggested_actual"])
        self.assertEqual(result["log"][0]["kind"], "upload")

    def test_easy_pace_trend(self):
        with closing(self.conn()) as conn, conn:
            result = review.easy_pace_trend(conn, 5, today=date(2026, 11, 15))
        self.assertEqual(result["summary"]["hr_band"], [136, 144])
        self.assertEqual(result["summary"]["reading"], "stable")
        self.assertEqual(result["weeks"][-1]["pace_min_per_km"], "5:30")
        with closing(self.conn()) as conn, conn:
            outside = review.easy_pace_trend(conn, 5, 120, 130, today=date(2026, 11, 15))
        self.assertEqual(outside["summary"]["runs_in_band"], 0)

    def test_readiness_calibration_shape(self):
        with closing(self.conn()) as conn, conn:
            result = review.readiness_calibration(conn, 4, today=date(2026, 11, 16))
        self.assertEqual(len(result["weeks"]), 4)
        self.assertEqual(result["weeks"][-1]["run_km"], 40.0)
        self.assertIn("hrv_drop_pct", result["thresholds"])

    def test_season_phases(self):
        races = review.parse_races(json.dumps([{"name": "A", "date": "2026-12-06", "priority": "A"},
                                               {"name": "B", "date": "2026-11-01", "priority": "B"}]))
        rules = {"phases": {"taper_weeks": 2, "peak_weeks": 4, "build_weeks": 10, "recovery_weeks_after_race": 1}}
        phase = lambda day: review.season_phase(races, day, rules)["phase"]
        self.assertEqual(phase("2026-11-03"), "recovery")  # 2 days after the B race
        self.assertEqual(phase("2026-11-10"), "peak")      # 4 weeks to go
        self.assertEqual(phase("2026-11-24"), "taper")
        self.assertEqual(phase("2026-12-03"), "race_week")
        self.assertEqual(phase("2026-06-01"), "base")
        self.assertEqual(phase("2027-01-01"), "base")      # no upcoming A race


class ServerTests(unittest.TestCase):
    def test_instructions_and_registration(self):
        local = mcp_server.build_server()
        self.assertIn("upload_plan(dry_run=true)", local.instructions)
        names = {t.name for t in asyncio.run(local.list_tools())}
        self.assertTrue({"easy_pace_trend", "readiness_calibration"} <= names)
        self.assertNotIn("week_review", names)
        with patch.object(config, "PUBLIC_URL", "https://example.test"):
            hosted = mcp_server.build_server(hosted=True)
        self.assertEqual(hosted.instructions, mcp_server.INSTRUCTIONS)
        self.assertIn("week_review", {t.name for t in asyncio.run(hosted.list_tools())})
        self.assertNotIn("tired", mcp_server.check_readiness.__doc__.split("Other feelings")[0])


if __name__ == "__main__":
    unittest.main()
