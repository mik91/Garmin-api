import json
import unittest

from garmin_ai import coach, db, mcp_server, workouts
from garmin_ai.coaching import execution, load_rules, season
from test_coach_review import Base

# 4 x 5 min at 4:12-4:22/km (252-262 s/km) with 90 s jog; watch indexes: warmup 0, rep 1, jog 2,
# (repeat marker 3), cooldown 4.
STEPS = [{"type": "warmup", "duration": {"time_s": 600}, "target": {"hr_bpm": [105, 148]}},
         {"type": "repeat", "repeat": {"count": 4, "steps": [
             {"type": "interval", "duration": {"time_s": 300}, "target": {"pace_min_per_km": ["4:12", "4:22"]}},
             {"type": "recovery", "duration": {"time_s": 90}}]}},
         {"type": "cooldown", "duration": {"time_s": 600}}]
PAYLOAD = workouts.build_workout("running", "Threshold 4x5min", STEPS)


def lap(index, seconds, meters, hr=None):
    return {"wktStepIndex": index, "duration": seconds, "distance": meters, "averageHR": hr, "maxHR": hr}


def splits(rep_paces, hrs=None, cut=None):
    """Laps for the workout: warm-up split in two auto-laps, reps at the given s/km, jogs, cool-down."""
    laps = [lap(0, 300, 1000, 130), lap(0, 300, 1050, 140)]
    for i, pace in enumerate(rep_paces):
        seconds = 300 if cut is None or i < cut else 120
        laps.append(lap(1, seconds, seconds / pace * 1000, (hrs or [160] * len(rep_paces))[i]))
        laps.append(lap(2, 90, 200, 140))
    laps.append(lap(4, 600, 1700, 135))
    return json.dumps({"lapDTOs": laps})


class AnalyzeTests(unittest.TestCase):
    def test_watch_order_puts_repeat_children_before_the_marker(self):
        order = execution.fit_steps(PAYLOAD)
        kinds = [s and s["stepType"]["stepTypeKey"] for s in order]
        self.assertEqual(kinds, ["warmup", "interval", "recovery", None, "cooldown"])

    def test_consecutive_laps_are_one_step(self):
        groups = execution.instances(splits([255, 255]))
        self.assertEqual([g["step_index"] for g in groups], [0, 1, 2, 1, 2, 4])
        self.assertEqual((groups[0]["duration_s"], groups[0]["distance_m"], groups[0]["avg_hr"]), (600, 2050, 135))

    def test_on_target_session_advances(self):
        result = execution.analyze(PAYLOAD, splits([258, 256, 257, 255], hrs=[160, 163, 165, 167]))
        self.assertEqual((result["decision"], result["completion"], result["on_target_share"]), ("advance", 1.0, 1.0))
        self.assertEqual(result["hr_drift_bpm"], 4)  # from rep 2 (163) to rep 4 (167)
        self.assertEqual(result["reps"][0]["pace"], "4:18")

    def test_cut_short_repeats(self):
        result = execution.analyze(PAYLOAD, splits([256, 256]))
        self.assertEqual((result["decision"], result["reps_done"]), ("repeat", 2))
        self.assertIn("cut short", result["reasons"][0])

    def test_slow_reps_fade_and_drift_repeat(self):
        slow = execution.analyze(PAYLOAD, splits([270, 272, 268, 275]))
        self.assertEqual(slow["decision"], "repeat")
        self.assertTrue(any("slower than the range" in r for r in slow["reasons"]))
        self.assertGreaterEqual(slow["pace_bias_s_per_km"], 8)
        fade = execution.analyze(PAYLOAD, splits([252, 252, 262, 272]))
        self.assertTrue(any("faded" in r for r in fade["reasons"]), fade)
        drift = execution.analyze(PAYLOAD, splits([256, 256, 256, 256], hrs=[150, 160, 168, 175]))
        self.assertTrue(any("HR drifted +15" in r for r in drift["reasons"]), drift)

    def test_fast_reps_advance_but_note_the_bias(self):
        result = execution.analyze(PAYLOAD, splits([240, 242, 241, 243]))
        self.assertEqual(result["decision"], "advance")
        self.assertIn("faster than the range", result["pace_note"])

    def test_short_reps_have_no_drift_and_no_laps_is_reported(self):
        short = workouts.build_workout("running", "VO2 6x2min", [{"type": "repeat", "repeat": {"count": 3, "steps": [
            {"type": "interval", "duration": {"time_s": 120}, "target": {"pace_min_per_km": ["3:50", "4:00"]}},
            {"type": "recovery", "duration": {"time_s": 120}}]}}])
        laps = json.dumps({"lapDTOs": [lap(i % 2, 120, 120 / 235 * 1000 if i % 2 == 0 else 300, 150 + 10 * i)
                                       for i in range(6)]})
        result = execution.analyze(short, laps)
        self.assertIsNone(result["hr_drift_bpm"])
        self.assertEqual(result["decision"], "advance")
        self.assertEqual(execution.analyze(PAYLOAD, json.dumps({"lapDTOs": []}))["status"], "no_laps")

    def test_pace_bias_needs_two_sessions_in_a_row(self):
        found = [{"status": "analyzed", "pace_bias_s_per_km": 7}, {"status": "analyzed", "pace_bias_s_per_km": 9}]
        self.assertEqual(execution.pace_bias(found)["direction"], "slow")
        self.assertIsNone(execution.pace_bias(found[:1]))
        self.assertIsNone(execution.pace_bias([{"status": "analyzed", "pace_bias_s_per_km": 7},
                                               {"status": "analyzed", "pace_bias_s_per_km": -6}]))


