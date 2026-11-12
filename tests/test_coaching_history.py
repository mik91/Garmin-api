import asyncio
import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from garmin_ai import coach, config, db, mcp_server
from garmin_ai.coaching import history, review
from test_coach import FakeGarmin, week


class CoachingHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = str(Path(self.temp.name) / "garmin.db")
        self.stack = ExitStack()
        for target in (config, db):
            self.stack.enter_context(patch.object(target, "DB_PATH", self.path))
        with db.get_connection() as conn:
            for day, km, raw in (
                ("2026-11-03", 10.0, {}),                                          # week of 11-03
                ("2026-11-05", 6.0, {"trainingEffectLabel": "LACTATE_THRESHOLD"}),
                ("2026-11-10", 8.0, {}),                                           # week of 11-10
                ("2026-11-12", 4.0, {"eventType": {"typeKey": "race"}}),
            ):
                conn.execute("INSERT INTO activities(activity_id, start_time, type, distance_m, raw_json) "
                             "VALUES(?, ?, 'running', ?, ?)",
                             (int(day.replace("-", "")), f"{day} 08:00", km * 1000, json.dumps(raw)))
            for i in range(28):
                day = f"2026-11-{16 - i:02d}" if i < 16 else f"2026-10-{31 - (i - 16):02d}"
                conn.execute("INSERT INTO daily_stats(date, hrv_overnight, resting_hr) VALUES(?, 60, 50)", (day,))
                conn.execute("INSERT INTO sleep(date, total_sleep_s) VALUES(?, ?)", (day, 7.5 * 3600))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_log_and_read_back_newest_first(self):
        first = json.loads(mcp_server.log_coaching_decision(
            "Base week, easy only.", kind="plan", week_start="2026-11-18", readiness="green",
            planned="34 km, 1 quality"))
        self.assertEqual(first["week_start"], "2026-11-16")  # snapped to the Monday
        mcp_server.log_coaching_decision("Missed Thursday, sick.", kind="adjustment", week_start="2026-11-16")
        entries = json.loads(mcp_server.coaching_history())
        self.assertEqual([e["kind"] for e in entries], ["adjustment", "plan"])
        self.assertEqual(json.loads(mcp_server.coaching_history(kind="plan"))[0]["planned"], "34 km, 1 quality")
        self.assertEqual(json.loads(mcp_server.coaching_history(start="2026-11-23")), [])

    def test_bad_input_is_a_readable_error(self):
        self.assertTrue(mcp_server.log_coaching_decision("x", kind="bogus").startswith("Error: kind"))
        self.assertTrue(mcp_server.log_coaching_decision("", kind="note").startswith("Error: summary"))
        self.assertTrue(mcp_server.log_coaching_decision("x", readiness="purple").startswith("Error: readiness"))
        self.assertTrue(mcp_server.update_athlete_profile("rule_overrides", "not json").startswith("Error"))

    def test_profile_edits_keep_the_previous_text(self):
        mcp_server.update_athlete_profile("Cautions", "Left Achilles niggle above 45 km/week")
        mcp_server.update_athlete_profile("cautions", "Left Achilles niggle above 50 km/week", "Held up at 48 km")
        profile = json.loads(mcp_server.athlete_profile())
        self.assertIn("50 km", profile["sections"]["cautions"]["content"])
        self.assertNotIn("cautions", profile["missing"])
        self.assertNotIn("volume_history", profile["missing"])
        edits = json.loads(mcp_server.coaching_history(kind="profile"))
        self.assertEqual(edits[0]["summary"], "Held up at 48 km")
        self.assertIn("45 km", edits[0]["details"]["previous"])
        unchanged = json.loads(mcp_server.update_athlete_profile("cautions", profile["sections"]["cautions"]["content"]))
        self.assertFalse(unchanged["changed"])

    def test_races_are_json_and_set_the_season_phase(self):
        self.assertTrue(mcp_server.update_athlete_profile("races", "Valencia in December").startswith("Error: races"))
        races = [{"name": "Valencia Marathon", "date": "2027-02-01", "priority": "a"},
                 {"name": "Parkrun TT", "date": "2026-12-05", "priority": "C"}]
        json.loads(mcp_server.update_athlete_profile("races", json.dumps(races)))
        self.assertIn("phase", json.loads(mcp_server.athlete_profile())["season"])
        with db.get_connection() as conn:
            season = review.season(conn, "2026-11-12")
        self.assertEqual(season["next_a_race"]["priority"], "A")
        self.assertEqual(season["weeks_to_a_race"], 12)  # from Monday 2026-11-09
        self.assertEqual(season["phase"], "build")
        self.assertEqual(season["tune_up_races"][0]["name"], "Parkrun TT")

    def test_rule_overrides_change_the_validator(self):
        plan = {"week_start": "2026-11-16", "sessions": [
            {"date": "2026-11-16", "sport": "swim", "minutes": 30, "slot": "midday", "intensity": "easy"}]}
        self.assertIn("swim", {e["rule"] for e in json.loads(mcp_server.validate_week_plan(plan))["errors"]})
        mcp_server.update_athlete_profile("rule_overrides", '{"swim": {"max_midday_minutes": 35}}')
        self.assertNotIn("swim", {e["rule"] for e in json.loads(mcp_server.validate_week_plan(plan))["errors"]})

    def test_validator_history_comes_from_activities(self):
        plan = {"week_start": "2026-11-16", "sessions": [
            {"date": "2026-11-17", "sport": "run", "minutes": 40, "km": 6, "intensity": "quality"}]}
        result = json.loads(mcp_server.validate_week_plan(plan, {"weeks_since_light": 0}))
        used = result["history_used"]
        self.assertEqual(used["weekly_run_km"][-2:], [16.0, 12.0])
        self.assertEqual(used["longest_run_km_30d"], 10.0)
        self.assertEqual(used["last_quality_date"], "2026-11-05")
        self.assertEqual(used["last_all_out_date"], "2026-11-12")
        self.assertEqual(used["weeks_since_light"], 0)  # the override wins
        self.assertTrue(mcp_server.validate_week_plan({"sessions": []}).startswith("plan needs"))

    def test_readiness_reads_the_synced_days(self):
        result = json.loads(mcp_server.check_readiness("2026-11-16"))
        self.assertEqual(result["status"], "green")
        self.assertEqual(json.loads(mcp_server.check_readiness("2026-11-16", ["sick"]))["status"], "red")

    def test_real_uploads_are_logged_and_dry_runs_are_not(self):
        api = FakeGarmin()
        with patch.object(coach, "client", lambda: api):
            coach.upload_plan([{"date": "2026-11-18", "workout": week()}], dry_run=True)
            self.assertEqual(json.loads(mcp_server.coaching_history()), [])
            coach.upload_plan([{"date": "2026-11-18", "workout": week()}])
            coach.upload_plan([{"date": "2026-11-18", "workout": week()}])  # unchanged: no new entry
        entries = json.loads(mcp_server.coaching_history())
        self.assertEqual(len(entries), 1)
        self.assertEqual((entries[0]["kind"], entries[0]["week_start"]), ("upload", "2026-11-16"))
        self.assertIn("[CP] Easy", entries[0]["planned"])


class RegistrationTests(unittest.TestCase):
    def test_history_tools_are_hosted_only(self):
        names = lambda server: {t.name: t for t in asyncio.run(server.list_tools())}
        local = names(mcp_server.build_server())
        self.assertIn("check_readiness", local)
        self.assertIn("validate_week_plan", local)
        self.assertNotIn("log_coaching_decision", local)
        with patch.object(config, "PUBLIC_URL", "https://example.test"):
            hosted = names(mcp_server.build_server(hosted=True))
        for name in ("log_coaching_decision", "coaching_history", "athlete_profile", "update_athlete_profile"):
            self.assertIn(name, hosted)
        self.assertTrue(hosted["coaching_history"].annotations.readOnlyHint)
        self.assertFalse(hosted["log_coaching_decision"].annotations.openWorldHint)


if __name__ == "__main__":
    unittest.main()
