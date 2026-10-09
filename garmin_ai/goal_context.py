"""Goal-independent evidence for adjustable race scenarios."""
from datetime import date, timedelta

import pandas as pd

from garmin_ai import race_prediction
from garmin_ai.discoveries import run_observations


def build_goal_context(activities, daily, as_of=None):
    as_of = as_of or date.today()
    model = race_prediction.build_model(activities, pd.DataFrame(), as_of)
    previous = race_prediction.build_model(activities, pd.DataFrame(), as_of - timedelta(days=28))
    # Match each recent run to a distinct earlier run, rather than compare all pace averages.
    runs = run_observations(activities, pd.DataFrame(), as_of - timedelta(days=55), as_of,
                            distance=(1, 100), heart_rate=(40, 230), max_climb=200)
    cutoff = pd.Timestamp(as_of - timedelta(days=27))
    old = runs[runs.date < cutoff].copy()
    recent = runs[runs.date >= cutoff]
    pairs = []
    for _, run in recent.iterrows():
        eligible = old[(old.avg_hr - run.avg_hr).abs().le(3)
                       & (old.distance_km / run.distance_km - 1).abs().le(.2)
                       & (old.climb_m_per_km - run.climb_m_per_km).abs().le(5)]
        if eligible.empty:
            continue
        index = ((eligible.avg_hr - run.avg_hr).abs() / 3
                 + (eligible.distance_km / run.distance_km - 1).abs() / .2
                 + (eligible.climb_m_per_km - run.climb_m_per_km).abs() / 5).idxmin()
        prior = old.loc[index]
        pairs.append({"recent_date": run.date.date().isoformat(), "earlier_date": prior.date.date().isoformat(),
                      "pace_change_seconds": float((run.pace_min_km-prior.pace_min_km)*60),
                      "recent_hr": float(run.avg_hr), "earlier_hr": float(prior.avg_hr),
                      "recent_km": float(run.distance_km), "earlier_km": float(prior.distance_km)})
        old = old.drop(index)
    response = {}
    if not daily.empty:
        stats = daily.copy()
        dates = pd.to_datetime(stats.date).dt.date
        for column in ("hrv_overnight", "resting_hr", "training_readiness", "stress_avg"):
            values = pd.to_numeric(stats[column], errors="coerce")
            now = values[(dates >= as_of-timedelta(days=6)) & (dates <= as_of)].dropna()
            baseline = values[(dates >= as_of-timedelta(days=27)) & (dates < as_of-timedelta(days=6))].dropna()
            response[column] = {"recent_n": len(now), "baseline_n": len(baseline),
                                "change": float(now.median()-baseline.median()) if len(now)>=4 and len(baseline)>=10 else None}
    model.pop("credible_efforts", None)
    return {"model": model, "previous_predictions": previous["predictions"], "previous_profile": previous["training_profile"],
            "matched_runs": pairs, "recovery_trends": response,
            "latest_daily_date": str(daily.date.max())[:10] if not daily.empty else None}