class HoldTests(unittest.TestCase):
    ANCHOR = {"start_week": "2026-11-16", "start_run_km": 32.8, "start_long_run_km": 9.4, "peak_run_km": 56,
              "start_vdot": 50.4}
    RACE = {"name": "City 10K", "date": "2027-05-29", "priority": "A", "distance": "10K", "goal": "38:30"}

    def threshold(self, anchor):
        return [(w["week_start"], q) for w in season.generate(anchor, self.RACE, load_rules())
                for q in w["quality"] if q["type"] == "threshold"]

    def test_a_hold_repeats_the_next_session_of_that_type(self):
        plain = self.threshold(self.ANCHOR)
        held = self.threshold({**self.ANCHOR, "holds": [
            {"type": "threshold", "from_week": "2026-11-23", "date": "2026-11-17", "reason": "cut short"}]})
        self.assertEqual(plain[0], held[0])
        self.assertEqual(held[1][1]["step"], held[0][1]["step"])  # week 2 repeats week 1's step
        self.assertIn("Same step as 2026-11-17", held[1][1]["repeat"])
        self.assertEqual(held[2][1]["step"], plain[1][1]["step"])  # then it continues one step behind
        self.assertNotIn("repeat", held[2][1])


class ReviewToolTests(Base):
    def setUp(self):
        super().setUp()
        mcp_server.update_athlete_profile("races", json.dumps([HoldTests.RACE]))
        mcp_server.update_athlete_profile("season_plan", json.dumps(HoldTests.ANCHOR))
        coach.upload_plan([{"date": "2026-11-03", "workout": {"sport": "running", "name": "Threshold 4x5min",
                                                              "steps": STEPS}},
                           {"date": "2026-11-04", "workout": {"sport": "running", "name": "Threshold 3x8min",
                                                              "steps": STEPS}}], allow_errors=True)

    def run_on(self, day, rep_paces, activity_id):
        with db.get_connection() as conn:
            conn.execute("DELETE FROM activities WHERE substr(start_time, 1, 10) = ?", (day,))
            conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, raw_json, "
                         "splits_json) VALUES(?, ?, 'running', 9000, 3000, ?, ?)",
                         (activity_id, f"{day} 07:00", json.dumps({"activityName": "City - [TP] Threshold 4x5min"}),
                          splits(rep_paces)))

    def test_review_finds_sessions_and_holds_once(self):
        self.run_on("2026-11-03", [256, 256], 801)  # cut short; the 2026-11-04 one was not done
        result = json.loads(mcp_server.session_review("2026-11-01", "2026-11-08"))
        by_date = {s["date"]: s for s in result["sessions"]}
        self.assertEqual((by_date["2026-11-03"]["decision"], by_date["2026-11-03"]["activity_id"]), ("repeat", 801))
        self.assertEqual(by_date["2026-11-03"]["kind"], "threshold")
        self.assertEqual(by_date["2026-11-04"]["status"], "missed")
        applied = json.loads(mcp_server.session_review("2026-11-01", "2026-11-08", apply=True))
        self.assertEqual(len(applied["holds_added"]), 2)
        again = json.loads(mcp_server.session_review("2026-11-01", "2026-11-08", apply=True))
        self.assertEqual(again["holds_added"], [])  # one hold per session
        with db.get_connection() as conn:
            self.assertEqual(len(season.anchor(conn)["holds"]), 2)
        logged = json.loads(mcp_server.coaching_history(kind="profile"))
        self.assertIn("Repeat step", logged[0]["summary"])


if __name__ == "__main__":
    unittest.main()
