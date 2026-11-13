import copy
import unittest

from garmin_ai import workouts
from garmin_ai.workouts import WorkoutError, build_workout


def steps_of(workout):
    return workout["workoutSegments"][0]["workoutSteps"]


def garmin_copy(workout):
    """What Garmin returns: the same workout with ids, nulls and extra fields added."""
    stored = copy.deepcopy(workout)
    stored.update(workoutId=123, ownerId=9, author={"displayName": "x"}, estimatedDurationInSecs=None)
    counter = iter(range(1000, 2000))

    def fill(steps):
        for step in steps:
            step["stepId"] = next(counter)
            step.setdefault("targetType", None)
            step.setdefault("targetValueOne", None)
            step.setdefault("zoneNumber", None)
            step.setdefault("strokeType", {"strokeTypeId": 0, "strokeTypeKey": None, "displayOrder": 0})
            step.setdefault("equipmentType", {"equipmentTypeId": 0, "equipmentTypeKey": None, "displayOrder": 0})
            if step.get("targetValueOne") is not None:
                step["targetValueOne"] = round(step["targetValueOne"], 7)
            fill(step.get("workoutSteps") or [])
    fill(steps_of(stored))
    return stored


class RunTests(unittest.TestCase):
    def test_intervals_with_pace_targets(self):
        workout = build_workout("running", "Test 10min easy + 4x1min", [
            {"type": "warmup", "duration": {"time_s": 600}, "target": {"hr_zone": 2}},
            {"type": "repeat", "repeat": {"count": 4, "steps": [
                {"type": "interval", "duration": {"time_s": 60}, "target": {"pace_min_per_km": ["4:00", "3:50"]}},
                {"type": "recovery", "duration": {"time_s": 60}}]}},
            {"type": "cooldown", "duration": {"distance_m": 1000}},
        ])
        self.assertEqual(workout["workoutName"], "[TP] Test 10min easy + 4x1min")
        self.assertEqual(workout["sportType"], {"sportTypeId": 1, "sportTypeKey": "running", "displayOrder": 1})
        warmup, repeat, cooldown = steps_of(workout)
        self.assertEqual((warmup["targetType"]["workoutTargetTypeKey"], warmup["zoneNumber"]), ("heart.rate.zone", 2))
        self.assertEqual(repeat["type"], "RepeatGroupDTO")
        self.assertEqual((repeat["numberOfIterations"], repeat["endConditionValue"]), (4, 4.0))
        self.assertEqual(repeat["endCondition"]["conditionTypeKey"], "iterations")
        interval, recovery = repeat["workoutSteps"]
        # Fastest end first, as Garmin stores it: 3:50/km = 4.3478 m/s, 4:00/km = 4.1667 m/s.
        self.assertEqual(interval["targetType"]["workoutTargetTypeKey"], "pace.zone")
        self.assertAlmostEqual(interval["targetValueOne"], 1000 / 230, places=5)
        self.assertAlmostEqual(interval["targetValueTwo"], 1000 / 240, places=5)
        self.assertEqual([s["stepOrder"] for s in (warmup, repeat, interval, recovery, cooldown)], [1, 2, 3, 4, 5])
        self.assertEqual((interval["childStepId"], recovery["childStepId"], warmup["childStepId"]), (1, 1, None))
        self.assertEqual(recovery["targetType"]["workoutTargetTypeKey"], "no.target")
        self.assertEqual(cooldown["endCondition"]["conditionTypeKey"], "distance")
        self.assertEqual(cooldown["preferredEndConditionUnit"]["unitKey"], "kilometer")
        self.assertIn("3:50-4:00/km", workouts.summary(workout))

    def test_matches_runna_pace_format(self):
        # Runna's "400m at 3:30/km (3:20-3:40/km)" is stored as 5.0 and 4.5454545 m/s.
        workout = build_workout("running", "x", [
            {"type": "interval", "duration": {"distance_m": 400}, "target": {"pace_min_per_km": ["3:20", "3:40"]}}])
        step = steps_of(workout)[0]
        self.assertAlmostEqual(step["targetValueOne"], 5.0)
        self.assertAlmostEqual(step["targetValueTwo"], 4.5454545, places=6)

    def test_easy_run_on_hr_zone_and_numeric_pace(self):
        workout = build_workout("run", "[TP] Easy", [
            {"type": "interval", "duration": {"time_s": 2700}, "target": {"hr_zone": 2}, "notes": "conversational"}])
        self.assertEqual(workout["workoutName"], "[TP] Easy")  # no double prefix
        legacy = build_workout("run", "[CP] Easy", [{"type": "interval", "duration": {"time_s": 600}}])
        self.assertEqual(legacy["workoutName"], "[TP] Easy")  # old prefix renamed, not stacked
        step = steps_of(workout)[0]
        self.assertEqual((step["zoneNumber"], step["description"]), (2, "conversational"))
        numeric = build_workout("running", "x", [
            {"type": "interval", "duration": {"time_s": 60}, "target": {"pace_min_per_km": [4.5, 5]}}])
        self.assertAlmostEqual(steps_of(numeric)[0]["targetValueOne"], 1000 / 270, places=5)


