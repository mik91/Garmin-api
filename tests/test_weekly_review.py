import json
from contextlib import closing
import unittest
from datetime import datetime, time
from unittest.mock import patch

from garmin_ai import coach, config, db, mcp_server, web_dashboard
from garmin_ai.coaching import history, weekly
from test_coach_review import Base
from test_progress import ANCHOR, RACE


class ScheduleTests(unittest.TestCase):
    def test_parse(self):
        schedule = weekly.Schedule.parse("Sunday 19:30")
        self.assertEqual((schedule.weekday, schedule.at), (6, time(19, 30)))
        self.assertEqual(schedule.describe(), "Sun 19:30")
        self.assertIsNone(weekly.Schedule.parse("off"))
        self.assertIsNone(weekly.Schedule.parse(""))
        for bad in ("sun", "sunday 25:00", "someday 10:00"):
            with self.assertRaises(ValueError):
                weekly.Schedule.parse(bad)

    def test_last_run_before(self):
        schedule = weekly.Schedule.parse("sun 19:00")
        self.assertEqual(schedule.last_run_before(datetime(2026, 11, 15, 19, 0)), datetime(2026, 11, 15, 19, 0))
        self.assertEqual(schedule.last_run_before(datetime(2026, 11, 15, 18, 59)), datetime(2026, 11, 8, 19, 0))
        self.assertEqual(schedule.last_run_before(datetime(2026, 11, 18, 9, 0)), datetime(2026, 11, 15, 19, 0))


class WeeklyReviewTests(Base):
    def setUp(self):
        super().setUp()
        mcp_server.update_athlete_profile("races", json.dumps([RACE]))
        mcp_server.update_athlete_profile("season_plan", json.dumps({**ANCHOR, "start_week": "2026-10-19"}))
        self.stack.enter_context(patch.object(config, "GARMIN_WEEKLY_REVIEW", "sun 19:00"))
        self.stack.enter_context(patch.object(config, "GARMIN_TIMEZONE", ""))

    def reviews(self):
        with closing(self.conn()) as conn:
            return history.entries(conn, kind="review")

    def test_payload(self):
        with db.get_connection() as conn:
            entry = weekly.run(conn, "2026-11-11")  # any day picks its Monday
        self.assertEqual(entry["week_start"], "2026-11-09")
        self.assertEqual(entry["kind"], "review")
        self.assertTrue(entry["summary"].startswith("Weekly review (auto) 2026-11-09:"))
        self.assertIn("40 km run in 5 runs", entry["actual"])
        details = entry["details"]
        self.assertTrue(details["auto"])
        self.assertNotIn("log", details["week"])
        card = details["card"]
        self.assertIn(card["verdict"], ("on track", "watch", "act"))
        levels = [f["level"] for f in card["flags"]]
        self.assertEqual(levels, sorted(levels, key=weekly.LEVEL_ORDER.get))
        self.assertEqual((card["done"]["runs"], card["done"]["run_km"]), (5, 40.0))
        self.assertEqual(card["readiness"].keys(), {"start", "end"})
        nxt = card["next_week"]
        self.assertEqual(nxt["week_start"], "2026-11-16")
        self.assertTrue(nxt["run_km"] > 0 and nxt["long_run_km"] > 0 and nxt["quality"] and nxt["swims"])
        self.assertIn("Next week", entry["planned"])
        [logged] = json.loads(mcp_server.coaching_history(kind="review"))
        self.assertEqual(set(logged["details"]), {"auto", "generated_at", "card"})  # small for the coach

    def test_rerun_replaces_only_the_automatic_entry(self):
        with db.get_connection() as conn:
            history.add_entry(conn, "review", "Coach's own review", week_start="2026-11-09")
            first = weekly.run(conn, "2026-11-09")
            second = weekly.run(conn, "2026-11-09")
            weekly.run(conn, "2026-11-02")
        reviews = self.reviews()
        self.assertEqual(len(reviews), 4 - 1)
        self.assertNotIn(first["id"], [r["id"] for r in reviews])
        self.assertIn(second["id"], [r["id"] for r in reviews])
        self.assertIn("Coach's own review", [r["summary"] for r in reviews])
        with closing(self.conn()) as conn:
            self.assertEqual(weekly.latest(conn)["week_start"], "2026-11-09")
            self.assertEqual(weekly.latest(conn, "2026-11-05")["week_start"], "2026-11-02")
            self.assertIsNone(weekly.latest(conn, "2026-10-26"))

    def test_job_reviews_the_week_of_the_last_scheduled_time_once(self):
        before = web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 15, 18, 0), sync=False)
        self.assertEqual(before["week_start"], "2026-11-02")  # Sunday 19:00 not reached: last week's
        entry = web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 15, 19, 5), sync=False)
        self.assertEqual(entry["week_start"], "2026-11-09")
        self.assertIsNone(web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 15, 22, 0), sync=False))
        self.assertIsNone(web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 17, 8, 0), sync=False))
        self.assertEqual([r["week_start"] for r in self.reviews()], ["2026-11-09", "2026-11-02"])
        with patch.object(config, "GARMIN_WEEKLY_REVIEW", "off"):
            self.assertIsNone(web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 22, 20, 0), sync=False))

    def test_job_syncs_first(self):
        calls = []
        with patch.object(web_dashboard, "_can_sync", return_value=True), \
                patch.object(web_dashboard, "_sync_and_wait", side_effect=lambda: calls.append(1) or "ok"):
            web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 15, 19, 5))
            web_dashboard.run_weekly_review_if_due(datetime(2026, 11, 15, 20, 5))
        self.assertEqual(calls, [1])  # not again once the week is reviewed

    def test_sync_and_wait_waits_for_the_new_sync(self):
        def fake_start(days):
            with web_dashboard.sync_lock:
                web_dashboard.sync_status.update(running=False, message="Sync complete.",
                                                 finished=web_dashboard.sync_status["finished"] + 1)
        with patch.object(web_dashboard, "start_sync", side_effect=fake_start):
            self.assertEqual(web_dashboard._sync_and_wait(timeout_s=1, poll_s=0), "Sync complete.")

    def test_api_payloads(self):
        self.assertIsNone(web_dashboard.review_payload({})["review"])
        self.assertIsNone(web_dashboard.weekly_card())
        with db.get_connection() as conn:
            weekly.run(conn, "2026-11-09")
        result = web_dashboard.review_payload({})
        self.assertEqual((result["review"]["week_start"], result["schedule"]), ("2026-11-09", "Sun 19:00"))
        self.assertIn("progress", result["review"]["details"])
        self.assertIsNone(web_dashboard.review_payload({"week": ["2026-11-02"]})["review"])
        with self.assertRaises(ValueError):
            web_dashboard.review_payload({"week": ["soon"]})
        card = web_dashboard.payload({"start": ["2026-11-01"], "end": ["2026-11-15"]})["weekly_review"]
        self.assertEqual(card["week_start"], "2026-11-09")
        self.assertIn("next_week", card)
        self.assertEqual(coach.plan_payload({})["weekly_review"]["week_start"], "2026-11-09")


if __name__ == "__main__":
    unittest.main()
