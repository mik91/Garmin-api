import json
import unittest
from datetime import date
from unittest.mock import patch

from garmin_ai import coach, db, mcp_server
from garmin_ai.coaching import load_rules, season
from test_coach_review import Base, run, week

RACE = {"name": "City 10K", "date": "2027-05-29", "priority": "A", "distance": "10K", "goal": "10K under 38:30"}
ANCHOR = {"start_week": "2026-11-16", "start_run_km": 32.8, "start_long_run_km": 9.4, "peak_run_km": 56,
          "start_long_swim_m": 1550, "start_vdot": 50.4}


class GenerateTests(unittest.TestCase):
    def setUp(self):
        self.weeks = season.generate(ANCHOR, RACE, load_rules())

    def test_phases_run_in_order_to_the_race_week(self):
        phases = [w["phase"] for w in self.weeks]
        order = ["base", "build", "peak", "taper", "race_week"]
        self.assertEqual([p for i, p in enumerate(phases) if i == 0 or phases[i - 1] != p], order)
        self.assertEqual(phases.count("taper"), 1)  # a 10K tapers for one week
        self.assertEqual(phases.count("peak"), 4)
        self.assertEqual(self.weeks[-1]["week_start"], "2027-05-24")
        self.assertEqual(self.weeks[-1]["quality"][-1]["type"], "race")

    def test_volume_and_long_run_limits(self):
        for i, (prev, cur) in enumerate(zip(self.weeks, self.weeks[1:]), start=1):
            if cur["light"] or cur["phase"] in ("taper", "race_week"):
                continue
            if prev["light"]:  # back over two weeks: halfway, then the volume from before
                before, after = self.weeks[i - 2], self.weeks[i + 1]
                self.assertAlmostEqual(cur["run_km"], (prev["run_km"] + before["run_km"]) / 2, delta=0.1)
                if not after["light"] and after["phase"] not in ("taper", "race_week"):
                    self.assertEqual(after["run_km"], before["run_km"])
            elif i < 2 or not self.weeks[i - 2]["light"]:
                self.assertLessEqual(cur["run_km"], prev["run_km"] * 1.1 + 1e-9)
        normal = [w for w in self.weeks if not w["light"] and w["phase"] not in ("taper", "race_week")]
        for i, cur in enumerate(normal[1:], start=1):  # <= 1.5 km over the longest so far
            longest = max(w["long_run_km"] for w in normal[:i])
            self.assertLessEqual(cur["long_run_km"], longest + 1.5 + 1e-9)
        for w in self.weeks:
            self.assertLessEqual(w["run_km"], 56)
            self.assertLessEqual(w["long_run_km"], 18)
            if w["phase"] == "base":
                self.assertLessEqual(w["run_km"], 56 * 0.9 + 0.1)
            if w["run_km"]:
                self.assertLessEqual(w["long_run_km"], 0.3 * w["run_km"] + 0.1)
        self.assertEqual(max(w["run_km"] for w in self.weeks), 56)

    def test_lighter_weeks_once_near_peak(self):
        light = [w for w in self.weeks if w["light"]]
        self.assertTrue(light)
        self.assertFalse(any(w["light"] for w in self.weeks[:4]))  # rebuilding at the start
        first = self.weeks.index(light[0])
        normal_since = 0
        for w in self.weeks[first:]:
            if w["phase"] in ("taper", "race_week"):
                break
            normal_since = 0 if w["light"] else normal_since + 1
            self.assertLess(normal_since, 4)  # 3 normal weeks, then a lighter one

    def test_quality_by_phase_and_progression(self):
        by_phase = {w["phase"]: [q["type"] for q in w["quality"]] for w in self.weeks if not w["light"]}
        self.assertEqual(by_phase["base"], ["threshold"])
        self.assertEqual(by_phase["build"], ["threshold", "vo2"])
        self.assertIn("race_pace", by_phase["peak"])
        threshold = [q for w in self.weeks for q in w["quality"] if q["type"] == "threshold"]
        self.assertEqual(threshold[0]["session"], "cruise intervals: 3 x 5 min T / 2 min jog")
        formats = {q["format"] for q in threshold}
        self.assertEqual(formats, {"cruise", "pyramid", "progression", "tempo", "fartlek"})
        for a, b in zip(threshold, threshold[1:]):  # never the same format twice in a row
            self.assertNotEqual(a["format"], b["format"])
        vo2 = {q["format"] for w in self.weeks for q in w["quality"] if q["type"] == "vo2"}
        self.assertTrue({"minutes", "800s", "pyramid", "1k", "hills"} <= vo2)
        light = next(w for w in self.weeks if w["light"])
        self.assertEqual(len(light["quality"]), 1)

    def test_doses_stay_within_daniels_limits(self):
        from garmin_ai.coaching import sessions
        for w in self.weeks:
            for q in w["quality"]:
                if q["type"] in ("threshold", "vo2") and w["run_km"]:
                    minutes = float(q["dose"].split()[0])
                    kind = "T" if q["type"] == "threshold" else "I"
                    km_at = minutes * 60 / sessions.pace(50.4, kind)
                    limit = sessions.LIMITS[q["type"]] * w["run_km"]
                    self.assertLessEqual(km_at, max(limit, 15 * 0.8 * 60 / sessions.pace(50.4, kind)) + 0.3,
                                         (w["week_start"], q))

    def test_challenge_race_week_and_rehearsals(self):
        challenge = {**RACE, "events": ["2K", "5K", "10K"]}
        weeks = season.generate({**ANCHOR, "start_vdot": 50.4}, challenge, load_rules())
        race_week = weeks[-1]
        self.assertEqual(race_week["race_day_km"], 17)
        events = [q["session"] for q in race_week["quality"] if q["type"] == "race"]
        self.assertEqual(len(events), 3)
        self.assertTrue(events[0].startswith("2K controlled") and events[1].startswith("5K controlled"))
        self.assertIn("38:30", events[2])
        rehearsals = [w for w in weeks if any(q.get("rehearsal") for q in w["quality"])]
        self.assertEqual(len(rehearsals), 2)
        for w in rehearsals:
            self.assertEqual(w["phase"], "peak")
            self.assertEqual(len(w["quality"]), 1)  # the rehearsal day is the week's only quality
            self.assertEqual((w["runs"], w["long_run"]), (6, "easy"))
            self.assertIn("AM 7 km steady", w["quality"][0]["session"])
        self.assertFalse(any(q.get("rehearsal") for w in self.weeks for q in w["quality"]))  # single race

    def test_long_run_finishes(self):
        finishes = {(w["phase"], w["long_run"]) for w in self.weeks}
        self.assertIn(("build", "easy, last 15 min at marathon pace"), finishes)
        self.assertFalse(any("at T" in w["long_run"] for w in self.weeks))
        self.assertTrue(all(w["long_run"] == "easy" for w in self.weeks if w["phase"] == "base" or w["light"]))

    def test_swim_and_flex(self):
        self.assertTrue(all(w["swim"]["sessions"] == 3 for w in self.weeks))
        self.assertTrue(any(w["swim"]["css_test"] for w in self.weeks))  # details in test_swim.py
        rules = load_rules()
        rules["structure"]["flex"] = {"count": 1, "sports": ["gym", "bike"],
                                      "prefer": {"default": "gym", "build": "bike", "peak": "bike"}}
        flex = {w["phase"]: w["flex"] for w in season.generate(ANCHOR, RACE, rules)}
        self.assertEqual((flex["base"], flex["build"], flex["peak"]), ("gym", "bike", "bike"))

    def test_goal_and_distance(self):
        self.assertEqual(season.goal_pace(RACE)["pace_min_per_km"], "3:51")
        self.assertAlmostEqual(season.goal_pace(RACE)["vdot"], 54.3, delta=0.1)
        self.assertIsNone(season.goal_pace({**RACE, "goal": "have fun"}))
        self.assertEqual(season.distance_key({"name": "Half marathon", "distance": "21.1 km"}), "half")
        self.assertEqual(season.distance_key({"name": "Marathon de Montreal"}), "marathon")


