import json
import unittest
from datetime import date, timedelta

from garmin_ai import db, mcp_server, workouts
from garmin_ai.coaching import load_rules, progress, season
from test_coach_review import Base

RACE = {"name": "City 10K", "date": "2027-05-29", "priority": "A", "distance": "10K", "goal": "10K under 38:30"}
ANCHOR = {"start_week": "2026-11-16", "start_run_km": 32.8, "start_long_run_km": 9.4, "peak_run_km": 56,
          "start_vdot": 50.4, "start_weekly_swim_m": 2600, "start_continuous_swim_m": 400}


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.weeks = season.generate(ANCHOR, RACE, load_rules())
        self.checks = [w for w in self.weeks if w.get("checkpoint")]

    def test_three_checkpoints_on_lighter_weeks_spaced_apart(self):
        self.assertEqual([c["checkpoint"]["test"] for c in self.checks], ["5K", "10K", "5K"])
        self.assertTrue(all(c["light"] for c in self.checks))
        days = [date.fromisoformat(c["week_start"]) for c in self.checks] + [date(2027, 5, 24)]
        self.assertTrue(all((b - a).days >= 28 for a, b in zip(days, days[1:])))

    def test_expected_results_follow_the_line_to_the_goal(self):
        expected = [c["checkpoint"]["expected"]["vdot"] for c in self.checks]
        self.assertEqual(expected, sorted(expected))
        self.assertTrue(50.4 < expected[0] < expected[-1] < 54.3)
        first = self.checks[0]
        self.assertEqual(len(first["quality"]), 1)
        self.assertIn(f"On track if about {first['checkpoint']['expected']['time']}", first["quality"][0]["session"])
        self.assertIn("parkrun", first["quality"][0]["session"])
        self.assertNotIn("parkrun", self.checks[1]["quality"][0]["session"])  # no 10K parkrun

    def test_race_time_inverts_vdot(self):
        self.assertAlmostEqual(season.race_time(10000, 54.3), 2310, delta=2)
        self.assertAlmostEqual(workouts.vdot(5000, season.race_time(5000, 50.4)), 50.4, delta=0.05)

    def test_validator_reads_a_time_trial_as_all_out(self):
        tt = workouts.build_workout("running", "5K time trial", [{"type": "interval", "duration": {"distance_m": 5000}}])
        session = workouts.to_session("2027-01-16", tt)
        self.assertEqual((session["intensity"], session["all_out"]), ("race", True))


class ProgressTests(Base):
    def setUp(self):
        super().setUp()
        # A season that started four weeks before the fixture's last week of runs (40 km/week).
        mcp_server.update_athlete_profile("races", json.dumps([RACE]))
        mcp_server.update_athlete_profile("season_plan", json.dumps({**ANCHOR, "start_week": "2026-10-19"}))

    def test_progress_flags_and_verdict(self):
        with db.get_connection() as conn:
            result = progress.progress(conn, today=date(2026, 11, 18))
        self.assertIn(result["verdict"], ("on track", "watch", "act"))
        self.assertEqual(result["season"]["weeks_done"], 4)
        self.assertEqual([v["week_start"] for v in result["volume"]],
                         ["2026-10-19", "2026-10-26", "2026-11-02", "2026-11-09"])
        self.assertTrue(all(v["actual_km"] == 40.0 for v in result["volume"]))
        areas = {f["area"]: f["level"] for f in result["flags"]}
        self.assertIn("volume", areas)
        self.assertEqual(areas["swim"], "watch")  # no CSS test recorded
        self.assertTrue(all(c["status"] == "upcoming" for c in result["checkpoints"]))
        self.assertEqual(result["season"]["this_week_target"]["week_start"], "2026-11-16")

    def test_volume_well_below_target_is_an_act_flag(self):
        mcp_server.update_athlete_profile("season_plan", json.dumps(
            {**ANCHOR, "start_week": "2026-10-19", "start_run_km": 60, "peak_run_km": 70}))
        with db.get_connection() as conn:
            result = progress.progress(conn, today=date(2026, 11, 18))
        self.assertEqual(result["verdict"], "act")
        self.assertTrue(any(f["area"] == "volume" and f["level"] == "act" for f in result["flags"]))

    def test_checkpoint_result_from_the_fastest_matching_run(self):
        with db.get_connection() as conn:
            conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, raw_json) "
                         "VALUES(500, '2027-01-16 09:00', 'running', 5020, 1160, '{}')")
            week = {"week_start": "2027-01-11",
                    "checkpoint": {"test": "5K", "distance_m": 5000, "expected": {"vdot": 51.7, "time": "19:22"}}}
            result = progress.checkpoint_result(conn, week)
        self.assertEqual((result["date"], result["time"]), ("2027-01-16", "19:20"))
        self.assertGreater(result["vs_expected_vdot"], 0)

    def test_tool_needs_a_season(self):
        mcp_server.update_athlete_profile("season_plan", "")
        self.assertIn("error", json.loads(mcp_server.season_progress()))


if __name__ == "__main__":
    unittest.main()
