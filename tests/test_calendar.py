import sqlite3
import unittest
from datetime import date

import pandas as pd

from garmin_ai import calendar, db, sync


def activities(*rows):
    frame = pd.DataFrame(rows)
    frame["start_time"] = pd.to_datetime(frame["start_time"])
    return frame


class CalendarParsingTests(unittest.TestCase):
    def test_finds_and_normalizes_nested_garmin_calendar_entry(self):
        payload = {
            "data": {
                "workoutScheduleSummariesScalar": [
                    {
                        "workoutScheduleId": 123,
                        "calendarDate": "2026-09-08",
                        "estimatedDurationInSecs": "2700",
                        "estimatedDistanceInMeters": 8000,
                        "workout": {
                            "workoutId": 456,
                            "workoutName": "Tempo run",
                            "sportType": {"sportTypeKey": "running"},
                        },
                    }
                ]
            }
        }

        found = calendar.scheduled_items(payload)
        self.assertEqual(len(found), 1)
        row = calendar.normalize_scheduled_workout(found[0], "2026-09-06T12:00:00")
        self.assertEqual(row["scheduled_workout_id"], "123")
        self.assertEqual(row["workout_id"], "456")
        self.assertEqual(row["name"], "Tempo run")
        self.assertEqual(row["sport"], "running")
        self.assertEqual(row["duration_s"], 2700)
        self.assertEqual(row["distance_m"], 8000)

    def test_sync_replaces_only_after_fetching_all_months(self):
        class Client:
            def __init__(self):
                self.calls = []

            def get_scheduled_workouts(self, year, month):
                self.calls.append((year, month))
                return [{
                    "workoutScheduleId": month,
                    "calendarDate": f"2026-{month:02d}-10",
                    "workout": {"workoutName": f"Month {month}"},
                }]

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        client = Client()
        sync._sync_scheduled_workouts(
            client, conn, date(2026, 1, 20), date(2026, 2, 20)
        )
        self.assertEqual(client.calls, [(2026, 1), (2026, 2)])
        rows = conn.execute("SELECT name FROM scheduled_workouts ORDER BY date").fetchall()
        # January 10 is correctly excluded from the requested partial-month window.
        self.assertEqual([row["name"] for row in rows], ["Month 2"])


class AdherenceTests(unittest.TestCase):
    def test_associated_activity_is_a_high_confidence_completion(self):
        scheduled = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "date": pd.Timestamp("2026-09-01"),
            "name": "Easy run",
            "sport": "running",
            "duration_s": 1800,
            "distance_m": 5000,
            "associated_activity_id": 99,
        }])
        actual = activities({
            "activity_id": 99,
            "start_time": "2026-09-01 08:00:00",
            "type": "running",
            "category": "Running",
            "duration_s": 1900,
            "distance_m": 5100,
            "raw_json": '{"activityName": "Morning Run"}',
        })

        result = calendar.build_adherence(scheduled, actual, today=date(2026, 9, 2))
        self.assertEqual(result.iloc[0]["status"], "completed")
        self.assertEqual(result.iloc[0]["confidence"], "high")
        self.assertEqual(result.iloc[0]["matched_activity_name"], "Morning Run")

    def test_garmin_association_marks_complete_when_activity_is_not_cached(self):
        scheduled = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "date": pd.Timestamp("2026-09-01"),
            "name": "Easy run",
            "sport": "running",
            "duration_s": 1800,
            "distance_m": 5000,
            "associated_activity_id": 99,
        }])

        result = calendar.build_adherence(
            scheduled, pd.DataFrame(), today=date(2026, 9, 2)
        )
        self.assertEqual(result.iloc[0]["status"], "completed")
        self.assertEqual(result.iloc[0]["matched_activity_id"], 99)
        self.assertEqual(result.iloc[0]["confidence"], "high")

    def test_local_match_uses_sport_duration_and_distance(self):
        scheduled = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "date": pd.Timestamp("2026-09-01"),
            "name": "Five kilometres",
            "sport": "running",
            "duration_s": 1800,
            "distance_m": 5000,
            "associated_activity_id": None,
        }])
        actual = activities(
            {
                "activity_id": 1,
                "start_time": "2026-09-01 08:00:00",
                "type": "cycling",
                "category": "Cycling",
                "duration_s": 1800,
                "distance_m": 5000,
                "raw_json": "{}",
            },
            {
                "activity_id": 2,
                "start_time": "2026-09-01 17:00:00",
                "type": "running",
                "category": "Running",
                "duration_s": 1850,
                "distance_m": 5050,
                "raw_json": "{}",
            },
        )

        result = calendar.build_adherence(scheduled, actual, today=date(2026, 9, 2))
        self.assertEqual(result.iloc[0]["matched_activity_id"], 2)
        self.assertEqual(result.iloc[0]["status"], "completed")

    def test_manual_skipped_override_wins_over_automatic_match(self):
        scheduled = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "date": pd.Timestamp("2026-09-01"),
            "name": "Run",
            "sport": "running",
            "duration_s": None,
            "distance_m": None,
            "associated_activity_id": None,
        }])
        actual = activities({
            "activity_id": 1,
            "start_time": "2026-09-01 08:00:00",
            "type": "running",
            "category": "Running",
            "duration_s": 1800,
            "distance_m": 5000,
            "raw_json": "{}",
        })
        overrides = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "status": "skipped",
            "matched_activity_id": None,
        }])

        result = calendar.build_adherence(
            scheduled, actual, overrides, today=date(2026, 9, 2)
        )
        self.assertEqual(result.iloc[0]["status"], "skipped")
        self.assertTrue(result.iloc[0]["manual_override"])
        self.assertTrue(pd.isna(result.iloc[0]["matched_activity_id"]))

    def test_unknown_plan_without_targets_does_not_guess(self):
        scheduled = pd.DataFrame([{
            "scheduled_workout_id": "one",
            "date": pd.Timestamp("2026-09-01"),
            "name": "Unspecified workout",
            "sport": None,
            "duration_s": None,
            "distance_m": None,
            "associated_activity_id": None,
        }])
        actual = activities({
            "activity_id": 1,
            "start_time": "2026-09-01 08:00:00",
            "type": "strength_training",
            "category": "Other",
            "duration_s": 1800,
            "distance_m": 0,
            "raw_json": "{}",
        })

        result = calendar.build_adherence(scheduled, actual, today=date(2026, 9, 2))
        self.assertEqual(result.iloc[0]["status"], "missed")
        self.assertTrue(pd.isna(result.iloc[0]["matched_activity_id"]))


if __name__ == "__main__":
    unittest.main()
