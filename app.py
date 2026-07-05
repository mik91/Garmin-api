import json
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from garmin_ai import analysis, ai_insights, config
from garmin_ai import sync as sync_module

BLUE = "#2a78d6"
AQUA = "#1baf7a"
YELLOW = "#eda100"
VIOLET = "#4a3aa7"
ROSE = "#c44764"
INK = "#232323"
MUTED = "#898781"
GRID = "#e9e5dc"
ACTIVITY_COLORS = {
    "Running": BLUE,
    "Swimming": AQUA,
    "Cycling": YELLOW,
    "Other": MUTED,
}
PERIODS = {
    "Daily": "D",
    "Weekly": "W-SUN",
    "Monthly": "MS",
    "Yearly": "YS",
}


st.set_page_config(page_title="Garmin Insights", layout="wide")


st.markdown(
    """
    <style>
    .block-container {padding-top: 1.3rem;}
    [data-testid="stMetric"] {
        background: #fbfaf7;
        border: 1px solid #eee8dd;
        border-radius: 8px;
        padding: 0.85rem 0.9rem;
    }
    [data-testid="stMetricLabel"] {color: #57534c;}
    [data-testid="stMetricValue"] {color: #232323;}
    div[data-testid="stDataFrame"] {border: 1px solid #eee8dd; border-radius: 8px;}
    </style>
    """,
    unsafe_allow_html=True,
)