class SeasonToolTests(Base):
    def setUp(self):
        super().setUp()
        mcp_server.update_athlete_profile("races", json.dumps([RACE]))

    def test_dry_run_then_save_then_read(self):
        with patch.object(season, "date") as fake:
            fake.today.return_value, fake.fromisoformat = date(2026, 11, 12), date.fromisoformat
            dry = json.loads(mcp_server.start_season_plan(peak_run_km=56))
        self.assertTrue(dry["dry_run"])
        self.assertEqual(dry["anchor"]["start_week"], "2026-11-16")
        self.assertEqual(dry["goal"]["time"], "38:30")
        self.assertIn("No season plan", mcp_server.season_plan())
        json.loads(mcp_server.start_season_plan(peak_run_km=56, dry_run=False))
        saved = json.loads(mcp_server.season_plan(weeks=2))
        self.assertEqual(len(saved["weeks"]), 2)
        self.assertEqual(saved["anchor"]["peak_run_km"], 56)
        logged = json.loads(mcp_server.coaching_history(kind="profile"))
        self.assertIn("Season plan for City 10K", logged[0]["summary"])

    def test_targets_are_capped_by_last_week_and_checked_on_upload(self):
        mcp_server.update_athlete_profile("season_plan", json.dumps({**ANCHOR, "start_run_km": 40}))
        with db.get_connection() as conn:
            target = season.targets_for(conn, "2026-11-23", last_week_km=20)
        self.assertEqual(target["run_km"], 22.0)
        self.assertEqual(target["planned_run_km"], 48.4)  # 40 -> 44 (week 1) -> 48.4
        result = coach.upload_plan(week("2026-11-16"), dry_run=True)
        checked = result["validation"][0]
        self.assertEqual(checked["season_target"]["week_start"], "2026-11-16")
        # The fixture week has 5 runs (~38 km) against a 40 km target: no volume warning,
        # but 1 quality session where the base plan has 1 -> no quality warning either.
        self.assertEqual([w for w in checked["warnings"] if w["rule"] == "season"], [])
        heavy = week("2026-11-16") + [{"date": "2026-11-19", "workout": run("Intervals", 30, zone=5)}]
        checked = coach.upload_plan(heavy, dry_run=True)["validation"][0]
        self.assertTrue(any("quality sessions planned; the season plan has 1" in w["message"]
                            for w in checked["warnings"] if w["rule"] == "season"), checked["warnings"])

    def test_a_lighter_week_run_as_planned_does_not_cap_the_way_back(self):
        mcp_server.update_athlete_profile("season_plan", json.dumps(ANCHOR))
        weeks = season.generate(ANCHOR, RACE, load_rules())
        i = next(i for i, w in enumerate(weeks) if w["light"])
        light, back = weeks[i], weeks[i + 1]
        with db.get_connection() as conn:
            as_planned = season.targets_for(conn, back["week_start"], last_week_km=light["run_km"])
            short = season.targets_for(conn, back["week_start"], last_week_km=light["run_km"] - 10)
        self.assertEqual(as_planned["run_km"], back["run_km"])
        self.assertNotIn("planned_run_km", as_planned)
        self.assertLess(short["run_km"], back["run_km"])  # a missed week still slows the plan

    def test_bad_season_plan_is_refused(self):
        self.assertTrue(mcp_server.update_athlete_profile("season_plan", '{"start_week": "2026-11-16"}')
                        .startswith("Error: season_plan needs"))


if __name__ == "__main__":
    unittest.main()