class SwimTests(unittest.TestCase):
    def build(self, **kwargs):
        return build_workout("lap_swimming", "Swim test", [
            {"type": "warmup", "duration": {"distance_m": 200}, "swim": {"stroke": "free"}},
            {"type": "repeat", "repeat": {"count": 4, "skip_last_rest": True, "steps": [
                {"type": "interval", "duration": {"distance_m": 50},
                 "swim": {"stroke": "freestyle", "drill": "kick", "equipment": "kickboard"}},
                {"type": "rest", "duration": {"time_s": 20}}]}},
            {"type": "rest", "duration": {"lap_button": True}},
            {"type": "cooldown", "duration": {"distance_m": 100}, "swim": {"stroke": "any"}},
        ], **kwargs)

    def test_swim_with_repeats_and_rest(self):
        workout = self.build()
        self.assertEqual(workout["sportType"]["sportTypeKey"], "swimming")
        self.assertEqual((workout["poolLength"], workout["poolLengthUnit"]["unitKey"]), (25.0, "meter"))
        warmup, repeat, lap_rest, cooldown = steps_of(workout)
        self.assertEqual(warmup["strokeType"], {"strokeTypeId": 6, "strokeTypeKey": "free", "displayOrder": 6})
        self.assertEqual(warmup["preferredEndConditionUnit"]["unitKey"], "meter")
        self.assertTrue(repeat["skipLastRestStep"])
        swim, rest = repeat["workoutSteps"]
        self.assertEqual(swim["stepType"]["stepTypeKey"], "main")  # Garmin's own swim step type
        self.assertEqual(swim["drillType"]["drillTypeKey"], "kick")
        self.assertEqual(swim["equipmentType"]["equipmentTypeId"], 2)
        self.assertEqual((rest["endCondition"]["conditionTypeKey"], rest["endConditionValue"]), ("fixed.rest", 20.0))
        self.assertNotIn("strokeType", rest)
        self.assertEqual(lap_rest["endCondition"]["conditionTypeKey"], "lap.button")
        self.assertEqual(cooldown["strokeType"]["strokeTypeKey"], "any_stroke")
        text = workouts.summary(workout)
        self.assertIn("4x, skip last rest", text)
        self.assertIn("Total: about 0.5 km", text)

    def test_pool_length_and_whole_lengths(self):
        self.assertEqual(self.build(pool_length_m=50)["poolLength"], 50.0)
        with self.assertRaisesRegex(WorkoutError, "whole number of 25 m lengths"):
            build_workout("lap_swimming", "x", [{"type": "interval", "duration": {"distance_m": 60}}])
        with self.assertRaisesRegex(WorkoutError, "not distance"):
            build_workout("lap_swimming", "x", [{"type": "rest", "duration": {"distance_m": 50}}])
        with self.assertRaisesRegex(WorkoutError, "Unknown stroke"):
            build_workout("lap_swimming", "x", [{"type": "interval", "duration": {"distance_m": 50},
                                                 "swim": {"stroke": "doggy"}}])