def raw_value(row: pd.Series, key: str, default=None):
    try:
        raw = json.loads(row.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return default
    return raw.get(key, default)


def raw_dict(row: pd.Series) -> dict:
    try:
        return json.loads(row.get("raw_json") or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}


def fmt_number(value, suffix="", decimals=0, empty="-"):
    if value is None or pd.isna(value):
        return empty
    return f"{float(value):,.{decimals}f}{suffix}"


def fmt_duration(seconds, empty="-"):
    if seconds is None or pd.isna(seconds):
        return empty
    seconds = int(seconds)
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    return f"{hours}h {minutes:02d}m" if hours else f"{minutes}m"


def fmt_pace(distance_m, duration_s):
    if not distance_m or pd.isna(distance_m) or not duration_s or pd.isna(duration_s):
        return "-"
    minutes_per_km = (duration_s / 60) / (distance_m / 1000)
    minutes = int(minutes_per_km)
    seconds = int(round((minutes_per_km - minutes) * 60))
    if seconds == 60:
        minutes += 1
        seconds = 0
    return f"{minutes}:{seconds:02d} /km"


def apply_date_filter(df: pd.DataFrame, column: str, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return df[(df[column] >= start_ts) & (df[column] <= end_ts)].copy()


def current_period_bounds(period_name: str, anchor: date) -> tuple[date, date]:
    anchor_ts = pd.Timestamp(anchor)
    if period_name == "Daily":
        return anchor, anchor
    if period_name == "Weekly":
        period = anchor_ts.to_period("W-SUN")
    elif period_name == "Monthly":
        period = anchor_ts.to_period("M")
    else:
        period = anchor_ts.to_period("Y")
    return period.start_time.date(), period.end_time.date()


def period_start(series: pd.Series, period_name: str) -> pd.Series:
    if period_name == "Daily":
        return series.dt.floor("D")
    if period_name == "Weekly":
        return series.dt.to_period("W-SUN").dt.start_time
    if period_name == "Monthly":
        return series.dt.to_period("M").dt.start_time
    return series.dt.to_period("Y").dt.start_time


def aggregate_daily(df: pd.DataFrame, period_name: str) -> pd.DataFrame:
    if df.empty:
        return df
    grouped = (
        df.assign(period=period_start(df["date"], period_name))
        .groupby("period", as_index=False)
        .agg(
            steps=("steps", "sum"),
            calories=("calories", "sum"),
            resting_hr=("resting_hr", "mean"),
            stress_avg=("stress_avg", "mean"),
            body_battery_high=("body_battery_high", "mean"),
            body_battery_low=("body_battery_low", "mean"),
            days=("date", "nunique"),
        )
    )
    return grouped


def aggregate_sleep(df: pd.DataFrame, period_name: str) -> pd.DataFrame:
    if df.empty:
        return df
    sleep = df.copy()
    for column in ["total_sleep_s", "deep_s", "light_s", "rem_s", "awake_s"]:
        sleep[f"{column}_h"] = sleep[column] / 3600
    return (
        sleep.assign(period=period_start(sleep["date"], period_name))
        .groupby("period", as_index=False)
        .agg(
            total_sleep_h=("total_sleep_s_h", "mean"),
            deep_h=("deep_s_h", "mean"),
            light_h=("light_s_h", "mean"),
            rem_h=("rem_s_h", "mean"),
            awake_h=("awake_s_h", "mean"),
            sleep_score=("sleep_score", "mean"),
            nights=("date", "nunique"),
        )
    )


def aggregate_activities(df: pd.DataFrame, period_name: str) -> pd.DataFrame:
    if df.empty:
        return df
    return (
        df.assign(period=period_start(df["start_time"], period_name))
        .groupby(["period", "category"], as_index=False)
        .agg(
            count=("activity_id", "count"),
            distance_m=("distance_m", "sum"),
            duration_s=("duration_s", "sum"),
            calories=("calories", "sum"),
            elevation_gain_m=("elevation_gain_m", "sum"),
            avg_hr=("avg_hr", "mean"),
        )
    )


def style_figure(fig: go.Figure, yaxis_title: str | None = None) -> go.Figure:
    fig.update_layout(
        margin=dict(l=10, r=10, t=26, b=10),
        height=340,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend_title_text="",
        hovermode="x unified",
        font=dict(color=INK),
    )
    fig.update_xaxes(showgrid=False)
    fig.update_yaxes(showgrid=True, gridcolor=GRID, zeroline=False, title=yaxis_title)
    return fig


def line_chart(df: pd.DataFrame, x: str, y: str, name: str, color: str, yaxis_title: str):
    fig = go.Figure()
    fig.add_trace(
        go.Scatter(
            x=df[x],
            y=df[y],
            name=name,
            mode="lines+markers",
            line=dict(color=color, width=2.5),
            marker=dict(size=7),
        )
    )
    return style_figure(fig, yaxis_title)


def activity_details_df(activities: pd.DataFrame) -> pd.DataFrame:
    if activities.empty:
        return activities
    df = activities.copy()
    df["name"] = df.apply(lambda row: raw_value(row, "activityName", row["type"]), axis=1)
    df["location"] = df.apply(lambda row: raw_value(row, "locationName", "-"), axis=1)
    df["moving_duration_s"] = df.apply(lambda row: raw_value(row, "movingDuration", row["duration_s"]), axis=1)
    df["training_load"] = df.apply(lambda row: raw_value(row, "activityTrainingLoad"), axis=1)
    df["aerobic_te"] = df.apply(lambda row: raw_value(row, "aerobicTrainingEffect"), axis=1)
    df["anaerobic_te"] = df.apply(lambda row: raw_value(row, "anaerobicTrainingEffect"), axis=1)
    df["avg_power"] = df.apply(lambda row: raw_value(row, "avgPower"), axis=1)
    df["pace"] = df.apply(lambda row: fmt_pace(row["distance_m"], row["moving_duration_s"]), axis=1)
    df["distance_km"] = df["distance_m"] / 1000
    df["duration"] = df["duration_s"].apply(fmt_duration)
    return df


def clean_value(value):
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def build_current_view_summary(
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    activities: pd.DataFrame,
    body: pd.DataFrame,
    start: date,
    end: date,
    group_by: str,
) -> dict:
    summary = {
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "chart_grouping": group_by,
    }

    if not daily.empty:
        summary["daily_stats"] = {
            "days": int(daily["date"].nunique()),
            "total_steps": clean_value(daily["steps"].sum(skipna=True)),
            "avg_steps_per_day": clean_value(daily["steps"].mean(skipna=True)),
            "avg_resting_hr": clean_value(daily["resting_hr"].mean(skipna=True)),
            "avg_stress": clean_value(daily["stress_avg"].mean(skipna=True)),
            "avg_body_battery_high": clean_value(daily["body_battery_high"].mean(skipna=True)),
            "avg_body_battery_low": clean_value(daily["body_battery_low"].mean(skipna=True)),
            "total_calories": clean_value(daily["calories"].sum(skipna=True)),
        }

    if not sleep.empty:
        summary["sleep"] = {
            "nights": int(sleep["date"].nunique()),
            "avg_total_sleep_hours": clean_value(sleep["total_sleep_s"].mean(skipna=True) / 3600),
            "avg_deep_hours": clean_value(sleep["deep_s"].mean(skipna=True) / 3600),
            "avg_light_hours": clean_value(sleep["light_s"].mean(skipna=True) / 3600),
            "avg_rem_hours": clean_value(sleep["rem_s"].mean(skipna=True) / 3600),
            "avg_awake_hours": clean_value(sleep["awake_s"].mean(skipna=True) / 3600),
            "avg_sleep_score": clean_value(sleep["sleep_score"].mean(skipna=True)),
        }

    if not activities.empty:
        activity_summary = {}
        for category, category_df in activities.groupby("category"):
            activity_summary[category] = {
                "count": int(len(category_df)),
                "distance_km": clean_value(category_df["distance_m"].sum(skipna=True) / 1000),
                "duration_hours": clean_value(category_df["duration_s"].sum(skipna=True) / 3600),
                "calories": clean_value(category_df["calories"].sum(skipna=True)),
                "elevation_gain_m": clean_value(category_df["elevation_gain_m"].sum(skipna=True)),
                "avg_hr": clean_value(category_df["avg_hr"].mean(skipna=True)),
                "max_hr": clean_value(category_df["max_hr"].max(skipna=True)),
            }
        summary["activities_by_type"] = activity_summary

    if not body.empty:
        summary["body_composition"] = {
            "entries": int(len(body)),
            "latest_weight_kg": clean_value(body["weight_kg"].dropna().iloc[-1])
            if body["weight_kg"].notna().any()
            else None,
            "latest_body_fat_pct": clean_value(body["body_fat_pct"].dropna().iloc[-1])
            if body["body_fat_pct"].notna().any()
            else None,
        }

    return summary


daily_stats = analysis.load_daily_stats()
sleep_df = analysis.load_sleep()
activities_df = analysis.ensure_activity_categories(analysis.load_activities())
body_df = analysis.load_body_composition()

all_dates = []
if not daily_stats.empty:
    all_dates.extend(daily_stats["date"].dt.date.tolist())
if not sleep_df.empty:
    all_dates.extend(sleep_df["date"].dt.date.tolist())
if not activities_df.empty:
    all_dates.extend(activities_df["start_time"].dt.date.tolist())

min_date = min(all_dates) if all_dates else date.today()
max_date = max(all_dates) if all_dates else date.today()

st.sidebar.header("Garmin Insights")
sync_days = st.sidebar.number_input("Days to sync", min_value=1, max_value=365, value=7)
if st.sidebar.button("Sync now", use_container_width=True):
    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        st.sidebar.error("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env first.")
    else:
        with st.spinner("Syncing with Garmin Connect..."):
            try:
                start, end = sync_module.sync(days=sync_days)
                st.sidebar.success(f"Synced {sync_days} days: {start} to {end}.")
                st.rerun()
            except Exception as e:
                st.sidebar.error(f"Sync failed: {e}")

st.sidebar.divider()
st.sidebar.subheader("View")
date_range = st.sidebar.date_input(
    "Date range",
    value=(min_date, max_date),
    min_value=min_date,
    max_value=max_date,
)
if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range

period = st.sidebar.segmented_control("Group charts by", list(PERIODS), default="Weekly")
show_other = st.sidebar.checkbox("Include other activities", value=True)

daily_filtered = apply_date_filter(daily_stats, "date", start_date, end_date)
sleep_filtered = apply_date_filter(sleep_df, "date", start_date, end_date)
activities_filtered = apply_date_filter(activities_df, "start_time", start_date, end_date)
if not show_other and not activities_filtered.empty:
    activities_filtered = activities_filtered[activities_filtered["category"] != "Other"]
body_filtered = apply_date_filter(body_df, "date", start_date, end_date)

overview_start_date, overview_end_date = current_period_bounds(period, end_date)
overview_daily = apply_date_filter(daily_stats, "date", overview_start_date, overview_end_date)
overview_sleep = apply_date_filter(sleep_df, "date", overview_start_date, overview_end_date)
overview_activities = apply_date_filter(
    activities_df, "start_time", overview_start_date, overview_end_date
)
if not show_other and not overview_activities.empty:
    overview_activities = overview_activities[overview_activities["category"] != "Other"]

daily_period = aggregate_daily(daily_filtered, period)
sleep_period = aggregate_sleep(sleep_filtered, period)
activity_period = aggregate_activities(activities_filtered, period)
activity_details = activity_details_df(activities_filtered)
overview_daily_period = aggregate_daily(overview_daily, "Daily")

st.title("Garmin Insights")
st.caption(f"{start_date:%b %d, %Y} to {end_date:%b %d, %Y} | grouped {period.lower()}")

tab_overview, tab_trends, tab_activities, tab_ai = st.tabs(
    ["Overview", "Trends", "Activities", "AI Insights"]
)

with tab_overview:
    if daily_stats.empty and activities_df.empty and sleep_df.empty:
        st.info("No data yet. Use 'Sync now' in the sidebar to pull data from Garmin Connect.")
    else:
        st.caption(
            f"Overview period: {overview_start_date:%b %d, %Y} to "
            f"{overview_end_date:%b %d, %Y}"
        )

        total_steps = overview_daily["steps"].sum(skipna=True) if not overview_daily.empty else None
        avg_steps = overview_daily["steps"].mean(skipna=True) if not overview_daily.empty else None
        avg_sleep_h = (
            overview_sleep["total_sleep_s"].mean(skipna=True) / 3600
            if not overview_sleep.empty
            else None
        )
        avg_resting_hr = (
            overview_daily["resting_hr"].mean(skipna=True) if not overview_daily.empty else None
        )
        activity_count = len(overview_activities)
        distance_km = (
            overview_activities["distance_m"].sum(skipna=True) / 1000
            if not overview_activities.empty
            else None
        )
        duration_h = (
            overview_activities["duration_s"].sum(skipna=True) / 3600
            if not overview_activities.empty
            else None
        )

        cols = st.columns(6)
        cols[0].metric("Total steps", fmt_number(total_steps))
        cols[1].metric("Avg steps/day", fmt_number(avg_steps))
        cols[2].metric("Avg sleep", fmt_number(avg_sleep_h, " h", 1))
        cols[3].metric("Resting HR", fmt_number(avg_resting_hr, " bpm"))
        cols[4].metric("Activities", f"{activity_count}")
        cols[5].metric("Distance", fmt_number(distance_km, " km", 1))

        cols = st.columns([1.05, 1])
        with cols[0]:
            st.subheader("Steps and resting HR")
            if overview_daily_period.empty:
                st.info("No daily stats in this overview period.")
            else:
                fig = go.Figure()
                fig.add_trace(
                    go.Bar(
                        x=overview_daily_period["period"],
                        y=overview_daily_period["steps"],
                        name="Steps",
                        marker_color=BLUE,
                        yaxis="y",
                    )
                )
                fig.add_trace(
                    go.Scatter(
                        x=overview_daily_period["period"],
                        y=overview_daily_period["resting_hr"],
                        name="Resting HR",
                        mode="lines+markers",
                        line=dict(color=ROSE, width=2.5),
                        yaxis="y2",
                    )
                )
                fig.update_layout(
                    yaxis=dict(title="steps", gridcolor=GRID, zeroline=False),
                    yaxis2=dict(title="bpm", overlaying="y", side="right", showgrid=False),
                )
                st.plotly_chart(style_figure(fig), use_container_width=True)

        with cols[1]:
            st.subheader("Activity mix")
            if overview_activities.empty:
                st.info("No activities in this overview period.")
            else:
                mix = (
                    overview_activities.groupby("category", as_index=False)
                    .agg(distance_m=("distance_m", "sum"), count=("activity_id", "count"))
                    .sort_values("distance_m", ascending=False)
                )
                fig = go.Figure(
                    go.Pie(
                        labels=mix["category"],
                        values=mix["distance_m"],
                        hole=0.55,
                        marker=dict(
                            colors=[ACTIVITY_COLORS.get(c, MUTED) for c in mix["category"]]
                        ),
                    )
                )
                fig.update_traces(textinfo="label+percent", hovertemplate="%{label}<br>%{value:.0f} m<extra></extra>")
                st.plotly_chart(style_figure(fig), use_container_width=True)

        cols = st.columns(3)
        cols[0].metric("Training time", fmt_number(duration_h, " h", 1))
        if not overview_activities.empty:
            cols[1].metric(
                "Elevation gain",
                fmt_number(overview_activities["elevation_gain_m"].sum(skipna=True), " m"),
            )
            cols[2].metric(
                "Avg workout HR",
                fmt_number(overview_activities["avg_hr"].mean(skipna=True), " bpm"),
            )
        else:
            cols[1].metric("Elevation gain", "-")
            cols[2].metric("Avg workout HR", "-")

with tab_trends:
    if daily_period.empty and sleep_period.empty:
        st.info("No trend data in this date range.")
    else:
        cols = st.columns(2)
        with cols[0]:
            st.subheader("Steps")
            if not daily_period.empty:
                fig = go.Figure(
                    go.Bar(x=daily_period["period"], y=daily_period["steps"], marker_color=BLUE)
                )
                st.plotly_chart(style_figure(fig, "steps"), use_container_width=True)
        with cols[1]:
            st.subheader("Resting heart rate")
            if not daily_period.empty:
                fig = line_chart(daily_period, "period", "resting_hr", "Resting HR", ROSE, "bpm")
                st.plotly_chart(fig, use_container_width=True)

        cols = st.columns(2)
        with cols[0]:
            st.subheader("Stress")
            if not daily_period.empty and daily_period["stress_avg"].notna().any():
                fig = line_chart(daily_period, "period", "stress_avg", "Stress", VIOLET, "avg stress")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No stress values for this range.")
        with cols[1]:
            st.subheader("Sleep score")
            if not sleep_period.empty and sleep_period["sleep_score"].notna().any():
                fig = line_chart(sleep_period, "period", "sleep_score", "Sleep score", AQUA, "score")
                st.plotly_chart(fig, use_container_width=True)
            else:
                st.info("No sleep score values for this range.")

        st.subheader("Sleep stages")
        if not sleep_period.empty:
            fig = go.Figure()
            for column, name, color in [
                ("deep_h", "Deep", BLUE),
                ("light_h", "Light", AQUA),
                ("rem_h", "REM", YELLOW),
                ("awake_h", "Awake", VIOLET),
            ]:
                fig.add_trace(
                    go.Bar(
                        x=sleep_period["period"],
                        y=sleep_period[column],
                        name=name,
                        marker_color=color,
                    )
                )
            fig.update_layout(barmode="stack")
            st.plotly_chart(style_figure(fig, "avg hours/night"), use_container_width=True)

        if not body_filtered.empty:
            st.subheader("Body composition")
            fig = go.Figure()
            fig.add_trace(
                go.Scatter(
                    x=body_filtered["date"],
                    y=body_filtered["weight_kg"],
                    name="Weight",
                    mode="lines+markers",
                    line=dict(color=INK, width=2.5),
                )
            )
            if body_filtered["body_fat_pct"].notna().any():
                fig.add_trace(
                    go.Scatter(
                        x=body_filtered["date"],
                        y=body_filtered["body_fat_pct"],
                        name="Body fat %",
                        mode="lines+markers",
                        line=dict(color=YELLOW, width=2.5),
                        yaxis="y2",
                    )
                )
                fig.update_layout(
                    yaxis2=dict(title="body fat %", overlaying="y", side="right", showgrid=False)
                )
            st.plotly_chart(style_figure(fig, "kg"), use_container_width=True)

with tab_activities:
    if activities_filtered.empty:
        st.info("No activities match the current filters.")
    else:
        category_options = list(analysis.ACTIVITY_CATEGORIES)
        existing = [c for c in category_options if c in activities_filtered["category"].unique()]
        selected_categories = st.multiselect(
            "Activity types",
            options=category_options,
            default=existing,
        )
        activity_view = activities_filtered[activities_filtered["category"].isin(selected_categories)]
        activity_details = activity_details_df(activity_view)
        activity_period = aggregate_activities(activity_view, period)

        if activity_view.empty:
            st.info("No activities match the selected types.")
        else:
            summary = (
                activity_view.groupby("category", as_index=False)
                .agg(
                    count=("activity_id", "count"),
                    distance_m=("distance_m", "sum"),
                    duration_s=("duration_s", "sum"),
                    calories=("calories", "sum"),
                    elevation_gain_m=("elevation_gain_m", "sum"),
                    avg_hr=("avg_hr", "mean"),
                )
                .sort_values("distance_m", ascending=False)
            )
            metric_cols = st.columns(min(4, max(1, len(summary))))
            for idx, (_, row) in enumerate(summary.iterrows()):
                col = metric_cols[idx % len(metric_cols)]
                col.metric(
                    row["category"],
                    fmt_number(row["distance_m"] / 1000, " km", 1),
                    f"{int(row['count'])} activities | {fmt_duration(row['duration_s'])}",
                )

            cols = st.columns(2)
            with cols[0]:
                st.subheader(f"Distance by {period.lower()}")
                fig = go.Figure()
                for category in selected_categories:
                    category_period = activity_period[activity_period["category"] == category]
                    if category_period.empty:
                        continue
                    fig.add_trace(
                        go.Bar(
                            x=category_period["period"],
                            y=category_period["distance_m"] / 1000,
                            name=category,
                            marker_color=ACTIVITY_COLORS.get(category, MUTED),
                        )
                    )
                fig.update_layout(barmode="group")
                st.plotly_chart(style_figure(fig, "km"), use_container_width=True)

            with cols[1]:
                st.subheader(f"Time by {period.lower()}")
                fig = go.Figure()
                for category in selected_categories:
                    category_period = activity_period[activity_period["category"] == category]
                    if category_period.empty:
                        continue
                    fig.add_trace(
                        go.Bar(
                            x=category_period["period"],
                            y=category_period["duration_s"] / 3600,
                            name=category,
                            marker_color=ACTIVITY_COLORS.get(category, MUTED),
                        )
                    )
                fig.update_layout(barmode="group")
                st.plotly_chart(style_figure(fig, "hours"), use_container_width=True)

            st.subheader("Activity history")
            table_cols = [
                "start_time",
                "name",
                "category",
                "location",
                "distance_km",
                "duration",
                "pace",
                "calories",
                "avg_hr",
                "max_hr",
                "elevation_gain_m",
                "training_load",
            ]
            st.dataframe(
                activity_details[table_cols].sort_values("start_time", ascending=False),
                use_container_width=True,
                hide_index=True,
                column_config={
                    "start_time": st.column_config.DatetimeColumn("Start"),
                    "name": "Activity",
                    "distance_km": st.column_config.NumberColumn("Distance km", format="%.2f"),
                    "avg_hr": st.column_config.NumberColumn("Avg HR", format="%.0f"),
                    "max_hr": st.column_config.NumberColumn("Max HR", format="%.0f"),
                    "elevation_gain_m": st.column_config.NumberColumn("Gain m", format="%.0f"),
                    "training_load": st.column_config.NumberColumn("Load", format="%.0f"),
                },
            )

            st.subheader("Activity details")
            options = activity_details.sort_values("start_time", ascending=False)
            selected_id = st.selectbox(
                "Select an activity",
                options=options["activity_id"].tolist(),
                format_func=lambda activity_id: options.loc[
                    options["activity_id"] == activity_id, "name"
                ].iloc[0],
            )
            selected = options[options["activity_id"] == selected_id].iloc[0]
            raw = raw_dict(selected)

            st.markdown(f"#### {selected['name']}")
            st.caption(
                f"{selected['start_time']:%A, %b %d, %Y at %I:%M %p} | "
                f"{selected['category']} | {selected['location']}"
            )
            cols = st.columns(6)
            cols[0].metric("Distance", fmt_number(selected["distance_km"], " km", 2))
            cols[1].metric("Moving time", fmt_duration(selected["moving_duration_s"]))
            cols[2].metric("Pace", selected["pace"])
            cols[3].metric("Calories", fmt_number(selected["calories"]))
            cols[4].metric("Avg HR", fmt_number(selected["avg_hr"], " bpm"))
            cols[5].metric("Max HR", fmt_number(selected["max_hr"], " bpm"))

            cols = st.columns(5)
            cols[0].metric("Elevation gain", fmt_number(selected["elevation_gain_m"], " m"))
            cols[1].metric("Training load", fmt_number(selected["training_load"]))
            cols[2].metric("Aerobic TE", fmt_number(selected["aerobic_te"], decimals=1))
            cols[3].metric("Anaerobic TE", fmt_number(selected["anaerobic_te"], decimals=1))
            cols[4].metric("Avg power", fmt_number(selected["avg_power"], " W"))

            zone_cols = [f"hrTimeInZone_{i}" for i in range(1, 6)]
            if any(raw.get(column) for column in zone_cols):
                st.markdown("##### Heart rate zones")
                zone_values = [float(raw.get(column) or 0) / 60 for column in zone_cols]
                fig = go.Figure(
                    go.Bar(
                        x=[f"Z{i}" for i in range(1, 6)],
                        y=zone_values,
                        marker_color=[AQUA, BLUE, YELLOW, ROSE, VIOLET],
                    )
                )
                st.plotly_chart(style_figure(fig, "minutes"), use_container_width=True)

            split_summaries = raw.get("splitSummaries") or []
            if split_summaries:
                st.markdown("##### Split summaries")
                split_df = pd.DataFrame(split_summaries)
                visible = [
                    column
                    for column in [
                        "splitType",
                        "noOfSplits",
                        "distance",
                        "duration",
                        "averageSpeed",
                        "maxSpeed",
                        "totalAscent",
                        "elevationLoss",
                    ]
                    if column in split_df.columns
                ]
                st.dataframe(split_df[visible], use_container_width=True, hide_index=True)

            with st.expander("Raw Garmin activity payload"):
                st.json(raw)

with tab_ai:
    if daily_stats.empty:
        st.info("No data yet. Sync first.")
    else:
        try:
            ai_ready = ai_insights.is_configured()
            provider = ai_insights.provider_name()
            model = ai_insights.configured_model()
            provider_error = None
        except ai_insights.AIProviderError as e:
            ai_ready = False
            provider = config.AI_PROVIDER
            model = "-"
            provider_error = str(e)

        if provider_error:
            st.error(provider_error)
        elif not ai_ready:
            st.warning(ai_insights.missing_config_message())

        summary = build_current_view_summary(
            daily_filtered,
            sleep_filtered,
            activities_filtered,
            body_filtered,
            start_date,
            end_date,
            period,
        )
        summary_json = json.dumps(summary, indent=2, default=str)
        summary_key = f"{provider}:{model}:{summary_json}"

        if "ai_chat_messages" not in st.session_state:
            st.session_state.ai_chat_messages = []
        if st.session_state.get("ai_chat_summary_key") != summary_key:
            st.session_state.ai_chat_messages = []
            st.session_state.ai_chat_summary_key = summary_key

        st.caption(f"Provider: {provider} / Model: {model}")
        with st.expander("Aggregated summary that will be sent"):
            st.json(summary)

        cols = st.columns([1, 1, 5])
        if cols[0].button("Start with insights", disabled=not ai_ready, use_container_width=True):
            st.session_state.ai_chat_messages = [
                {
                    "role": "user",
                    "content": "Give me a concise summary of this dashboard view.",
                }
            ]
            with st.spinner(f"Asking {provider} for a summary..."):
                try:
                    answer = ai_insights.chat(summary, st.session_state.ai_chat_messages)
                except Exception as e:
                    answer = f"I couldn't get an AI response for that summary: {e}"
            st.session_state.ai_chat_messages.append({"role": "assistant", "content": answer})
            st.rerun()
        if cols[1].button("Clear chat", use_container_width=True):
            st.session_state.ai_chat_messages = []
            st.rerun()

        st.divider()
        st.subheader("Chat about your Garmin data")

        if not st.session_state.ai_chat_messages:
            st.info("Ask a question below, or start with an AI summary of the current dashboard view.")

        for message in st.session_state.ai_chat_messages:
            with st.chat_message(message["role"]):
                st.markdown(message["content"])

        with st.form("ai_chat_form", clear_on_submit=True):
            prompt = st.text_area(
                "Question",
                placeholder="Ask a follow-up about training, sleep, recovery, or trends",
                label_visibility="collapsed",
                disabled=not ai_ready,
                height=90,
            )
            submitted = st.form_submit_button("Send", disabled=not ai_ready)

        prompt = prompt.strip() if prompt else ""
        if submitted and prompt:
            st.session_state.ai_chat_messages.append({"role": "user", "content": prompt})
            with st.spinner("Thinking..."):
                try:
                    answer = ai_insights.chat(summary, st.session_state.ai_chat_messages)
                except Exception as e:
                    answer = f"I couldn't get an AI response for that question: {e}"
            st.session_state.ai_chat_messages.append({"role": "assistant", "content": answer})
            st.rerun()
