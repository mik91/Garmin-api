import json
import sqlite3
import unittest
from datetime import date, timedelta

from garmin_ai import db
from garmin_ai.coaching import history, load_rules, materialize, season, validate_week

ANCHOR = {"start_week": "2026-11-16", "start_run_km": 32.8, "start_long_run_km": 9.4, "peak_run_km": 56,
          "start_vdot": 50.4}
TEN_K = {"name": "City 10K", "date": "2027-05-29", "priority": "A", "distance": "10K", "goal": "10K under 38:30"}
SEASONS = [
    (TEN_K, ANCHOR),
    ({**TEN_K, "name": "Relay festival", "events": ["2K", "5K", "10K"]}, ANCHOR),
    ({"name": "Spring half", "date": "2027-04-11", "priority": "A", "distance": "half", "goal": "1:25:00"},
     {"start_week": "2026-11-16", "start_run_km": 40, "start_long_run_km": 14, "peak_run_km": 65, "start_vdot": 48}),
    ({"name": "Fall marathon", "date": "2027-10-10", "priority": "A", "distance": "marathon", "goal": "3:10:00"},
     {"start_week": "2027-03-01", "start_run_km": 45, "start_long_run_km": 18, "peak_run_km": 80, "start_vdot": 47}),
]
CSS = 100.0


def materialize_season(race, anchor):
    """Every week materialized and validated in order, each against the weeks before it."""
    rules = load_rules()
    weeks = season.generate(anchor, race, rules, CSS, with_steps=True)
    goal = season.goal_pace(race)
    goal_s = goal["seconds"] / season.DISTANCE_M[season.distance_key(race)] * 1000
    weekly = [anchor["start_run_km"]]
    runs = [(date.fromisoformat(anchor["start_week"]) - timedelta(days=1), anchor["start_long_run_km"])]
    last_quality = last_all_out = None
    out = []
    for i, target in enumerate(weeks):
        vdot = season.expected_vdot(anchor, race, len(weeks), i - 1) if i else anchor["start_vdot"]
        items = materialize.week(target, vdot, goal_s, race)
        sessions = materialize.to_sessions(items, materialize.paces_for(vdot))
        monday = date.fromisoformat(target["week_start"])
        used = {"weekly_run_km": weekly[-12:],
                "longest_run_km_30d": max(km for d, km in runs if (monday - d).days <= 30),
                "weeks_since_light": history.weeks_since_light(weekly),
                "last_quality_date": last_quality, "last_all_out_date": last_all_out,
                "season_phase": target["phase"]}
        result = validate_week({"week_start": target["week_start"], "sessions": sessions}, used, rules)
        out.append({"target": target, "items": items, "sessions": sessions, "result": result})
        weekly.append(result["summary"]["planned_run_km"])
        runs += [(date.fromisoformat(s["date"]), s["km"]) for s in sessions if s["sport"] == "run"]
        quality = [s["date"] for s in sessions if s.get("intensity") in ("quality", "race")]
        last_quality = max(quality, default=last_quality)
        last_all_out = max((s["date"] for s in sessions if s.get("all_out")), default=last_all_out)
    return out


class FullSeasonTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seasons = {race["name"]: (race, materialize_season(race, anchor)) for race, anchor in SEASONS}

    def test_every_week_passes_the_coaching_rules(self):
        for name, (race, weeks) in self.seasons.items():
            for week in weeks:
                with self.subTest(race=name, week=week["target"]["week_start"]):
                    self.assertEqual(week["result"]["errors"], [])

    def test_weekly_km_meets_the_target_without_going_over(self):
        for name, (race, weeks) in self.seasons.items():
            for week in weeks:
                planned, target = week["result"]["summary"]["planned_run_km"], week["target"]["run_km"]
                if week["target"]["phase"] == "race_week":  # the race itself can be most of the week
                    planned = sum(s["km"] for s in week["sessions"] if s["sport"] == "run" and s["date"] < race["date"])
                with self.subTest(race=name, week=week["target"]["week_start"]):
                    self.assertLessEqual(planned, target + 1e-6)
                    if week["target"]["phase"] != "race_week":
                        self.assertGreaterEqual(planned, target - 1.0)

    def test_every_week_has_its_quality_long_run_and_swims(self):
        for name, (race, weeks) in self.seasons.items():
            for week in weeks:
                t, items = week["target"], week["items"]
                kinds = [i["kind"] for i in items]
                with self.subTest(race=name, week=t["week_start"]):
                    if t["phase"] != "race_week":
                        self.assertEqual(kinds.count("long"), 1)
                        quality = [q for q in t["quality"] if q["type"] != "race"]
                        self.assertEqual(sum(k in ("threshold", "vo2", "race_pace", "sharpen", "time_trial")
                                             for k in kinds), len(quality))
                        self.assertEqual(sum(i["sport"] == "swim" for i in items), t["swim"]["sessions"])
                    self.assertEqual(len({(i["date"], i["name"]) for i in items}), len(items))

    def test_easy_runs_have_hr_targets_only(self):
        for _, (_, weeks) in self.seasons.items():
            for week in weeks:
                for item in week["items"]:
                    if item["kind"] in ("easy", "long"):
                        for step in item["spec"]["steps"]:
                            self.assertNotIn("pace_min_per_km", str(step.get("target")))

    def test_race_week(self):
        race, weeks = self.seasons["Relay festival"]
        items = weeks[-1]["items"]
        self.assertTrue(all(i["date"] <= race["date"] for i in items))
        on_race_day = [i["name"] for i in items if i["date"] == race["date"] and i["sport"] == "run"]
        self.assertEqual(on_race_day, ["2K controlled", "5K controlled", "Race day: 10K"])
        sharpen = next(i for i in items if i["kind"] == "sharpen")
        self.assertEqual(sharpen["date"], "2027-05-26")  # three days before
        race_session = next(s for s in weeks[-1]["sessions"] if s["name"] == "Race day: 10K")
        self.assertTrue(race_session["all_out"])

    def test_deterministic(self):
        target = season.generate(ANCHOR, TEN_K, load_rules(), CSS, with_steps=True)[10]
        self.assertEqual(materialize.week(target, 50.4, 231), materialize.week(target, 50.4, 231))


class StepsTests(unittest.TestCase):
    def test_quality_steps_only_when_asked(self):
        plain = season.generate(ANCHOR, TEN_K, load_rules())
        detailed = season.generate(ANCHOR, TEN_K, load_rules(), with_steps=True)
        self.assertFalse(any("steps" in q for w in plain for q in w["quality"]))
        built = [q for w in detailed for q in w["quality"] if q["type"] in ("threshold", "vo2", "race_pace")]
        self.assertTrue(built and all(q["steps"] for q in built))

    def test_swim_text_to_steps(self):
        steps = materialize._swim_steps("200 easy, 200 m drills (catch-up and sculling), "
                                        "6 x 100 m @ CSS+5 s (~1:45/100 m) / 20 s rest, 300 m pull easy, 100 easy")
        self.assertEqual([s["type"] for s in steps], ["warmup", "interval", "repeat", "interval", "cooldown"])
        self.assertEqual(steps[2]["repeat"]["count"], 6)
        self.assertEqual(steps[2]["repeat"]["steps"][1], {"type": "rest", "duration": {"time_s": 20}})
        self.assertEqual(steps[3]["swim"]["equipment"], "pull_buoy")

    def test_sharpen_text_to_steps(self):
        steps = materialize._parse_reps("6 x 400 m at I / 2 min jog")
        self.assertEqual(steps[0]["repeat"]["count"], 6)
        self.assertEqual(steps[0]["repeat"]["steps"][0]["pace"], "I")


class TablesTests(unittest.TestCase):
    def test_plan_tables_exist(self):
        conn = sqlite3.connect(":memory:")
        conn.executescript(db.SCHEMA)
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        self.assertTrue({"plan_drafts", "plan_versions"} <= names)


USER_SCHEDULE = {"long": "sat", "quality": ["tue", "thu"], "test": "wed", "easy": ["wed", "fri", "mon"],
                 "swims": ["mon", "thu", "sun"], "swim_hard": "thu", "swim_long": "sun",
                 "midday_swims": ["mon", "thu"], "gym": "wed", "bike": "thu", "flex": "sun"}


