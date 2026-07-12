import pandas as pd

from garmin_ai import db

ACTIVITY_CATEGORIES = ("Running", "Swimming", "Cycling", "Other")


def activity_category(activity_type: object) -> str:
    type_text = str(activity_type or "").lower()
    if any(term in type_text for term in ("run", "jog", "treadmill")):
        return "Running"
    if any(term in type_text for term in ("cycl", "bike", "biking", "mtb", "ebike")):
        return "Cycling"
    if any(term in type_text for term in ("swim", "pool", "open_water")):
        return "Swimming"
    return "Other"


def ensure_activity_categories(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "category" in df.columns:
        return df
    df = df.copy()
    df["category"] = df["type"].apply(activity_category)
    return df


def load_daily_stats() -> pd.DataFrame:
    with db.get_connection() as conn:
        df = pd.read_sql_query("SELECT * FROM daily_stats ORDER BY date", conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_sleep() -> pd.DataFrame:
    with db.get_connection() as conn:
        df = pd.read_sql_query("SELECT * FROM sleep ORDER BY date", conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_activities() -> pd.DataFrame:
    with db.get_connection() as conn:
        df = pd.read_sql_query("SELECT * FROM activities ORDER BY start_time", conn)
    if not df.empty:
        df["start_time"] = pd.to_datetime(df["start_time"])
        df = ensure_activity_categories(df)
    return df


def load_body_composition() -> pd.DataFrame:
    with db.get_connection() as conn:
        df = pd.read_sql_query("SELECT * FROM body_composition ORDER BY date", conn)
    if not df.empty:
        df["date"] = pd.to_datetime(df["date"])
    return df


def load_sync_state() -> dict:
    with db.get_connection() as conn:
        return db.get_sync_state(conn)


def add_rolling_averages(df: pd.DataFrame, column: str, windows=(7, 30)) -> pd.DataFrame:
    df = df.copy()
    for w in windows:
        df[f"{column}_roll{w}"] = df[column].rolling(window=w, min_periods=1).mean()
    return df


def period_over_period_delta(df: pd.DataFrame, column: str, period_days: int) -> float | None:
    """Compare the mean of the most recent `period_days` window vs. the one before it."""
    if df.empty or column not in df:
        return None
    df = df.dropna(subset=[column]).sort_values("date")
    if len(df) < 2:
        return None
    cutoff = df["date"].max() - pd.Timedelta(days=period_days)
    prev_cutoff = cutoff - pd.Timedelta(days=period_days)
    recent = df[df["date"] > cutoff][column]
    previous = df[(df["date"] <= cutoff) & (df["date"] > prev_cutoff)][column]
    if recent.empty or previous.empty:
        return None
    return float(recent.mean() - previous.mean())


def flag_anomalies(df: pd.DataFrame, column: str, z_threshold: float = 2.0) -> pd.DataFrame:
    df = df.copy()
    mean = df[column].mean()
    std = df[column].std()
    if not std or pd.isna(std):
        df["is_anomaly"] = False
        return df
    df["z_score"] = (df[column] - mean) / std
    df["is_anomaly"] = df["z_score"].abs() >= z_threshold
    return df


def summarize_activities_by_category(
    activities: pd.DataFrame, recent_days: int = 30
) -> dict:
    if activities.empty:
        return {}

    df = ensure_activity_categories(activities).copy()
    df = df.dropna(subset=["start_time"])
    if df.empty:
        return {}

    recent_cutoff = df["start_time"].max() - pd.Timedelta(days=recent_days)
    recent = df[df["start_time"] > recent_cutoff]
    summary = {}

    for category in ACTIVITY_CATEGORIES:
        category_df = recent[recent["category"] == category]
        if category_df.empty:
            continue

        distance_m = category_df["distance_m"].sum(skipna=True)
        duration_s = category_df["duration_s"].sum(skipna=True)
        avg_hr = category_df["avg_hr"].dropna()
        summary[category.lower()] = {
            "count": int(len(category_df)),
            "distance_km": round(float(distance_m or 0) / 1000, 2),
            "duration_hours": round(float(duration_s or 0) / 3600, 2),
            "avg_hr": round(float(avg_hr.mean()), 1) if not avg_hr.empty else None,
        }

    return summary


def build_summary() -> dict:
    """Aggregate recent trend numbers into a compact dict suitable for AI insights."""
    stats = load_daily_stats()
    sleep = load_sleep()
    activities = load_activities()

    summary = {}

    if not stats.empty:
        stats = add_rolling_averages(stats, "resting_hr")
        stats = add_rolling_averages(stats, "steps")
        stats = add_rolling_averages(stats, "stress_avg")
        summary["latest_resting_hr"] = stats["resting_hr"].dropna().iloc[-1] if stats["resting_hr"].notna().any() else None
        summary["resting_hr_7d_avg"] = stats["resting_hr_roll7"].iloc[-1]
        summary["resting_hr_30d_avg"] = stats["resting_hr_roll30"].iloc[-1]
        summary["resting_hr_wow_delta"] = period_over_period_delta(stats, "resting_hr", 7)
        summary["steps_7d_avg"] = stats["steps_roll7"].iloc[-1]
        summary["stress_7d_avg"] = stats["stress_avg_roll7"].iloc[-1]

    if not sleep.empty:
        sleep = add_rolling_averages(sleep, "total_sleep_s")
        summary["sleep_7d_avg_hours"] = (sleep["total_sleep_s_roll7"].iloc[-1] or 0) / 3600
        summary["sleep_30d_avg_hours"] = (sleep["total_sleep_s_roll30"].iloc[-1] or 0) / 3600
        summary["sleep_score_latest"] = sleep["sleep_score"].dropna().iloc[-1] if sleep["sleep_score"].notna().any() else None

    activity_summary = summarize_activities_by_category(activities)
    if activity_summary:
        summary["activities_last_30_days_by_type"] = activity_summary

    return summary
