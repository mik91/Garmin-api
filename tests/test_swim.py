import json
import sqlite3
import unittest
from contextlib import closing
from datetime import date, timedelta

from garmin_ai import db, mcp_server, workouts
from garmin_ai.coaching import load_rules, season, swim, validate_week
from test_coach_review import Base

RACE = {"name": "City 10K", "date": "2027-05-29", "priority": "A", "distance": "10K", "goal": "10K under 38:30"}
ANCHOR = {"start_week": "2026-11-16", "start_run_km": 32.8, "start_long_run_km": 9.4, "peak_run_km": 56,
          "start_vdot": 50.4, "start_weekly_swim_m": 2600, "start_continuous_swim_m": 400}


class CssTests(unittest.TestCase):
    def test_css_and_zones(self):
        self.assertEqual(swim.css_from_test("7:40", "3:35"), 122.5)
        self.assertEqual(swim.css_from_test(460, 215), 122.5)
        z = swim.zones(122.5)
        self.assertEqual(z["css"]["per_100m"], ["2:02", "2:06"])
        self.assertLess(z["fast"]["seconds"][0], z["css"]["seconds"][0])
        self.assertLess(z["css"]["seconds"][0], z["endurance"]["seconds"][0])
        with self.assertRaises(swim.history.HistoryError):
            swim.css_from_test("3:35", "7:40")  # times swapped


class SeasonSwimTests(unittest.TestCase):
    def setUp(self):
        self.weeks = season.generate(ANCHOR, RACE, load_rules(), css=122.5)

    def test_volume_and_continuous_progress_within_caps(self):
        first = self.weeks[0]["swim"]
        self.assertEqual((first["continuous_m"], first["target_weekly_m"]), (400, 2600))
        for w in self.weeks:
            s = w["swim"]
            self.assertEqual(s["sessions"], 3)
            self.assertLessEqual(s["weekly_m"], s["target_weekly_m"] + 350, w["week_start"])
            self.assertLessEqual(s["target_weekly_m"], swim.DEFAULTS["weekly_cap_m"])
            self.assertLessEqual(s["continuous_m"], swim.DEFAULTS["continuous_cap_m"])
        normal = [w["swim"]["continuous_m"] for w in self.weeks if not w["light"] and w["phase"] == "base"]
        self.assertEqual(normal, sorted(normal))  # continuous distance only grows in normal base weeks
        self.assertEqual(max(w["swim"]["continuous_m"] for w in self.weeks), 2000)

    def test_sets_follow_the_phase(self):
        names = {w["phase"]: [x["name"] for x in w["swim"]["plan"]] for w in self.weeks
                 if not w["light"] and not w["swim"]["css_test"]}
        self.assertEqual(names["base"], ["Technique", "Kick + aerobic", "Continuous"])
        self.assertEqual(names["build"], ["CSS set", "Pull endurance", "Continuous"])
        self.assertEqual(names["peak"], ["CSS set", "Pull + sighting", "Continuous"])
        self.assertIn("Sharpen", names["race_week"])
        css_sets = [x["session"] for w in self.weeks for x in w["swim"]["plan"] if x["name"] == "CSS set"]
        self.assertIn("~2:08/100 m", css_sets[0])  # CSS+5 s from a 2:02.5 CSS
        self.assertNotEqual(css_sets[0], css_sets[-1])  # the CSS set progresses

    def test_test_weeks_have_one_hard_swim(self):
        tests = [w for w in self.weeks if w["swim"]["css_test"]]
        self.assertGreaterEqual(len(tests), 3)
        for w in tests:
            hard = [x for x in w["swim"]["plan"] if x.get("hard")]
            self.assertEqual([x["name"] for x in hard], ["CSS test"], w["week_start"])

    def test_no_css_means_effort_words(self):
        plan = season.generate(ANCHOR, RACE, load_rules())
        sets = [x["session"] for w in plan for x in w["swim"]["plan"] if x["name"] == "CSS set"]
        self.assertTrue(sets and all("CSS" in s and "/100 m)" not in s for s in sets))


