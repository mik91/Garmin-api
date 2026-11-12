import json
import random
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from garmin_ai import config, db, sync
from garmin_ai.coaching import efficiency


def lap(speed, hr, seconds=None, meters=1000):
    seconds = seconds or meters / speed
    return {"distance": meters, "movingDuration": seconds, "duration": seconds, "averageHR": hr,
            "avgGradeAdjustedSpeed": speed, "averageSpeed": speed}


class Fixture(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        self.stack = ExitStack()
        for target in (config, db):
            self.stack.enter_context(patch.object(target, "DB_PATH", self.path))
        with db.get_connection():
            pass
        self.next_id = 1

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def conn(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def add_run(self, day, laps, dew_f=None, label="AEROBIC_BASE", name="Easy Run", splits=True):
        with db.get_connection() as conn:
            conn.execute(
                "INSERT INTO activities(activity_id, start_time, type, distance_m, duration_s, splits_json, "
                "weather_json, raw_json) VALUES(?, ?, 'running', ?, ?, ?, ?, ?)",
                (self.next_id, f"{day} 07:00", sum(l["distance"] for l in laps),
                 sum(l["duration"] for l in laps), json.dumps({"lapDTOs": laps}) if splits else None,
                 json.dumps({"dewPoint": dew_f}) if dew_f is not None else None,
                 json.dumps({"trainingEffectLabel": label, "activityName": name})))
        self.next_id += 1


class SyncDetailsTests(Fixture):
    def test_backfills_newest_first_and_retries_failures(self):
        for i in range(3):
            self.add_run(f"2026-11-0{i + 1}", [lap(3, 140)], splits=False)

        class Client:
            calls = []

            def get_activity_splits(self, activity_id):
                self.calls.append(("splits", activity_id))
                if activity_id == 2:
                    raise RuntimeError("Garmin hiccup")
                return {"lapDTOs": [lap(3, 140)]}

            def get_activity_weather(self, activity_id):
                self.calls.append(("weather", activity_id))
                return None if activity_id == 1 else {"dewPoint": 50}  # treadmill: no weather

        client = Client()
        with db.get_connection() as conn:
            self.assertEqual(sync._sync_activity_details(client, conn, limit=2, pause=0), 2)
            conn.commit()
        self.assertEqual([c[1] for c in client.calls], [3, 3, 2, 2])  # newest first, limit 2
        with closing(self.conn()) as conn:
            rows = {r["activity_id"]: (r["splits_json"], r["weather_json"])
                    for r in conn.execute("SELECT activity_id, splits_json, weather_json FROM activities")}
        self.assertIsNotNone(rows[3][0])
        self.assertIsNone(rows[2][0])  # failed: retried by a later sync
        self.assertIsNotNone(rows[2][1])
        with db.get_connection() as conn:
            sync._sync_activity_details(client, conn, limit=5, pause=0)
            conn.commit()
            stored = conn.execute("SELECT weather_json FROM activities WHERE activity_id = 1").fetchone()[0]
        self.assertEqual(stored, "{}")  # Garmin had none: not asked again
        client.calls.clear()
        with db.get_connection() as conn:
            sync._sync_activity_details(client, conn, limit=5, pause=0)
        self.assertEqual(client.calls, [("splits", 2)])  # only the failed one is retried


class EfficiencyTests(Fixture):
    LEVEL_BEFORE, LEVEL_AFTER, SLOPE, HEAT = 60.0, 66.0, 25.0, 0.5

    def build_season(self, noise=1.5):
        rng = random.Random(4)
        start = date(2026, 6, 1)
        for week in range(18):
            level = self.LEVEL_BEFORE if week < 12 else self.LEVEL_AFTER  # fitness drops in week 12
            for d in (0, 2, 4):
                day = start + timedelta(days=7 * week + d)
                dew_c = 20 if week < 10 else 5  # hot summer, cool autumn
                laps = [lap(3.4, 120)]  # warm-up: ignored
                for k in range(6):
                    speed = 2.6 + 0.12 * k + rng.uniform(-0.05, 0.05)
                    hr = level + self.SLOPE * speed + self.HEAT * max(0, dew_c - 10) + rng.gauss(0, noise)
                    laps.append(lap(speed, hr))
                laps.append(lap(4.2, 170))  # a pickup lap: ignored
                self.add_run(day.isoformat(), laps, dew_f=dew_c * 9 / 5 + 32)
        return start + timedelta(days=7 * 18 - 1)

    def test_recovers_slope_heat_and_the_fitness_drop(self):
        today = self.build_season()
        with closing(self.conn()) as conn:
            result = efficiency.trend(conn, weeks=18, ref_hr=135, today=today)
        model = result["model"]
        self.assertAlmostEqual(model["bpm_per_m_s"], self.SLOPE, delta=1.5)
        self.assertAlmostEqual(model["bpm_per_c_dew_point"], self.HEAT, delta=0.1)
        expected_before = 1000 / ((135 - self.LEVEL_BEFORE) / self.SLOPE)
        expected_after = 1000 / ((135 - self.LEVEL_AFTER) / self.SLOPE)
        self.assertAlmostEqual(result["windows"][0]["seconds_per_km"], expected_before, delta=4)
        self.assertAlmostEqual(result["windows"][-1]["seconds_per_km"], expected_after, delta=4)
        # Hot summer weeks are not mistaken for lost fitness: before the drop all windows agree.
        early = [w["seconds_per_km"] for w in result["windows"][:4]]
        self.assertLess(max(early) - min(early), 6)
        self.assertTrue(result["summary"]["significant"])
        self.assertEqual(result["summary"]["reading"], "slower at the same HR")

    def test_noise_alone_is_not_significant(self):
        rng = random.Random(9)
        start = date(2026, 6, 1)
        for week in range(12):
            for d in (0, 3):
                laps = [lap(3.0, 120)] + [lap(2.7 + 0.1 * k, 60 + 25 * (2.7 + 0.1 * k) + rng.gauss(0, 6))
                                          for k in range(5)]
                self.add_run((start + timedelta(days=7 * week + d)).isoformat(), laps, dew_f=45)
        with closing(self.conn()) as conn:
            result = efficiency.trend(conn, weeks=12, ref_hr=135, today=start + timedelta(days=83))
        self.assertFalse(result["summary"]["significant"])

    def test_needs_enough_laps_and_skips_hard_runs(self):
        self.add_run("2026-11-02", [lap(3, 120)] + [lap(3, 140)] * 5)
        self.add_run("2026-11-03", [lap(3, 120)] + [lap(4.5, 175)] * 5, label="VO2MAX", name="Intervals")
        with closing(self.conn()) as conn:
            result = efficiency.trend(conn, weeks=6, today=date(2026, 11, 8))
            points = efficiency.laps(conn, "2026-11-01", "2026-11-08")
        self.assertFalse(result["available"])
        self.assertEqual(len(points), 4)  # laps starting in the first 10 min and the interval run left out


if __name__ == "__main__":
    unittest.main()