class BikeAndStrengthTests(unittest.TestCase):
    def test_trainer_ride_with_power_and_hr(self):
        workout = build_workout("cycling", "Trainer", [
            {"type": "warmup", "duration": {"time_s": 600}, "target": {"power_w": [150, 120]}},
            {"type": "interval", "duration": {"time_s": 1200}, "target": {"hr_bpm": [150, 160]}},
            {"type": "cooldown", "duration": {"time_s": 300}, "target": {"hr_zone": 1}},
        ])
        warmup, interval, cooldown = steps_of(workout)
        self.assertEqual(workout["sportType"]["sportTypeId"], 2)
        self.assertEqual((warmup["targetType"]["workoutTargetTypeKey"], warmup["targetValueOne"],
                          warmup["targetValueTwo"]), ("power.zone", 120.0, 150.0))
        self.assertEqual((interval["targetType"]["workoutTargetTypeKey"], interval["targetValueOne"]),
                         ("heart.rate.zone", 150.0))
        self.assertIsNone(interval.get("zoneNumber"))
        self.assertIn("Total: about 35:00", workouts.summary(workout))
        with self.assertRaisesRegex(WorkoutError, "Pace targets are for running"):
            build_workout("cycling", "x", [{"type": "interval", "duration": {"time_s": 60},
                                            "target": {"pace_min_per_km": ["4:00", "4:10"]}}])

    def test_strength_block(self):
        workout = build_workout("strength", "Gym", [
            {"type": "warmup", "duration": {"time_s": 300}, "notes": "Row easy"},
            {"type": "repeat", "repeat": {"count": 3, "steps": [
                {"type": "interval", "duration": {"reps": 8}, "notes": "Back squat @ 80 kg"},
                {"type": "rest", "duration": {"time_s": 120}}]}},
            {"type": "interval", "duration": {"lap_button": True}, "notes": "Core circuit"},
        ])
        self.assertEqual(workout["sportType"], {"sportTypeId": 5, "sportTypeKey": "strength_training", "displayOrder": 3})
        squat, rest = steps_of(workout)[1]["workoutSteps"]
        self.assertEqual((squat["endCondition"]["conditionTypeKey"], squat["endConditionValue"]), ("reps", 8.0))
        self.assertEqual(rest["endCondition"]["conditionTypeKey"], "time")  # fixed.rest is swim-only
        self.assertIn("Interval 8 reps - Back squat @ 80 kg", workouts.summary(workout))
        with self.assertRaisesRegex(WorkoutError, "not distance"):
            build_workout("strength", "x", [{"type": "interval", "duration": {"distance_m": 100}}])


class ValidationTests(unittest.TestCase):
    def test_clear_errors(self):
        cases = [
            (("tennis", "x", [{}]), "Unknown sport"),
            (("running", "", [{"type": "interval", "duration": {"time_s": 60}}]), "needs a name"),
            (("running", "x", []), "non-empty"),
            (("running", "x", [{"type": "sprint", "duration": {"time_s": 60}}]), "Unknown step type"),
            (("running", "x", [{"type": "interval", "duration": {"time_s": 60, "distance_m": 5}}]), "exactly one"),
            (("running", "x", [{"type": "interval", "duration": {"reps": 5}}]), "only for strength"),
            (("running", "x", [{"type": "interval", "duration": {"time_s": 60},
                                "target": {"pace_min_per_km": ["4:75", "5:00"]}}]), "min:sec"),
            (("running", "x", [{"type": "interval", "duration": {"time_s": 60}, "target": {"hr_zone": 6}}]), "outside"),
            (("running", "x", [{"type": "rest", "duration": {"time_s": 60}, "target": {"hr_zone": 1}}]), "Rest steps"),
            (("running", "x", [{"type": "repeat", "repeat": {"count": 2, "steps": [
                {"type": "repeat", "repeat": {"count": 2, "steps": [{"type": "interval", "duration": {"time_s": 5}}]}}]}}]),
             "nested"),
            (("running", "x", [{"type": "interval", "duration": {"time_s": 60}}], None, 25), "only for lap_swimming"),
        ]
        for args, message in cases:
            with self.subTest(message=message), self.assertRaisesRegex(WorkoutError, message):
                build_workout(*args)


class ReadBackTests(unittest.TestCase):
    def setUp(self):
        self.sent = build_workout("lap_swimming", "Swim", [
            {"type": "warmup", "duration": {"distance_m": 200}, "swim": {"stroke": "free"}},
            {"type": "repeat", "repeat": {"count": 4, "steps": [
                {"type": "interval", "duration": {"distance_m": 50}},
                {"type": "rest", "duration": {"time_s": 20}}]}}])

    def test_garmin_additions_are_not_mismatches(self):
        self.assertEqual(workouts.compare(self.sent, garmin_copy(self.sent)), [])
        run = build_workout("running", "x", [
            {"type": "interval", "duration": {"time_s": 60}, "target": {"pace_min_per_km": ["3:50", "4:00"]}}])
        self.assertEqual(workouts.compare(run, garmin_copy(run)), [])

    def test_changed_steps_are_reported(self):
        stored = garmin_copy(self.sent)
        steps_of(stored)[1]["numberOfIterations"] = 3
        steps_of(stored)[1]["workoutSteps"][1]["endConditionValue"] = 30.0
        stored["poolLength"] = 50.0
        problems = workouts.compare(self.sent, stored)
        self.assertTrue(any("count sent 4 got 3" in p for p in problems))
        self.assertTrue(any("value sent 20.0 got 30.0" in p for p in problems))
        self.assertTrue(any("pool length" in p for p in problems))
        del steps_of(stored)[0]
        self.assertTrue(any("step count" in p for p in workouts.compare(self.sent, stored)))

    def test_hash_is_stable(self):
        self.assertEqual(workouts.payload_hash(self.sent), workouts.payload_hash(copy.deepcopy(self.sent)))