class HardSwimTests(unittest.TestCase):
    def test_hard_swim_flag_and_spacing_warning(self):
        def swim_workout(name):
            return workouts.build_workout("lap_swimming", name, [{"type": "interval", "duration": {"distance_m": 400}}])
        css = workouts.to_session("2026-11-19", swim_workout("Swim CSS Test"))
        kick = workouts.to_session("2026-11-19", swim_workout("Swim B Kick test 650"))
        self.assertTrue(css.get("hard"))
        self.assertNotIn("hard", kick)  # a kick "test" is drills, not hard
        plan = {"week_start": "2026-11-16", "sessions": [
            {**css, "date": "2026-11-18"},
            {"date": "2026-11-17", "sport": "run", "minutes": 45, "km": 9, "intensity": "quality"}]}
        history = {"weekly_run_km": [30], "longest_run_km_30d": 10, "weeks_since_light": 1}
        warnings = [w for w in validate_week(plan, history)["warnings"] if w["rule"] == "hard_days"]
        self.assertEqual(len(warnings), 1)
        plan["sessions"][0]["date"] = "2026-11-17"  # same day as the run quality: one hard day
        self.assertFalse([w for w in validate_week(plan, history)["warnings"] if w["rule"] == "hard_days"])


class SwimToolTests(Base):
    def setUp(self):
        super().setUp()
        with db.get_connection() as conn:
            for i, (meters, seconds, swolf) in enumerate([(1500, 2700, 66), (1000, 1750, 64), (1600, 2720, 60),
                                                         (1200, 2000, 58)]):
                day = date(2026, 11, 13) - timedelta(days=7 * (3 - i))
                conn.execute(
                    "INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, "
                    "moving_duration_s, raw_json) VALUES(?, ?, 'lap_swimming', ?, ?, ?, ?)",
                    (900 + i, f"{day} 12:00", meters, seconds, seconds, json.dumps({"averageSwolf": swolf})))

    def conn(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def test_own_pace_volume_and_trend(self):
        with closing(self.conn()) as conn:
            self.assertEqual(swim.recent_pace(conn, "2026-11-16"), round(9170 / 5300 * 100))
            self.assertEqual(swim.weekly_volume(conn, "2026-11-16"), round(5300 / 4))
            result = swim.trend(conn, 4, today=date(2026, 11, 15))
        self.assertEqual([w["swims"] for w in result["weeks"]], [1, 1, 1, 1])
        self.assertEqual(result["weeks"][-1]["swolf"], 58)
        self.assertEqual(result["summary"]["reading"], "more efficient (lower SWOLF)")

    def test_record_css_keeps_history_and_feeds_the_plan(self):
        first = json.loads(mcp_server.record_css_test("8:00", "3:50", "2026-11-19"))
        self.assertEqual(first["css"], "2:05")
        second = json.loads(mcp_server.record_css_test("7:40", "3:35", "2027-01-04", "felt strong"))
        self.assertEqual((second["css"], second["previous"]["css"]), ("2:02", "2:05"))
        paces = json.loads(mcp_server.swim_paces())
        self.assertEqual((paces["css"], len(paces["tests"])), ("2:02", 2))
        logged = json.loads(mcp_server.coaching_history(kind="profile"))
        self.assertIn("(was 2:05)", logged[0]["summary"])
        self.assertTrue(mcp_server.record_css_test("3:35", "7:40").startswith("Error"))
        mcp_server.update_athlete_profile("races", json.dumps([RACE]))
        mcp_server.update_athlete_profile("season_plan", json.dumps(ANCHOR))
        plan = json.loads(mcp_server.season_plan())
        sets = [x["session"] for w in plan["weeks"] for x in w["swim"]["plan"] if x["name"] == "CSS set"]
        self.assertIn("/100 m)", sets[0])

    def test_swim_trend_tool_and_validation_pace(self):
        self.assertIn("weeks", json.loads(mcp_server.swim_trend(4)))
        from garmin_ai import coach
        from test_coach_review import week
        [checked] = coach.upload_plan(week("2026-11-16"), dry_run=True)["validation"]
        self.assertTrue(checked["history_used"]["swim_pace_used"].endswith("/100 m"))


if __name__ == "__main__":
    unittest.main()
