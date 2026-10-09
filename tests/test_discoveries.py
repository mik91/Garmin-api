import json
import unittest

import pandas as pd

from garmin_ai.discoveries import compare_groups, recovery_observations, run_observations


class DiscoveriesTests(unittest.TestCase):
    def test_sleep_must_precede_run_and_only_one_run_per_day(self):
        activities = pd.DataFrame([
            dict(type="running", start_time=f"2026-09-0{day} {hour}:00", distance_m=5000,
                 moving_duration_s=0, duration_s=1800, avg_hr=140, elevation_gain_m=20)
            for day, hour in [(1, "08"), (1, "18"), (2, "08"), (3, "08")]
        ])
        sleep = pd.DataFrame([
            dict(date=f"2026-09-0{day}", total_sleep_s=8*3600, raw_json=json.dumps({"dailySleepDTO": {
                "sleepEndTimestampLocal": pd.Timestamp(f"2026-09-0{day} {hour}:00").value // 10**6
            }})) for day, hour in [(1, "07"), (2, "09")]
        ])
        result = run_observations(activities, sleep, "2026-09-01", "2026-09-03")
        self.assertEqual(len(result), 3)
        self.assertEqual(result.sleep_hours.notna().sum(), 1)
        self.assertEqual(result.iloc[0].sleep_hours, 8)
        self.assertEqual(result.iloc[0].pace_min_km, 6)

    def test_recovery_uses_calendar_dates_and_excludes_partial_loads(self):
        activity = pd.DataFrame([
            {"start_time": f"2026-09-0{day} 12:00", "raw_json": json.dumps({"activityTrainingLoad": load})}
            for day, load in [(1, 10), (2, 20), (3, 30), (3, None), (4, 100)]
        ])
        daily = pd.DataFrame({"date": ["2026-09-01", "2026-09-02", "2026-09-04", "2026-09-05"],
                              "hrv_overnight": [50, 55, 60, 40], "resting_hr": [50, 51, 49, 53]})
        result = recovery_observations(activity, daily, "2026-09-01", "2026-09-05")
        self.assertEqual(len(result), 3)
        self.assertEqual(result.iloc[0].hrv_change, 5)
        self.assertTrue(pd.isna(result.iloc[1].hrv_change))
        self.assertEqual(result.iloc[2].hrv_change, -20)
        self.assertEqual(result.iloc[2].group, "Higher load")
        bounded = recovery_observations(activity, daily, "2026-09-01", "2026-09-04")
        self.assertTrue((bounded.outcome_date <= pd.Timestamp("2026-09-04")).all())

    def test_comparison_counts_valid_values_and_suppresses_small_groups(self):
        frame = pd.DataFrame({"group": ["a"] * 5 + ["b"] * 5, "metric": [1]*5 + [3]*5})
        self.assertEqual(compare_groups(frame, "group", "metric", "a", "b")["difference"], -2)
        frame.loc[0, "metric"] = float("nan")
        self.assertIsNone(compare_groups(frame, "group", "metric", "a", "b")["difference"])

    def test_empty_history(self):
        empty = pd.DataFrame()
        self.assertTrue(run_observations(empty, empty, "2026-09-01", "2026-09-05").empty)
        self.assertTrue(recovery_observations(empty, empty, "2026-09-01", "2026-09-05").empty)


if __name__ == "__main__":
    unittest.main()
