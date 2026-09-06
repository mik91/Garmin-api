from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

import pandas as pd


@dataclass
class Insight:
    category: str
    severity: str
    title: str
    detail: str
    metric: str | None = None
    value: float | None = None
    baseline: float | None = None
    confidence: str = "medium"
    sample_size: int | None = None
    action: str | None = None

    def as_dict(self) -> dict:
        return {
            "category": self.category,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "metric": self.metric,
            "value": self.value,
            "baseline": self.baseline,
            "confidence": self.confidence,
            "sample_size": self.sample_size,
            "action": self.action,
        }


def _clean(value):
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def _mean(df: pd.DataFrame, column: str):
    if df.empty or column not in df or not df[column].notna().any():
        return None
    return _clean(df[column].mean(skipna=True))


def _sum(df: pd.DataFrame, column: str):
    if df.empty or column not in df or not df[column].notna().any():
        return None
    return _clean(df[column].sum(skipna=True))


def _count(df: pd.DataFrame, column: str) -> int:
    if df.empty or column not in df:
        return 0
    return int(df[column].notna().sum())


def _pct_change(current, baseline):
    if current is None or baseline in (None, 0) or pd.isna(current) or pd.isna(baseline):
        return None
    return (current - baseline) / baseline * 100


def _diff(current, baseline):
    if current is None or baseline is None or pd.isna(current) or pd.isna(baseline):
        return None
    return current - baseline