class CustomScheduleTests(unittest.TestCase):
    """A week like the athlete's own: Saturday long run, mid-week trials, short midday swims."""

    @classmethod
    def setUpClass(cls):
        cls.rules = history._merge(load_rules(), {"structure": {"gym_min": 1, "bikes_min": 0}})
        race = {**TEN_K, "events": ["2K", "5K", "10K"]}
        cls.weeks = [(t, materialize.week(t, 50.4, 231, race, USER_SCHEDULE, cls.rules,
                                          materialize.hr_targets({"floors": [105, 135, 149, 154, 163]}), 174))
                     for t in season.generate(ANCHOR, race, cls.rules, CSS, with_steps=True)]

    def day(self, item):
        return date.fromisoformat(item["date"]).strftime("%a")

    def test_long_run_saturday_and_trials_mid_week_with_one_run_less(self):
        trials = 0
        for target, items in self.weeks:
            runs = [i for i in items if i["sport"] == "run"]
            longs = [i for i in runs if i["kind"] == "long"]
            if longs:
                self.assertEqual({self.day(i) for i in longs}, {"Sat"})
            trial = [i for i in runs if i["kind"] == "time_trial"]
            if trial:
                trials += 1
                self.assertEqual(self.day(trial[0]), "Wed")
                self.assertEqual(len({i["date"] for i in runs}), target["runs"] - 1)
                swims = [i for i in items if i["sport"] == "swim"]
                self.assertFalse(any("CSS set" in i["name"] for i in swims))  # swum steady that week
        self.assertEqual(trials, 3)

    def test_midday_swims_fit_and_the_long_swim_is_on_sunday(self):
        for target, items in self.weeks:
            for item in items:
                if item["sport"] != "swim":
                    continue
                minutes = materialize._swim_steps_minutes(item["spec"]["steps"], 174)
                if item["session"].get("slot") == "midday":
                    self.assertLessEqual(minutes, 25 + 1e-9, item["name"])
                    self.assertNotIn("CSS test", item["name"])
                else:
                    self.assertEqual(self.day(item), "Sun")
                    self.assertLessEqual(minutes, 75 + 1e-9, item["name"])
                if "CSS set" in item["name"]:
                    self.assertEqual(self.day(item), "Thu")
                if "CSS test" not in item["name"]:  # the test's 10 min easy is timed, not measured
                    self.assertEqual(int(item["name"].split()[-2]), materialize._swim_meters(item["spec"]["steps"]))

    def test_speed_work_is_never_the_day_before_a_hard_day(self):
        days = []
        for target, items in self.weeks:
            hard = {i["date"] for i in items if i["kind"] in materialize.HARD_KINDS}
            for item in items:
                if item["name"].endswith("+ speed"):
                    after = (date.fromisoformat(item["date"]) + timedelta(days=1)).isoformat()
                    self.assertNotIn(after, hard, f"{item['date']} {item['name']}")
                    days.append(self.day(item))
        self.assertTrue(days)
        self.assertEqual(set(days), {"Fri"})  # base and build: between Thursday and the Saturday long run

    def test_cut_swims_describe_their_real_steps(self):
        for target, items in self.weeks:
            for item in items:
                if item["session"].get("slot") == "midday":
                    text = item["spec"]["description"]  # cut or not, the text matches the steps
                    for step in item["spec"]["steps"]:
                        if step["type"] == "repeat":
                            work = next(s for s in step["repeat"]["steps"] if s["type"] != "rest")
                            self.assertIn(f"{step['repeat']['count']} x {work['duration']['distance_m']} m", text)

    def test_no_bike_when_the_rules_need_none(self):
        bikes = [i for _, items in self.weeks for i in items if i["kind"] == "bike"]
        self.assertEqual(bikes, [])

    def test_schedule_keys_are_checked(self):
        from garmin_ai.coaching import rolling
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(db.SCHEMA)
        conn.execute("INSERT INTO coach_profile VALUES ('plan_schedule', ?, 'x')", (json.dumps(USER_SCHEDULE),))
        self.assertEqual(rolling.schedule(conn)["swim_long"], "sun")
        conn.execute("UPDATE coach_profile SET content = ? WHERE section = 'plan_schedule'",
                     (json.dumps({"midday_swims": "mon"}),))
        with self.assertRaises(history.HistoryError):
            rolling.schedule(conn)


if __name__ == "__main__":
    unittest.main()
