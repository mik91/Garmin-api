import sqlite3
import tempfile
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from garmin_ai import db, insights_engine, sync


class FakeGarmin:
    def get_stats(self, _day):
        return {
            "totalSteps": 10000,
            "restingHeartRate": 48,
            "averageStressLevel": 24,
            "bodyBatteryHighestValue": 90,
            "bodyBatteryLowestValue": 20,
            "totalKilocalories": 2300,
            "moderateIntensityMinutes": 20,
            "vigorousIntensityMinutes": 10,
            "avgWakingRespirationValue": 13.2,
            "averageSpo2": 97.0,
        }

    def get_morning_training_readiness(self, _day):
        return {"score": 72, "level": "HIGH", "recoveryTime": 120}

    def get_training_status(self, _day):
        return {
            "mostRecentTrainingStatus": {
                "latestTrainingStatusData": {
                    "device": {
                        "primaryTrainingDevice": True,
                        "trainingStatusFeedbackPhrase": "PRODUCTIVE_3",
                    }
                }
            },
            "mostRecentVO2Max": {
                "generic": {"vo2MaxPreciseValue": 57.2}
            },
        }


class SyncMappingTests(unittest.TestCase):
    def test_daily_recovery_mapping_and_units(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)

        sync._sync_daily_stats(
            FakeGarmin(),
            conn,
            date(2026, 8, 30),
            {"avgOvernightHrv": 56},
        )
        row = conn.execute("SELECT * FROM daily_stats").fetchone()

        self.assertEqual(row["hrv_overnight"], 56)
        self.assertEqual(row["training_readiness"], 72)
        self.assertEqual(row["training_status"], "Productive")
        self.assertEqual(row["vo2_max"], 57.2)
        self.assertEqual(row["recovery_time_hours"], 2.0)
        self.assertEqual(row["moderate_intensity_min"], 20)
        self.assertEqual(row["vigorous_intensity_min"], 10)
        self.assertEqual(row["avg_respiration"], 13.2)
        self.assertEqual(row["avg_spo2"], 97.0)

    def test_fully_recovered_readiness_stores_zero_recovery_time(self):
        client = FakeGarmin()
        client.get_morning_training_readiness = lambda _day: {
            "score": 80,
            "level": "HIGH",
            "recoveryTime": 90,
            "recoveryTimeChangePhrase": "REACHED_ZERO",
        }
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)

        sync._sync_daily_stats(client, conn, date(2026, 8, 30), {})
        row = conn.execute("SELECT * FROM daily_stats").fetchone()

        self.assertEqual(row["recovery_time_hours"], 0)

    def test_concurrent_auto_sync_reuses_completed_result(self):
        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "garmin.db")
            login_calls = []
            results = []

            def fake_login():
                login_calls.append(1)
                time.sleep(0.2)
                return object()

            with (
                patch.object(sync.config, "DB_PATH", database_path),
                patch.object(db, "DB_PATH", database_path),
                patch.object(sync, "login", fake_login),
                patch.object(sync, "_sync_activities"),
                patch.object(sync, "_sync_sleep", return_value=None),
                patch.object(sync, "_sync_daily_stats"),
                patch.object(sync, "_sync_body_composition"),
            ):
                threads = [
                    threading.Thread(
                        target=lambda: results.append(sync.sync(3, reuse_today=True))
                    )
                    for _ in range(2)
                ]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join()

            self.assertEqual(len(results), 2)
            self.assertEqual(len(login_calls), 1)

    def test_failed_sync_keeps_days_already_fetched(self):
        def stats(day):
            if day == "2026-08-31":
                raise ConnectionError("Garmin went away")
            return {"totalSteps": 5000}

        with tempfile.TemporaryDirectory() as directory:
            database_path = str(Path(directory) / "garmin.db")
            with (
                patch.object(db, "DB_PATH", database_path),
                patch.object(sync, "login", return_value=FakeGarmin()),
                patch.object(sync, "_sync_activities"),
                patch.object(sync, "_sync_sleep", return_value=None),
                patch.object(sync, "_sync_body_composition"),
                patch.object(FakeGarmin, "get_stats", side_effect=stats, create=True),
                patch.object(sync.time, "sleep"),
            ):
                with self.assertRaises(ConnectionError):
                    sync._sync_garmin(3, date(2026, 8, 30), date(2026, 9, 1))
                with db.get_connection() as conn:
                    days = [row[0] for row in conn.execute("SELECT date FROM daily_stats")]

        self.assertEqual(days, ["2026-08-30"])

    def test_rejected_sign_in_is_not_retried(self):
        calls = []

        def rejected():
            calls.append(1)
            raise sync.GarminConnectAuthenticationError("401")

        with patch.object(sync.time, "sleep") as sleep, self.assertRaises(sync.GarminConnectAuthenticationError):
            sync._with_retry(rejected)
        self.assertEqual(len(calls), 1)
        sleep.assert_not_called()


class InsightTests(unittest.TestCase):
    def test_sport_volume_uses_time_and_sport_sample_confidence(self):
        insights = []
        current = {
            "Swimming": {
                "count": 3,
                "weekly_duration_hours": 0.40,
                "weekly_distance_km": 0.61,
                "pace_min_per_km": None,
                "avg_hr": 110,
            }
        }
        previous = {
            "Swimming": {
                "count": 4,
                "weekly_duration_hours": 0.62,
                "weekly_distance_km": 0.57,
                "pace_min_per_km": None,
                "avg_hr": 104,
            }
        }

        insights_engine._add_sport_insights(insights, current, previous, {})
        insights_engine._enrich_insight(
            insights[0], {"daily_days": 30, "sleep_nights": 30, "activity_count": 20}
        )

        insight = insights[0].as_dict()
        self.assertEqual(insight["metric"], "weekly_duration_hours")
        self.assertEqual(insight["value"], 0.40)
        self.assertEqual(insight["baseline"], 0.62)
        self.assertEqual(insight["sample_size"], 3)
        self.assertEqual(insight["confidence"], "low")


if __name__ == "__main__":
    unittest.main()