def _window(df: pd.DataFrame, column: str, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return df[(df[column] >= start_ts) & (df[column] <= end_ts)].copy()


def _activity_window(df: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    return _window(df, "start_time", start, end)


def _period_length(start: date, end: date) -> int:
    return max((end - start).days + 1, 1)


def _previous_bounds(start: date, end: date) -> tuple[date, date]:
    days = _period_length(start, end)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return previous_start, previous_end


def _baseline_bounds(start: date, days: int = 90) -> tuple[date, date]:
    baseline_end = start - timedelta(days=1)
    return baseline_end - timedelta(days=days - 1), baseline_end


def _metrics(
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    activities: pd.DataFrame,
    start: date,
    end: date,
) -> dict:
    period_days = _period_length(start, end)
    distance_m = _sum(activities, "distance_m")
    duration_s = _sum(activities, "duration_s")
    training_load = _activity_training_load(activities)
    return {
        "period_days": period_days,
        "daily_days": int(daily["date"].dt.date.nunique()) if not daily.empty else 0,
        "sleep_nights": int(sleep["date"].dt.date.nunique()) if not sleep.empty else 0,
        "activity_count": int(len(activities)),
        "avg_steps": _mean(daily, "steps"),
        "total_steps": _sum(daily, "steps"),
        "avg_resting_hr": _mean(daily, "resting_hr"),
        "avg_stress": _mean(daily, "stress_avg"),
        "avg_body_battery_high": _mean(daily, "body_battery_high"),
        "avg_hrv_overnight": _mean(daily, "hrv_overnight"),
        "avg_training_readiness": _mean(daily, "training_readiness"),
        "avg_recovery_time_hours": _mean(daily, "recovery_time_hours"),
        "latest_training_status": _clean(daily["training_status"].dropna().iloc[-1])
        if "training_status" in daily and daily["training_status"].notna().any()
        else None,
        "latest_vo2_max": _clean(daily["vo2_max"].dropna().iloc[-1])
        if "vo2_max" in daily and daily["vo2_max"].notna().any()
        else None,
        "moderate_intensity_min": _sum(daily, "moderate_intensity_min"),
        "vigorous_intensity_min": _sum(daily, "vigorous_intensity_min"),
        "avg_respiration": _mean(daily, "avg_respiration"),
        "avg_spo2": _mean(daily, "avg_spo2"),
        "hrv_days": _count(daily, "hrv_overnight"),
        "readiness_days": _count(daily, "training_readiness"),
        "recovery_days": _count(daily, "recovery_time_hours"),
        "vo2_days": _count(daily, "vo2_max"),
        "avg_sleep_hours": (_mean(sleep, "total_sleep_s") or 0) / 3600 if _mean(sleep, "total_sleep_s") is not None else None,
        "avg_sleep_score": _mean(sleep, "sleep_score"),
        "distance_km": distance_m / 1000 if distance_m is not None else None,
        "duration_hours": duration_s / 3600 if duration_s is not None else None,
        "weekly_distance_km": (distance_m / 1000 / period_days * 7) if distance_m is not None else None,
        "weekly_duration_hours": (duration_s / 3600 / period_days * 7) if duration_s is not None else None,
        "training_load": training_load,
        "weekly_training_load": training_load / period_days * 7 if training_load is not None else None,
    }


def _activity_training_load(activities: pd.DataFrame):
    if activities.empty or "raw_json" not in activities:
        return None
    values = []
    for raw in activities["raw_json"].dropna():
        try:
            import json

            value = json.loads(raw or "{}").get("activityTrainingLoad")
        except (TypeError, ValueError):
            value = None
        if value is not None and not pd.isna(value):
            values.append(float(value))
    return sum(values) if values else None


def _running_pace_min_per_km(activities: pd.DataFrame):
    if activities.empty:
        return None
    distance_m = _sum(activities, "distance_m")
    duration_s = 0.0
    for _, row in activities.iterrows():
        moving = row.get("moving_duration_s")
        if moving is None or pd.isna(moving):
            try:
                import json

                moving = json.loads(row.get("raw_json") or "{}").get("movingDuration")
            except (TypeError, ValueError):
                moving = None
        if moving is None or pd.isna(moving):
            moving = row.get("duration_s")
        if moving is not None and not pd.isna(moving):
            duration_s += float(moving)
    if not distance_m or not duration_s:
        return None
    return (duration_s / 60) / (distance_m / 1000)


def _sport_summaries(activities: pd.DataFrame, start: date, end: date) -> dict:
    if activities.empty or "category" not in activities:
        return {}
    period_days = _period_length(start, end)
    summaries = {}
    for category, category_df in activities.groupby("category"):
        distance_m = _sum(category_df, "distance_m")
        duration_s = _sum(category_df, "duration_s")
        summaries[category] = {
            "count": int(len(category_df)),
            "distance_km": distance_m / 1000 if distance_m is not None else None,
            "duration_hours": duration_s / 3600 if duration_s is not None else None,
            "weekly_distance_km": distance_m / 1000 / period_days * 7 if distance_m is not None else None,
            "weekly_duration_hours": duration_s / 3600 / period_days * 7 if duration_s is not None else None,
            "avg_hr": _mean(category_df, "avg_hr"),
            "pace_min_per_km": _running_pace_min_per_km(category_df) if category == "Running" else None,
            "training_load": _activity_training_load(category_df),
        }
    return summaries


def _add_change_insights(insights: list[Insight], current: dict, previous: dict, baseline: dict):
    step_change = _pct_change(current.get("avg_steps"), previous.get("avg_steps"))
    if step_change is not None and abs(step_change) >= 15:
        insights.append(
            Insight(
                "Consistency",
                "positive" if step_change > 0 else "notice",
                "Step pattern changed",
                f"Average steps are {abs(step_change):.0f}% {'higher' if step_change > 0 else 'lower'} than the previous period.",
                "avg_steps",
                current.get("avg_steps"),
                previous.get("avg_steps"),
                sample_size=min(current.get("daily_days", 0), previous.get("daily_days", 0)),
            )
        )

    sleep_diff = _diff(current.get("avg_sleep_hours"), baseline.get("avg_sleep_hours"))
    if sleep_diff is not None and abs(sleep_diff) >= 0.5:
        insights.append(
            Insight(
                "Sleep",
                "positive" if sleep_diff > 0 else "warning",
                "Sleep moved away from baseline",
                f"Average sleep is {abs(sleep_diff):.1f} hours {'above' if sleep_diff > 0 else 'below'} your 90-day baseline.",
                "avg_sleep_hours",
                current.get("avg_sleep_hours"),
                baseline.get("avg_sleep_hours"),
                sample_size=min(current.get("sleep_nights", 0), baseline.get("sleep_nights", 0)),
            )
        )

    hr_diff = _diff(current.get("avg_resting_hr"), baseline.get("avg_resting_hr"))
    if hr_diff is not None and abs(hr_diff) >= 3:
        insights.append(
            Insight(
                "Recovery",
                "warning" if hr_diff > 0 else "positive",
                "Resting HR shifted",
                f"Resting HR is {abs(hr_diff):.0f} bpm {'above' if hr_diff > 0 else 'below'} your 90-day baseline.",
                "avg_resting_hr",
                current.get("avg_resting_hr"),
                baseline.get("avg_resting_hr"),
                sample_size=min(current.get("daily_days", 0), baseline.get("daily_days", 0)),
            )
        )

    hrv_change = _pct_change(
        current.get("avg_hrv_overnight"), baseline.get("avg_hrv_overnight")
    )
    if hrv_change is not None and abs(hrv_change) >= 10:
        insights.append(
            Insight(
                "Recovery",
                "positive" if hrv_change > 0 else "warning",
                "Overnight HRV shifted",
                f"Average overnight HRV is {abs(hrv_change):.0f}% {'above' if hrv_change > 0 else 'below'} baseline.",
                "avg_hrv_overnight",
                current.get("avg_hrv_overnight"),
                baseline.get("avg_hrv_overnight"),
                sample_size=min(current.get("hrv_days", 0), baseline.get("hrv_days", 0)),
            )
        )

    readiness_diff = _diff(
        current.get("avg_training_readiness"), baseline.get("avg_training_readiness")
    )
    if readiness_diff is not None and abs(readiness_diff) >= 10:
        insights.append(
            Insight(
                "Recovery",
                "positive" if readiness_diff > 0 else "warning",
                "Training readiness changed",
                f"Average training readiness is {abs(readiness_diff):.0f} points {'above' if readiness_diff > 0 else 'below'} baseline.",
                "avg_training_readiness",
                current.get("avg_training_readiness"),
                baseline.get("avg_training_readiness"),
                sample_size=min(
                    current.get("readiness_days", 0), baseline.get("readiness_days", 0)
                ),
            )
        )

    stress_diff = _diff(current.get("avg_stress"), baseline.get("avg_stress"))
    if stress_diff is not None and abs(stress_diff) >= 5:
        insights.append(
            Insight(
                "Recovery",
                "notice" if stress_diff > 0 else "positive",
                "Stress changed",
                f"Average stress is {abs(stress_diff):.0f} points {'above' if stress_diff > 0 else 'below'} your 90-day baseline.",
                "avg_stress",
                current.get("avg_stress"),
                baseline.get("avg_stress"),
                sample_size=min(current.get("daily_days", 0), baseline.get("daily_days", 0)),
            )
        )

    using_load = current.get("weekly_training_load") is not None and previous.get("weekly_training_load") is not None
    volume_metric = "weekly_training_load" if using_load else "weekly_distance_km"
    volume_change = _pct_change(current.get(volume_metric), previous.get(volume_metric))
    if volume_change is not None and abs(volume_change) >= 20:
        volume_label = "training load" if using_load else "distance"
        insights.append(
            Insight(
                "Training load",
                "warning" if volume_change > 25 else "notice",
                "Training volume changed",
                f"Weekly-equivalent {volume_label} is {abs(volume_change):.0f}% {'higher' if volume_change > 0 else 'lower'} than the previous period.",
                volume_metric,
                current.get(volume_metric),
                previous.get(volume_metric),
                sample_size=min(
                    current.get("activity_count", 0), previous.get("activity_count", 0)
                ),
            )
        )


def _add_combination_insights(insights: list[Insight], current: dict, baseline: dict):
    sleep_diff = _diff(current.get("avg_sleep_hours"), baseline.get("avg_sleep_hours"))
    hr_diff = _diff(current.get("avg_resting_hr"), baseline.get("avg_resting_hr"))
    volume_metric = (
        "weekly_training_load"
        if current.get("weekly_training_load") is not None
        and baseline.get("weekly_training_load") is not None
        else "weekly_distance_km"
    )
    volume_change = _pct_change(current.get(volume_metric), baseline.get(volume_metric))

    if sleep_diff is not None and hr_diff is not None and sleep_diff <= -0.4 and hr_diff >= 3:
        insights.append(
            Insight(
                "Recovery",
                "warning",
                "Recovery pressure is elevated",
                "Sleep is below baseline while resting HR is above baseline, a combination worth watching before adding intensity.",
                sample_size=min(current.get("sleep_nights", 0), baseline.get("sleep_nights", 0)),
            )
        )

    if volume_change is not None and volume_change >= 25 and hr_diff is not None and hr_diff >= 2:
        insights.append(
            Insight(
                "Training load",
                "warning",
                "Load spike with higher resting HR",
                "Training volume is up sharply while resting HR is also elevated, which can point to accumulating fatigue.",
                sample_size=min(
                    current.get("activity_count", 0), baseline.get("activity_count", 0)
                ),
            )
        )

    hrv_change = _pct_change(
        current.get("avg_hrv_overnight"), baseline.get("avg_hrv_overnight")
    )
    readiness = current.get("avg_training_readiness")
    if hrv_change is not None and hrv_change <= -10 and readiness is not None and readiness < 50:
        insights.append(
            Insight(
                "Recovery",
                "warning",
                "Recovery signals are aligned downward",
                "Overnight HRV is below baseline while training readiness is also low; consider reducing intensity until the trend improves.",
                sample_size=min(
                    current.get("hrv_days", 0), current.get("readiness_days", 0)
                ),
            )
        )


def _add_sport_insights(
    insights: list[Insight],
    current_sports: dict,
    previous_sports: dict,
    baseline_sports: dict,
):
    for sport, summary in current_sports.items():
        previous = previous_sports.get(sport, {})
        baseline = baseline_sports.get(sport, {})
        count = summary.get("count") or 0
        if count == 0:
            continue

        volume_change = _pct_change(summary.get("weekly_duration_hours"), previous.get("weekly_duration_hours"))
        if volume_change is not None and abs(volume_change) >= 25:
            insights.append(
                Insight(
                    sport,
                    "warning" if volume_change > 30 else "notice",
                    f"{sport} volume changed",
                    f"{sport} weekly-equivalent training time is {abs(volume_change):.0f}% {'higher' if volume_change > 0 else 'lower'} than the previous period.",
                    "weekly_duration_hours",
                    summary.get("weekly_duration_hours"),
                    previous.get("weekly_duration_hours"),
                    sample_size=min(count, previous.get("count") or 0),
                )
            )

        pace_change = _diff(summary.get("pace_min_per_km"), baseline.get("pace_min_per_km"))
        hr_change = _diff(summary.get("avg_hr"), baseline.get("avg_hr"))
        if pace_change is not None and hr_change is not None:
            if pace_change <= -0.2 and hr_change <= 2:
                insights.append(
                    Insight(
                        sport,
                        "positive",
                        f"{sport} efficiency improved",
                        f"{sport} pace is faster than baseline without a meaningful increase in average heart rate.",
                        "pace_min_per_km",
                        summary.get("pace_min_per_km"),
                        baseline.get("pace_min_per_km"),
                        sample_size=min(count, baseline.get("count") or 0),
                    )
                )
            elif pace_change >= 0.2 and hr_change >= 3:
                insights.append(
                    Insight(
                        sport,
                        "notice",
                        f"{sport} felt costlier",
                        f"{sport} pace is slower than baseline while average heart rate is higher.",
                        "pace_min_per_km",
                        summary.get("pace_min_per_km"),
                        baseline.get("pace_min_per_km"),
                        sample_size=min(count, baseline.get("count") or 0),
                    )
                )


def _enrich_insight(insight: Insight, current: dict) -> None:
    samples = insight.sample_size
    if samples is None:
        samples = max(
            current.get("daily_days", 0),
            current.get("sleep_nights", 0),
            current.get("activity_count", 0),
        )
        insight.sample_size = samples

    sport_categories = {"Running", "Cycling", "Swimming", "Other", "Training load"}
    high_threshold, medium_threshold = (8, 4) if insight.category in sport_categories else (14, 7)
    insight.confidence = (
        "high" if samples >= high_threshold else "medium" if samples >= medium_threshold else "low"
    )

    if insight.confidence == "low":
        insight.action = "Treat this as an early signal and collect more comparable samples before changing your plan."
    elif insight.severity == "warning":
        insight.action = "Review the underlying trend and keep the next session easy if recovery signals are also unfavorable."
    elif insight.severity == "positive":
        insight.action = "Keep the routine that produced this result consistent and monitor it over the next few weeks."
    else:
        insight.action = "Watch this metric for another week before changing your plan."


def _score(insight: Insight) -> tuple[int, int]:
    severity_rank = {"warning": 0, "notice": 1, "positive": 2}
    category_rank = {
        "Recovery": 0,
        "Training load": 1,
        "Sleep": 2,
        "Running": 3,
        "Cycling": 3,
        "Swimming": 3,
        "Consistency": 4,
    }
    return severity_rank.get(insight.severity, 3), category_rank.get(insight.category, 5)


def generate_insights(
    daily_stats: pd.DataFrame,
    sleep: pd.DataFrame,
    activities: pd.DataFrame,
    body: pd.DataFrame,
    start: date,
    end: date,
) -> dict:
    previous_start, previous_end = _previous_bounds(start, end)
    baseline_start, baseline_end = _baseline_bounds(start)

    current_daily = _window(daily_stats, "date", start, end)
    current_sleep = _window(sleep, "date", start, end)
    current_activities = _activity_window(activities, start, end)

    previous_daily = _window(daily_stats, "date", previous_start, previous_end)
    previous_sleep = _window(sleep, "date", previous_start, previous_end)
    previous_activities = _activity_window(activities, previous_start, previous_end)

    baseline_daily = _window(daily_stats, "date", baseline_start, baseline_end)
    baseline_sleep = _window(sleep, "date", baseline_start, baseline_end)
    baseline_activities = _activity_window(activities, baseline_start, baseline_end)

    current = _metrics(current_daily, current_sleep, current_activities, start, end)
    previous = _metrics(previous_daily, previous_sleep, previous_activities, previous_start, previous_end)
    baseline = _metrics(baseline_daily, baseline_sleep, baseline_activities, baseline_start, baseline_end)

    current_sports = _sport_summaries(current_activities, start, end)
    previous_sports = _sport_summaries(previous_activities, previous_start, previous_end)
    baseline_sports = _sport_summaries(baseline_activities, baseline_start, baseline_end)

    insights: list[Insight] = []
    _add_change_insights(insights, current, previous, baseline)
    _add_combination_insights(insights, current, baseline)
    _add_sport_insights(insights, current_sports, previous_sports, baseline_sports)

    if not insights:
        insights.append(
            Insight(
                "Overview",
                "notice",
                "More history will sharpen insights",
                "There were no major changes against the available comparison windows.",
            )
        )

    insights = sorted(insights, key=_score)
    for insight in insights:
        _enrich_insight(insight, current)

    return {
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "previous_range": {
            "start": previous_start.isoformat(),
            "end": previous_end.isoformat(),
        },
        "baseline_range": {
            "start": baseline_start.isoformat(),
            "end": baseline_end.isoformat(),
        },
        "metrics": {
            "current": current,
            "previous": previous,
            "baseline_90d": baseline,
        },
        "sports": {
            "current": current_sports,
            "previous": previous_sports,
            "baseline_90d": baseline_sports,
        },
        "insights": [insight.as_dict() for insight in insights],
    }




