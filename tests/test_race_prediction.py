import sqlite3
import unittest
from datetime import date

import pandas as pd

from garmin_ai import db, race_prediction, sync


class RacePredictionTests(unittest.TestCase):
    def test_normalizes_garmin_response(self):
        row = race_prediction.normalize_garmin_prediction(
            {
                "calendarDate": "2026-09-06",
                "time5K": 1171,
                "time10K": 2483,
                "timeHalfMarathon": 5588,
                "timeMarathon": 12347,
            },
            "2026-09-06T12:00:00",
        )
        self.assertEqual(row["time_5k_s"], 1171)
        self.assertEqual(row["time_half_s"], 5588)
        self.assertEqual(race_prediction.format_seconds(row["time_half_s"]), "1:33:08")

    def test_only_explicit_race_efforts_are_credible(self):
        activities = pd.DataFrame([
            {
                "activity_id": 1,
                "start_time": pd.Timestamp("2026-05-23 16:00:00"),
                "type": "running",
                "category": "Running",
                "duration_s": 1180,
                "distance_m": 5087,
                "raw_json": '{"activityName":"Ottawa 5K Race","fastestSplit_5000":1164.5}',
            },
            {
                "activity_id": 2,
                "start_time": pd.Timestamp("2026-08-25 17:00:00"),
                "type": "running",
                "category": "Running",
                "duration_s": 3600,
                "distance_m": 11000,
                "raw_json": '{"activityName":"Tempo 7km","fastestSplit_10000":2943}',
            },
        ])
        efforts = race_prediction.credible_race_efforts(
            activities, date(2026, 9, 6)
        )
        self.assertEqual(len(efforts), 1)
        self.assertEqual(efforts[0]["activity"], "Ottawa 5K Race")
        self.assertEqual(efforts[0]["formatted"], "19:24")

    def test_half_marathon_subsplits_are_not_classified_as_separate_races(self):
        activities = pd.DataFrame([{
            "activity_id": 3,
            "start_time": pd.Timestamp("2026-08-30 08:00:00"),
            "type": "running",
            "category": "Running",
            "duration_s": 9026,
            "distance_m": 21120,
            "raw_json": (
                '{"activityName":"Brossard Half Marathon Race",'
                '"fastestSplit_5000":1761,"fastestSplit_10000":4084,'
                '"fastestSplit_21097":9026}'
            ),
        }])
        efforts = race_prediction.credible_race_efforts(
            activities, date(2026, 9, 6)
        )
        self.assertEqual([effort["distance"] for effort in efforts], ["Half marathon"])

    def test_garmin_remains_primary_and_local_model_is_cross_check(self):
        activities = pd.DataFrame([{
            "activity_id": 1,
            "start_time": pd.Timestamp("2026-05-23 16:00:00"),
            "type": "running",
            "category": "Running",
            "duration_s": 1180,
            "distance_m": 5087,
            "raw_json": '{"activityName":"Ottawa 5K Race","fastestSplit_5000":1164.5}',
        }])
        history = pd.DataFrame([{
            "date": pd.Timestamp("2026-09-06"),
            "time_5k_s": 1171,
            "time_10k_s": 2483,
            "time_half_s": 5588,
            "time_marathon_s": 12347,
        }])
        model = race_prediction.build_model(
            activities, history, date(2026, 9, 6)
        )
        half = model["predictions"]["Half marathon"]
        self.assertEqual(half["expected"], "1:33:08")
        self.assertEqual(half["official_garmin"], "1:33:08")
        self.assertEqual(half["local_riegel"], "1:29:17")
        self.assertEqual(half["confidence"], "high")

    def test_stronger_race_beats_more_recent_slow_race(self):
        efforts = [
            {
                "distance": "5K", "distance_m": 5000, "seconds": 1164.5,
                "formatted": "19:24", "date": "2026-05-23", "activity": "Fast race",
                "evidence": "verified race/time trial",
            },
            {
                "distance": "Half marathon", "distance_m": 21097.5, "seconds": 9026,
                "formatted": "2:30:26", "date": "2026-08-30", "activity": "Slow race",
                "evidence": "verified race/time trial",
            },
        ]
        prediction = race_prediction._best_local_prediction(
            efforts, 21097.5, date(2026, 9, 6)
        )
        self.assertEqual(prediction["source_effort"]["activity"], "Fast race")
        self.assertEqual(prediction["formatted"], "1:29:17")

    def test_sync_stores_daily_prediction_history(self):
        class Client:
            def get_race_predictions(self, start, end, aggregation):
                self.args = (start, end, aggregation)
                return [
                    {"calendarDate": "2026-09-05", "timeHalfMarathon": 5600},
                    {"calendarDate": "2026-09-06", "timeHalfMarathon": 5588},
                ]

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        client = Client()
        sync._sync_race_predictions(client, conn, date(2026, 9, 6))
        self.assertEqual(client.args, ("2025-09-06", "2026-09-06", "daily"))
        rows = conn.execute(
            "SELECT date, time_half_s FROM race_predictions ORDER BY date"
        ).fetchall()
        self.assertEqual([(row["date"], row["time_half_s"]) for row in rows], [
            ("2026-09-05", 5600),
            ("2026-09-06", 5588),
        ])


if __name__ == "__main__":
    unittest.main()