class PaceAndPlanTests(unittest.TestCase):
    def test_vdot_50_matches_daniels_tables(self):
        result = workouts.training_paces(5000, 19 * 60 + 57)
        self.assertAlmostEqual(result["vdot"], 50.0, delta=0.2)
        paces = {name: value["seconds_per_km"] for name, value in result["paces"].items()}
        # Daniels VDOT 50: T 4:15, I 3:55, R ~3:41 per km, easy around 5:05-5:35.
        self.assertLessEqual(abs(sum(paces["threshold"]) / 2 - 255), 3)
        self.assertLessEqual(abs(sum(paces["interval"]) / 2 - 235), 3)
        self.assertLessEqual(abs(sum(paces["repetition"]) / 2 - 221), 4)
        self.assertTrue(295 <= paces["easy"][0] < paces["easy"][1] <= 345)

    def test_plan_warnings(self):
        paces = workouts.training_paces(5000, 1200)

        def run(name, minutes, pace=None, zone=None):
            target = {"pace_min_per_km": pace} if pace else {"hr_zone": zone} if zone else None
            return build_workout("running", name, [{"type": "interval", "duration": {"time_s": minutes * 60},
                                                    **({"target": target} if target else {})}])

        items = [
            {"date": "2026-11-16", "workout": run("Easy", 40, pace=["4:10", "4:20"])},
            {"date": "2026-11-17", "workout": run("Threshold", 30, zone=4)},
            {"date": "2026-11-19", "workout": run("Easy 2", 40, pace=["5:15", "5:35"])},
            {"date": "2026-11-22", "workout": run("Long", 130, pace=["5:15", "5:35"])},
        ]
        warnings = workouts.pace_warnings(items, paces)
        self.assertEqual(len(warnings), 1)
        self.assertIn("2026-11-16 [TP] Easy", warnings[0])
        self.assertIn("faster than marathon", warnings[0])
        self.assertEqual(workouts.pace_warnings(items[2:3], paces), [])
        self.assertEqual(workouts.pace_warnings(items, None), [])

        sessions = [workouts.to_session(i["date"], i["workout"], paces) for i in items]
        easy, threshold, easy2, long = sessions
        # HR-zone steps have no pace: km comes from the middle of the easy range
        self.assertEqual((threshold["intensity"], threshold["hard_minutes"], threshold["km"] > 0),
                         ("quality", 30, True))
        self.assertNotIn("estimate_partial", threshold)
        self.assertEqual(easy["intensity"], "quality")  # 4:10-4:20 is near threshold for a 20:00 5K
        self.assertTrue(easy2["pace_target"])
        self.assertEqual((long["kind"], long["minutes"]), ("long", 130))
        self.assertAlmostEqual(long["km"], 130 * 60 / 325, delta=0.2)
        race = workouts.to_session("2026-11-22", run("5K Race", 20, zone=5), paces)
        self.assertEqual((race["intensity"], race["all_out"]), ("race", True))
        tempo = workouts.to_session("2026-11-22", run("Race pace 4x1k", 20, zone=4), paces,
                                    {"slot": "midday"})
        self.assertNotIn("all_out", tempo)
        self.assertEqual(tempo["slot"], "midday")

        swim = build_workout("lap_swimming", "Swim B Kick test 650", [
            {"type": "warmup", "duration": {"distance_m": 200}},
            {"type": "repeat", "repeat": {"count": 6, "steps": [
                {"type": "interval", "duration": {"distance_m": 50}, "swim": {"drill": "kick"}},
                {"type": "rest", "duration": {"time_s": 20}}]}},
            {"type": "cooldown", "duration": {"distance_m": 150}}])
        swim_session = workouts.to_session("2026-11-19", swim, paces)
        self.assertEqual(swim_session["minutes"], 15)  # 650 m at 2:00/100 m + 6 x 20 s rest
        self.assertEqual(swim_session["intensity"], "easy")  # a drill "test" is not a race
        self.assertNotIn("all_out", swim_session)
        self.assertEqual(workouts.to_session("2026-11-19", swim, paces, swim_s_per_100m=150)["minutes"], 18)
        gym = build_workout("strength", "Triathlon gym 40min", [
            {"type": "interval", "duration": {"reps": 10}, "notes": "Squat"},
            {"type": "rest", "duration": {"time_s": 90}}])
        self.assertEqual(workouts.to_session("2026-11-18", gym, paces)["minutes"], 40)

        unnamed = [workouts.to_session("2026-11-16", run("Easy", 40, zone=2), paces),
                   workouts.to_session("2026-11-21", run("Sunday", 90, zone=2), paces)]
        workouts.mark_long_runs(unnamed)
        self.assertEqual([s.get("kind") for s in unnamed], [None, "long"])


if __name__ == "__main__":
    unittest.main()
