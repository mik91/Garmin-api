import json
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from garmin_ai import analysis, ai_insights, config, insights_engine
from garmin_ai import sync as sync_module

BLUE = "#60a5fa"
AQUA = "#34d399"
YELLOW = "#fbbf24"
VIOLET = "#a78bfa"
ROSE = "#fb7185"
INK = "#eaf2ff"
MUTED = "#a9b8cc"
GRID = "#2a3951"
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
DATE_PRESETS = (
    "Last 7 days",
    "Last 30 days",
    "This week",
    "This month",
    "This year",
    "All data",
    "Custom",
)


st.set_page_config(page_title="Garmin Insights", page_icon="🏃", layout="wide")


st.markdown(
    """
    <style>
    :root {
        --canvas: #09111f;
        --surface: #111c2e;
        --surface-raised: #162238;
        --border: #2a3951;
        --text: #eaf2ff;
        --muted: #a9b8cc;
        --accent: #60a5fa;
        --radius: 14px;
    }

    html, body, .stApp {
        font-family: "Segoe UI Variable", "Segoe UI", system-ui, -apple-system, BlinkMacSystemFont, sans-serif;
    }
    html {font-size: 16px;}
    .stApp {
        background:
            radial-gradient(circle at 78% -10%, rgba(37, 99, 235, 0.10), transparent 32rem),
            var(--canvas);
        color: var(--text);
    }
    .block-container {
        max-width: 1540px;
        padding: 2.4rem 2.6rem 5rem;
    }
    h1, h2, h3, h4, h5, h6 {
        color: var(--text);
        font-weight: 700;
        letter-spacing: -0.025em;
        line-height: 1.2;
    }
    h1 {font-size: clamp(2rem, 3vw, 2.8rem) !important; margin-bottom: 0.35rem !important;}
    h2 {font-size: 1.65rem !important; margin-top: 1.5rem !important;}
    h3 {font-size: 1.3rem !important; margin-top: 1.2rem !important;}
    p, li {line-height: 1.6;}
    [data-testid="stCaptionContainer"] {color: var(--muted); font-size: 0.9rem; line-height: 1.5;}

    .app-brand {
        display: flex;
        align-items: center;
        gap: 0.8rem;
        margin-bottom: 1.6rem;
    }
    .app-brand__mark {
        display: grid;
        width: 2.4rem;
        height: 2.4rem;
        place-items: center;
        border-radius: 11px;
        background: linear-gradient(145deg, #60a5fa, #2563eb);
        box-shadow: 0 8px 20px rgba(37, 99, 235, 0.25);
        font-size: 1.15rem;
    }
    .app-brand__name {font-size: 1.05rem; font-weight: 750; line-height: 1.2;}
    .app-brand__tagline {color: var(--muted); font-size: 0.78rem; margin-top: 0.15rem;}
    .page-kicker {
        color: var(--accent);
        font-size: 0.75rem;
        font-weight: 750;
        letter-spacing: 0.12em;
        margin-bottom: 0.5rem;
        text-transform: uppercase;
    }
    .page-title {font-size: clamp(2rem, 3vw, 2.8rem); font-weight: 750; letter-spacing: -0.035em; line-height: 1.1;}
    .page-subtitle {color: var(--muted); font-size: 0.95rem; line-height: 1.6; margin: 0.65rem 0 1.25rem;}

    section[data-testid="stSidebar"] {
        width: 330px !important;
        background: rgba(15, 24, 40, 0.98);
        border-right: 1px solid var(--border);
    }
    section[data-testid="stSidebar"] > div {width: 330px !important;}
    section[data-testid="stSidebar"] [data-testid="stSidebarContent"] {padding: 1.8rem 1.1rem 2.5rem;}
    section[data-testid="stSidebar"] h3 {
        font-size: 1rem !important;
        letter-spacing: 0.01em;
        margin-top: 1rem !important;
    }
    section[data-testid="stSidebar"] hr {margin: 1.8rem 0; border-color: var(--border);}

    [data-testid="stHorizontalBlock"] {gap: 1rem; flex-wrap: wrap;}
    [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {
        flex: 1 1 220px !important;
        min-width: 220px;
    }
    [data-testid="stMetric"] {
        min-height: 132px;
        background: linear-gradient(145deg, rgba(22, 34, 56, 0.96), rgba(15, 25, 42, 0.96));
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 1rem 1.05rem;
        box-shadow: 0 10px 28px rgba(2, 8, 23, 0.16);
    }
    [data-testid="stMetricLabel"] {color: var(--muted); font-size: 0.9rem; font-weight: 600;}
    [data-testid="stMetricValue"] {color: var(--text); font-size: clamp(1.65rem, 2.1vw, 2.15rem); line-height: 1.2;}
    [data-testid="stMetricDelta"] {font-size: 0.82rem; font-weight: 650;}

    [data-testid="stTabs"] [data-baseweb="tab-list"] {
        gap: 0.35rem;
        border-bottom: 1px solid var(--border);
        margin-bottom: 1rem;
    }
    [data-testid="stTabs"] button[role="tab"] {
        min-height: 48px;
        padding: 0.7rem 1rem;
        color: var(--muted);
        font-size: 0.95rem;
        font-weight: 650;
    }
    [data-testid="stTabs"] button[role="tab"][aria-selected="true"] {color: var(--text);}

    .stButton > button, .stDownloadButton > button {
        min-height: 44px;
        border: 1px solid #3b4d68;
        border-radius: 10px;
        font-size: 0.92rem;
        font-weight: 700;
        transition: border-color 120ms ease, background 120ms ease, transform 120ms ease;
    }
    .stButton > button:hover, .stDownloadButton > button:hover {
        border-color: var(--accent);
        background: rgba(96, 165, 250, 0.1);
        transform: translateY(-1px);
    }
    [data-baseweb="select"] > div,
    [data-testid="stNumberInput"] input,
    [data-testid="stTextInput"] input,
    [data-testid="stDateInput"] input,
    [data-testid="stChatInput"] textarea {
        min-height: 44px;
        border-color: var(--border) !important;
        border-radius: 10px !important;
        background: #0d1728 !important;
        font-size: 0.94rem;
    }
    [data-testid="stWidgetLabel"] p {font-size: 0.88rem; font-weight: 650; color: #d8e4f3;}

    [data-testid="stAlert"] {
        border: 1px solid rgba(148, 163, 184, 0.25);
        border-radius: 12px;
        padding: 0.85rem 1rem;
    }
    [data-testid="stAlert"] p {font-size: 0.94rem; line-height: 1.55;}
    [data-testid="stPlotlyChart"] {
        overflow: hidden;
        border: 1px solid rgba(42, 57, 81, 0.72);
        border-radius: var(--radius);
        background: rgba(13, 23, 40, 0.56);
        padding: 0.35rem;
    }
    div[data-testid="stDataFrame"] {
        overflow: hidden;
        border: 1px solid var(--border);
        border-radius: var(--radius);
    }
    [data-testid="stExpander"] {border: 1px solid var(--border); border-radius: 12px; background: rgba(17, 28, 46, 0.62);}
    [data-testid="stChatMessage"] {border: 1px solid var(--border); border-radius: 14px; background: rgba(17, 28, 46, 0.7); padding: 0.4rem 0.65rem;}
    hr {border-color: var(--border);}

    @media (max-width: 900px) {
        .block-container {padding: 1.35rem 1rem 4rem;}
        section[data-testid="stSidebar"],
        section[data-testid="stSidebar"] > div {width: auto !important;}
        [data-testid="stHorizontalBlock"] > [data-testid="stColumn"] {flex-basis: 100% !important; min-width: 100%;}
        [data-testid="stMetric"] {min-height: 112px;}
        [data-testid="stTabs"] [data-baseweb="tab-list"] {overflow-x: auto;}
        [data-testid="stTabs"] button[role="tab"] {padding-inline: 0.75rem; white-space: nowrap;}
    }
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


def fmt_delta(value, suffix="", decimals=0, inverse_good: bool = False):
    if value is None or pd.isna(value):
        return None
    direction = "normal"
    if inverse_good:
        direction = "inverse"
    return f"{float(value):+,.{decimals}f}{suffix}", direction


def sync_error_message(error: Exception) -> str:
    """Remain safe when Streamlit hot-reloads app.py before sync.py."""
    formatter = getattr(sync_module, "friendly_error", None)
    if callable(formatter):
        return formatter(error)

    # A stale imported module may not have the shared formatter yet. Keep the
    # most common network failure useful until the next full process restart.
    detail = str(error)
    if "WinError 10013" in detail or "forbidden by its access permissions" in detail:
        return (
            "Network access to Garmin was blocked by the app runner or Windows. "
            "Launch the dashboard with start.ps1 outside a restricted terminal; "
            "if it still fails, allow Python through Windows Firewall."
        )
    return detail


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


def fmt_time(seconds):
    if seconds is None or pd.isna(seconds):
        return None
    seconds = int(round(float(seconds)))
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes}:{seconds:02d}"


def apply_date_filter(df: pd.DataFrame, column: str, start: date, end: date) -> pd.DataFrame:
    if df.empty:
        return df
    start_ts = pd.Timestamp(start)
    end_ts = pd.Timestamp(end) + pd.Timedelta(days=1) - pd.Timedelta(microseconds=1)
    return df[(df[column] >= start_ts) & (df[column] <= end_ts)].copy()


def preset_date_range(preset: str, min_available: date, max_available: date) -> tuple[date, date]:
    today = date.today()
    anchor = min(max_available, today)
    if preset == "Last 7 days":
        start = anchor - timedelta(days=6)
        end = anchor
    elif preset == "Last 30 days":
        start = anchor - timedelta(days=29)
        end = anchor
    elif preset == "This week":
        start = anchor - timedelta(days=anchor.weekday())
        end = anchor
    elif preset == "This month":
        start = anchor.replace(day=1)
        end = anchor
    elif preset == "This year":
        start = anchor.replace(month=1, day=1)
        end = anchor
    elif preset == "All data":
        start = min_available
        end = max_available
    else:
        start = min_available
        end = max_available
    return max(start, min_available), min(end, max_available)


def previous_range(start: date, end: date) -> tuple[date, date]:
    days = max((end - start).days + 1, 1)
    previous_end = start - timedelta(days=1)
    previous_start = previous_end - timedelta(days=days - 1)
    return previous_start, previous_end


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


def sum_or_none(df: pd.DataFrame, column: str):
    if df.empty or column not in df or not df[column].notna().any():
        return None
    return df[column].sum(skipna=True)


def mean_or_none(df: pd.DataFrame, column: str):
    if df.empty or column not in df or not df[column].notna().any():
        return None
    return df[column].mean(skipna=True)


def diff(current, previous):
    if current is None or previous is None or pd.isna(current) or pd.isna(previous):
        return None
    return current - previous


def percent_delta(current, previous):
    if current is None or previous in (None, 0) or pd.isna(current) or pd.isna(previous):
        return None
    return (current - previous) / previous * 100


def activity_type_label(value) -> str:
    return str(value or "Unknown")


def style_figure(fig: go.Figure, yaxis_title: str | None = None) -> go.Figure:
    x_dates = []
    for trace in fig.data:
        x_values = getattr(trace, "x", None)
        if x_values is None:
            continue
        parsed = pd.to_datetime(pd.Series(list(x_values)), errors="coerce").dropna()
        x_dates.extend(parsed.tolist())

    fig.update_layout(
        margin=dict(l=18, r=18, t=48, b=18),
        height=360,
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        legend_title_text="",
        legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0),
        hovermode="x unified",
        font=dict(
            color=INK,
            family="Segoe UI Variable, Segoe UI, system-ui, sans-serif",
            size=13,
        ),
        hoverlabel=dict(bgcolor="#162238", bordercolor=GRID, font=dict(color=INK, size=13)),
    )
    fig.update_xaxes(showgrid=False, tickfont=dict(color=MUTED, size=12), automargin=True)
    if x_dates:
        fig.update_xaxes(tickformat="%b %d<br>%Y", hoverformat="%b %d, %Y")
        unique_dates = pd.Series(x_dates).drop_duplicates().sort_values()
        if len(unique_dates) == 1:
            center = pd.Timestamp(unique_dates.iloc[0])
            fig.update_xaxes(
                range=[center - pd.Timedelta(days=3), center + pd.Timedelta(days=3)],
                tick0=center.normalize(),
                dtick=24 * 60 * 60 * 1000,
            )
            fig.update_traces(width=18 * 60 * 60 * 1000, selector=dict(type="bar"))
    fig.update_yaxes(
        showgrid=True,
        gridcolor=GRID,
        zeroline=False,
        title=yaxis_title,
        tickfont=dict(color=MUTED, size=12),
        title_font=dict(color=MUTED, size=12),
        automargin=True,
    )
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


def build_data_quality_notes(
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    activities: pd.DataFrame,
    body: pd.DataFrame,
    sync_state: dict,
    start: date,
    end: date,
) -> list[str]:
    notes = []
    expected_days = max((end - start).days + 1, 1)
    last_synced_at = sync_state.get("last_synced_at")
    last_synced_date = sync_state.get("last_synced_date")

    if not last_synced_at and not last_synced_date:
        notes.append("No successful sync recorded yet.")
    elif last_synced_at:
        try:
            synced_at = datetime.fromisoformat(last_synced_at)
            if datetime.now() - synced_at > timedelta(days=2):
                notes.append(f"Last sync was {synced_at:%b %d, %Y at %I:%M %p}.")
        except ValueError:
            notes.append(f"Last sync timestamp looks unusual: {last_synced_at}.")

    if daily.empty:
        notes.append("No daily stats in the selected range.")
    elif daily["date"].dt.date.nunique() < max(1, expected_days * 0.6):
        notes.append("Daily stats cover less than 60% of this date range.")

    if sleep.empty:
        notes.append("No sleep records in the selected range.")
    elif sleep["date"].dt.date.nunique() < max(1, expected_days * 0.5):
        notes.append("Sleep data is sparse for this range.")

    if activities.empty:
        notes.append("No activities in the selected range.")
    elif "avg_hr" in activities and activities["avg_hr"].isna().mean() > 0.35:
        notes.append("More than a third of activities are missing average heart rate.")

    if body.empty:
        notes.append("No body composition entries in this range.")

    return notes[:5]


def build_coach_notes(
    daily: pd.DataFrame,
    previous_daily: pd.DataFrame,
    sleep: pd.DataFrame,
    previous_sleep: pd.DataFrame,
    activities: pd.DataFrame,
    previous_activities: pd.DataFrame,
) -> list[str]:
    notes = []
    current_steps = mean_or_none(daily, "steps")
    previous_steps = mean_or_none(previous_daily, "steps")
    step_change = percent_delta(current_steps, previous_steps)
    if step_change is not None:
        notes.append(f"Average steps are {abs(step_change):.0f}% {'higher' if step_change >= 0 else 'lower'} than the previous period.")

    current_sleep = mean_or_none(sleep, "total_sleep_s")
    previous_sleep_seconds = mean_or_none(previous_sleep, "total_sleep_s")
    sleep_change_h = diff(current_sleep, previous_sleep_seconds)
    if sleep_change_h is not None:
        notes.append(f"Average sleep is {abs(sleep_change_h) / 3600:.1f} h {'higher' if sleep_change_h >= 0 else 'lower'} per night than the previous period.")

    current_hr = mean_or_none(daily, "resting_hr")
    previous_hr = mean_or_none(previous_daily, "resting_hr")
    hr_change = diff(current_hr, previous_hr)
    if hr_change is not None:
        notes.append(f"Resting HR is {abs(hr_change):.0f} bpm {'higher' if hr_change >= 0 else 'lower'} than the previous period.")

    current_distance = sum_or_none(activities, "distance_m")
    previous_distance = sum_or_none(previous_activities, "distance_m")
    distance_change = percent_delta(current_distance, previous_distance)
    if distance_change is not None:
        notes.append(f"Training distance is {abs(distance_change):.0f}% {'higher' if distance_change >= 0 else 'lower'} than the previous period.")

    if not activities.empty:
        top_category = activities.groupby("category")["duration_s"].sum().sort_values(ascending=False)
        if not top_category.empty:
            notes.append(f"Most training time came from {top_category.index[0].lower()} this period.")

    return notes[:5] or ["Not enough comparison data yet. Sync more history to unlock trend notes."]


def training_view_df(activities: pd.DataFrame, category: str, activity_types: list[str]) -> pd.DataFrame:
    if activities.empty:
        return activities
    df = activities.copy()
    if category != "All":
        df = df[df["category"] == category]
    if activity_types:
        labels = df["type"].apply(activity_type_label)
        df = df[labels.isin(activity_types)]
    return df


def build_training_summary(activities: pd.DataFrame, start: date, end: date) -> dict:
    if activities.empty:
        return {}
    details = activity_details_df(activities)
    period_days = max((end - start).days + 1, 1)
    distance_km = details["distance_m"].sum(skipna=True) / 1000
    duration_h = details["duration_s"].sum(skipna=True) / 3600
    summary = {
        "count": len(details),
        "distance_km": distance_km,
        "duration_h": duration_h,
        "weekly_distance_km": distance_km / period_days * 7,
        "weekly_duration_h": duration_h / period_days * 7,
        "avg_hr": mean_or_none(details, "avg_hr"),
        "training_load": sum_or_none(details, "training_load"),
    }
    if details["distance_m"].sum(skipna=True) and details["moving_duration_s"].sum(skipna=True):
        summary["avg_pace"] = fmt_pace(details["distance_m"].sum(skipna=True), details["moving_duration_s"].sum(skipna=True))
    return summary


def clean_value(value):
    if value is None or pd.isna(value):
        return None
    if hasattr(value, "item"):
        return value.item()
    return value


def activity_metric(row: pd.Series, key: str):
    value = raw_value(row, key)
    return clean_value(value)


def best_activity_effort(activities: pd.DataFrame, key: str) -> dict | None:
    efforts = []
    for _, row in activities.iterrows():
        seconds = activity_metric(row, key)
        if seconds:
            efforts.append((float(seconds), row))
    if not efforts:
        return None
    seconds, row = min(efforts, key=lambda item: item[0])
    return {
        "activity": raw_value(row, "activityName", row["type"]),
        "date": row["start_time"].date().isoformat() if not pd.isna(row["start_time"]) else None,
        "seconds": clean_value(seconds),
        "time": fmt_time(seconds),
    }


def summarize_running_details(activities: pd.DataFrame, start: date, end: date) -> dict | None:
    if activities.empty or "category" not in activities:
        return None

    runs = activities[activities["category"] == "Running"].copy()
    runs = runs.dropna(subset=["start_time"])
    if runs.empty:
        return None

    runs["distance_km"] = runs["distance_m"] / 1000
    runs["moving_duration_s"] = runs.apply(
        lambda row: raw_value(row, "movingDuration", row["duration_s"]), axis=1
    )
    runs["pace_min_per_km"] = (runs["moving_duration_s"] / 60) / runs["distance_km"]
    period_days = max((end - start).days + 1, 1)
    weekly_volume_km = runs["distance_km"].sum(skipna=True) / period_days * 7

    longest = runs.sort_values("distance_km", ascending=False).iloc[0]
    recent_runs = runs.sort_values("start_time", ascending=False).head(8)

    return {
        "run_count": int(len(runs)),
        "total_distance_km": clean_value(runs["distance_km"].sum(skipna=True)),
        "estimated_weekly_volume_km": clean_value(weekly_volume_km),
        "avg_run_distance_km": clean_value(runs["distance_km"].mean(skipna=True)),
        "avg_pace_min_per_km": clean_value(runs["pace_min_per_km"].mean(skipna=True)),
        "avg_pace": fmt_pace(runs["distance_m"].sum(skipna=True), runs["moving_duration_s"].sum(skipna=True)),
        "best_5k": best_activity_effort(runs, "fastestSplit_5000"),
        "best_10k": best_activity_effort(runs, "fastestSplit_10000"),
        "best_1_mile": best_activity_effort(runs, "fastestSplit_1609"),
        "longest_run": {
            "activity": raw_value(longest, "activityName", longest["type"]),
            "date": longest["start_time"].date().isoformat(),
            "distance_km": clean_value(longest["distance_km"]),
            "moving_time": fmt_time(longest["moving_duration_s"]),
            "pace": fmt_pace(longest["distance_m"], longest["moving_duration_s"]),
            "avg_hr": clean_value(longest["avg_hr"]),
        },
        "recent_runs": [
            {
                "date": row["start_time"].date().isoformat(),
                "activity": raw_value(row, "activityName", row["type"]),
                "distance_km": clean_value(row["distance_km"]),
                "moving_time": fmt_time(row["moving_duration_s"]),
                "pace": fmt_pace(row["distance_m"], row["moving_duration_s"]),
                "avg_hr": clean_value(row["avg_hr"]),
            }
            for _, row in recent_runs.iterrows()
        ],
    }


def build_current_view_summary(
    daily: pd.DataFrame,
    sleep: pd.DataFrame,
    activities: pd.DataFrame,
    body: pd.DataFrame,
    start: date,
    end: date,
    group_by: str,
    insight_report: dict | None = None,
) -> dict:
    summary = {
        "date_range": {"start": start.isoformat(), "end": end.isoformat()},
        "chart_grouping": group_by,
    }
    if insight_report:
        summary["insight_engine"] = insight_report

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
        running_details = summarize_running_details(activities, start, end)
        if running_details:
            summary["running_details"] = running_details

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


def maybe_auto_sync_on_startup():
    if not config.GARMIN_AUTO_SYNC_ON_STARTUP:
        return
    if st.session_state.get("startup_sync_attempted"):
        return

    st.session_state["startup_sync_attempted"] = True
    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        st.session_state["startup_sync_status"] = "Set GARMIN_EMAIL and GARMIN_PASSWORD in .env first."
        return

    with st.spinner("Syncing with Garmin Connect..."):
        try:
            start, end = sync_module.sync(
                days=config.GARMIN_AUTO_SYNC_DAYS,
                reuse_today=True,
            )
            st.session_state["startup_sync_status"] = (
                f"Auto-synced {config.GARMIN_AUTO_SYNC_DAYS} days: {start} to {end}."
            )
        except Exception as e:
            st.session_state["startup_sync_status"] = (
                f"Auto-sync paused: {sync_error_message(e)}"
            )


maybe_auto_sync_on_startup()

daily_stats = analysis.load_daily_stats()
sleep_df = analysis.load_sleep()
activities_df = analysis.ensure_activity_categories(analysis.load_activities())
body_df = analysis.load_body_composition()
sync_state = analysis.load_sync_state()

all_dates = []
if not daily_stats.empty:
    all_dates.extend(daily_stats["date"].dt.date.tolist())
if not sleep_df.empty:
    all_dates.extend(sleep_df["date"].dt.date.tolist())
if not activities_df.empty:
    all_dates.extend(activities_df["start_time"].dt.date.tolist())
if not body_df.empty:
    all_dates.extend(body_df["date"].dt.date.tolist())

min_date = min(all_dates) if all_dates else date.today()
max_date = max(all_dates) if all_dates else date.today()

st.sidebar.markdown(
    """
    <div class="app-brand">
        <div class="app-brand__mark">↗</div>
        <div>
            <div class="app-brand__name">Garmin Insights</div>
            <div class="app-brand__tagline">Training &amp; recovery dashboard</div>
        </div>
    </div>
    """,
    unsafe_allow_html=True,
)
if startup_sync_status := st.session_state.get("startup_sync_status"):
    if startup_sync_status.startswith("Auto-synced"):
        st.sidebar.success(startup_sync_status)
    else:
        st.sidebar.warning(startup_sync_status)

st.sidebar.subheader("Sync")
last_synced_at = sync_state.get("last_synced_at")
last_synced_date = sync_state.get("last_synced_date")
if last_synced_at:
    try:
        parsed_sync = datetime.fromisoformat(last_synced_at)
        st.sidebar.caption(f"Last sync: {parsed_sync:%b %d, %Y at %I:%M %p}")
    except ValueError:
        st.sidebar.caption(f"Last sync: {last_synced_at}")
elif last_synced_date:
    st.sidebar.caption(f"Last sync date: {last_synced_date}")
else:
    st.sidebar.caption("Last sync: never")
st.sidebar.caption(
    f"Auto-sync: {'on' if config.GARMIN_AUTO_SYNC_ON_STARTUP else 'off'}"
    + (f" | {config.GARMIN_AUTO_SYNC_DAYS} days" if config.GARMIN_AUTO_SYNC_ON_STARTUP else "")
)
sync_days = st.sidebar.number_input(
    "Days to sync",
    min_value=1,
    max_value=365,
    value=7,
    help="Choose how much recent Garmin history to refresh.",
)
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
                st.sidebar.error(f"Sync failed: {sync_error_message(e)}")

st.sidebar.divider()
st.sidebar.subheader("View")
date_preset = st.sidebar.selectbox("Date preset", DATE_PRESETS, index=1)
default_start, default_end = preset_date_range(date_preset, min_date, max_date)
if date_preset == "Custom":
    date_range = st.sidebar.date_input(
        "Date range",
        value=(default_start, default_end),
        min_value=min_date,
        max_value=max_date,
    )
else:
    date_range = (default_start, default_end)
    st.sidebar.caption(f"{default_start:%b %d, %Y} to {default_end:%b %d, %Y}")
if isinstance(date_range, tuple) and len(date_range) == 2:
    start_date, end_date = date_range
else:
    start_date = end_date = date_range
if start_date > end_date:
    start_date, end_date = end_date, start_date

period = st.sidebar.segmented_control("Group charts by", list(PERIODS), default="Weekly")
show_other = st.sidebar.checkbox("Include other activities", value=True)

daily_filtered = apply_date_filter(daily_stats, "date", start_date, end_date)
sleep_filtered = apply_date_filter(sleep_df, "date", start_date, end_date)
activities_filtered = apply_date_filter(activities_df, "start_time", start_date, end_date)
if not show_other and not activities_filtered.empty:
    activities_filtered = activities_filtered[activities_filtered["category"] != "Other"]
body_filtered = apply_date_filter(body_df, "date", start_date, end_date)

previous_start_date, previous_end_date = previous_range(start_date, end_date)
previous_daily = apply_date_filter(daily_stats, "date", previous_start_date, previous_end_date)
previous_sleep = apply_date_filter(sleep_df, "date", previous_start_date, previous_end_date)
previous_activities = apply_date_filter(activities_df, "start_time", previous_start_date, previous_end_date)
if not show_other and not previous_activities.empty:
    previous_activities = previous_activities[previous_activities["category"] != "Other"]

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
quality_notes = build_data_quality_notes(
    daily_filtered,
    sleep_filtered,
    activities_filtered,
    body_filtered,
    sync_state,
    start_date,
    end_date,
)
coach_notes = build_coach_notes(
    daily_filtered,
    previous_daily,
    sleep_filtered,
    previous_sleep,
    activities_filtered,
    previous_activities,
)
insight_report = insights_engine.generate_insights(
    daily_stats,
    sleep_df,
    activities_df,
    body_df,
    start_date,
    end_date,
)
priority_insights = insight_report.get("insights", [])[:5]

st.markdown(
    f"""
    <div class="page-kicker">Health and performance</div>
    <div class="page-title">Your Garmin dashboard</div>
    <div class="page-subtitle">
        {start_date:%b %d, %Y} – {end_date:%b %d, %Y}
        &nbsp;•&nbsp; Grouped {period.lower()}
        &nbsp;•&nbsp; Compared with {previous_start_date:%b %d} – {previous_end_date:%b %d, %Y}
    </div>
    """,
    unsafe_allow_html=True,
)

tab_overview, tab_trends, tab_training, tab_activities, tab_ai = st.tabs(
    ["Overview", "Trends", "Training", "Activities", "AI Insights"]
)

with tab_overview:
    if daily_stats.empty and activities_df.empty and sleep_df.empty:
        st.info("No data yet. Use 'Sync now' in the sidebar to pull data from Garmin Connect.")
    else:
        total_steps = sum_or_none(daily_filtered, "steps")
        previous_steps = sum_or_none(previous_daily, "steps")
        avg_steps = mean_or_none(daily_filtered, "steps")
        avg_sleep_h = (
            mean_or_none(sleep_filtered, "total_sleep_s") / 3600
            if mean_or_none(sleep_filtered, "total_sleep_s") is not None
            else None
        )
        previous_sleep_h = (
            mean_or_none(previous_sleep, "total_sleep_s") / 3600
            if mean_or_none(previous_sleep, "total_sleep_s") is not None
            else None
        )
        avg_resting_hr = (
            mean_or_none(daily_filtered, "resting_hr") if not daily_filtered.empty else None
        )
        previous_resting_hr = (
            mean_or_none(previous_daily, "resting_hr") if not previous_daily.empty else None
        )
        avg_body_battery = mean_or_none(daily_filtered, "body_battery_high")
        previous_body_battery = mean_or_none(previous_daily, "body_battery_high")
        activity_count = len(activities_filtered)
        previous_activity_count = len(previous_activities)
        distance_km = (
            activities_filtered["distance_m"].sum(skipna=True) / 1000
            if not activities_filtered.empty
            else None
        )
        previous_distance_km = (
            previous_activities["distance_m"].sum(skipna=True) / 1000
            if not previous_activities.empty
            else None
        )
        duration_h = (
            activities_filtered["duration_s"].sum(skipna=True) / 3600
            if not activities_filtered.empty
            else None
        )

        cols = st.columns(6)
        steps_delta = fmt_delta(percent_delta(total_steps, previous_steps), "%", 0)
        cols[0].metric(
            "Total steps",
            fmt_number(total_steps),
            delta=steps_delta[0] if steps_delta else None,
            delta_color=steps_delta[1] if steps_delta else "normal",
        )
        avg_steps_delta = fmt_delta(diff(avg_steps, mean_or_none(previous_daily, "steps")))
        cols[1].metric(
            "Avg steps/day",
            fmt_number(avg_steps),
            delta=avg_steps_delta[0] if avg_steps_delta else None,
            delta_color=avg_steps_delta[1] if avg_steps_delta else "normal",
        )
        sleep_delta = fmt_delta(diff(avg_sleep_h, previous_sleep_h), " h", 1)
        cols[2].metric(
            "Avg sleep",
            fmt_number(avg_sleep_h, " h", 1),
            delta=sleep_delta[0] if sleep_delta else None,
            delta_color=sleep_delta[1] if sleep_delta else "normal",
        )
        hr_delta = fmt_delta(diff(avg_resting_hr, previous_resting_hr), " bpm", 0, inverse_good=True)
        cols[3].metric(
            "Resting HR",
            fmt_number(avg_resting_hr, " bpm"),
            delta=hr_delta[0] if hr_delta else None,
            delta_color=hr_delta[1] if hr_delta else "normal",
        )
        activity_delta = fmt_delta(diff(activity_count, previous_activity_count))
        cols[4].metric(
            "Activities",
            f"{activity_count}",
            delta=activity_delta[0] if activity_delta else None,
            delta_color=activity_delta[1] if activity_delta else "normal",
        )
        distance_delta = fmt_delta(percent_delta(distance_km, previous_distance_km), "%", 0)
        cols[5].metric(
            "Distance",
            fmt_number(distance_km, " km", 1),
            delta=distance_delta[0] if distance_delta else None,
            delta_color=distance_delta[1] if distance_delta else "normal",
        )

        cols = st.columns([1, 1])
        with cols[0]:
            st.subheader("Insight engine")
            if priority_insights:
                for insight in priority_insights:
                    title = f"{insight['category']}: {insight['title']}"
                    detail = insight["detail"] + f" (Confidence: {insight.get('confidence', 'medium')}. {insight.get('action') or ''})"
                    if insight["severity"] == "warning":
                        st.warning(f"{title} - {detail}")
                    elif insight["severity"] == "positive":
                        st.success(f"{title} - {detail}")
                    else:
                        st.info(f"{title} - {detail}")
            else:
                for note in coach_notes:
                    st.markdown(f"- {note}")
        with cols[1]:
            st.subheader("Data quality")
            if quality_notes:
                for note in quality_notes:
                    st.markdown(f"- {note}")
            else:
                st.success("Data coverage looks good for this range.")

        st.caption(
            f"Current {period.lower()} overview: {overview_start_date:%b %d, %Y} to "
            f"{overview_end_date:%b %d, %Y}"
        )

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

        cols = st.columns(4)
        cols[0].metric("Training time", fmt_number(duration_h, " h", 1))
        battery_delta = fmt_delta(diff(avg_body_battery, previous_body_battery))
        cols[1].metric(
            "Body battery high",
            fmt_number(avg_body_battery),
            delta=battery_delta[0] if battery_delta else None,
            delta_color=battery_delta[1] if battery_delta else "normal",
        )
        if not overview_activities.empty:
            cols[2].metric(
                "Elevation gain",
                fmt_number(overview_activities["elevation_gain_m"].sum(skipna=True), " m"),
            )
            cols[3].metric(
                "Avg workout HR",
                fmt_number(overview_activities["avg_hr"].mean(skipna=True), " bpm"),
            )
        else:
            cols[2].metric("Elevation gain", "-")
            cols[3].metric("Avg workout HR", "-")

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

with tab_training:
    if activities_filtered.empty:
        st.info("No activities match the current date filters.")
    else:
        available_categories = ["All"] + [
            category
            for category in analysis.ACTIVITY_CATEGORIES
            if category in activities_filtered["category"].dropna().unique()
        ]
        control_cols = st.columns([1, 2])
        sport_group = control_cols[0].selectbox(
            "Sport group",
            options=available_categories,
            index=available_categories.index("Running") if "Running" in available_categories else 0,
        )
        group_df = training_view_df(activities_filtered, sport_group, [])
        available_types = sorted(group_df["type"].dropna().apply(activity_type_label).unique().tolist())
        selected_types = control_cols[1].multiselect(
            "Activity types",
            options=available_types,
            default=available_types,
        )
        training_df = training_view_df(activities_filtered, sport_group, selected_types)
        training_details = activity_details_df(training_df)
        training_period = aggregate_activities(training_df, period)

        if training_df.empty:
            st.info("No activities match the selected training filters.")
        else:
            training_summary = build_training_summary(training_df, start_date, end_date)
            metric_cols = st.columns(6)
            metric_cols[0].metric("Sessions", f"{training_summary.get('count', 0)}")
            metric_cols[1].metric("Distance", fmt_number(training_summary.get("distance_km"), " km", 1))
            metric_cols[2].metric("Time", fmt_number(training_summary.get("duration_h"), " h", 1))
            metric_cols[3].metric(
                "Weekly volume",
                fmt_number(training_summary.get("weekly_distance_km"), " km", 1),
            )
            metric_cols[4].metric("Avg HR", fmt_number(training_summary.get("avg_hr"), " bpm"))
            metric_cols[5].metric("Load", fmt_number(training_summary.get("training_load")))

            cols = st.columns(2)
            with cols[0]:
                st.subheader(f"{period} distance")
                if training_period.empty:
                    st.info("No chart data for this selection.")
                else:
                    distance_period = (
                        training_period.groupby("period", as_index=False)["distance_m"].sum()
                    )
                    fig = go.Figure(
                        go.Bar(
                            x=distance_period["period"],
                            y=distance_period["distance_m"] / 1000,
                            marker_color=BLUE,
                        )
                    )
                    st.plotly_chart(style_figure(fig, "km"), use_container_width=True)

            with cols[1]:
                st.subheader(f"{period} training time")
                if not training_period.empty:
                    duration_period = (
                        training_period.groupby("period", as_index=False)["duration_s"].sum()
                    )
                    fig = go.Figure(
                        go.Bar(
                            x=duration_period["period"],
                            y=duration_period["duration_s"] / 3600,
                            marker_color=AQUA,
                        )
                    )
                    st.plotly_chart(style_figure(fig, "hours"), use_container_width=True)

            cols = st.columns(2)
            with cols[0]:
                st.subheader("Effort trend")
                if not training_details.empty and training_details["avg_hr"].notna().any():
                    fig = go.Figure()
                    fig.add_trace(
                        go.Scatter(
                            x=training_details["start_time"],
                            y=training_details["avg_hr"],
                            name="Avg HR",
                            mode="lines+markers",
                            line=dict(color=ROSE, width=2.5),
                        )
                    )
                    if training_details["training_load"].notna().any():
                        fig.add_trace(
                            go.Bar(
                                x=training_details["start_time"],
                                y=training_details["training_load"],
                                name="Load",
                                marker_color=YELLOW,
                                yaxis="y2",
                                opacity=0.45,
                            )
                        )
                        fig.update_layout(
                            yaxis2=dict(title="load", overlaying="y", side="right", showgrid=False)
                        )
                    st.plotly_chart(style_figure(fig, "bpm"), use_container_width=True)
                else:
                    st.info("No heart-rate trend for this selection.")

            with cols[1]:
                st.subheader("Recent sessions")
                recent_cols = [
                    "start_time",
                    "name",
                    "distance_km",
                    "duration",
                    "pace",
                    "avg_hr",
                    "training_load",
                ]
                st.dataframe(
                    training_details[recent_cols].sort_values("start_time", ascending=False).head(8),
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "start_time": st.column_config.DatetimeColumn("Start"),
                        "name": "Activity",
                        "distance_km": st.column_config.NumberColumn("Distance km", format="%.2f"),
                        "avg_hr": st.column_config.NumberColumn("Avg HR", format="%.0f"),
                        "training_load": st.column_config.NumberColumn("Load", format="%.0f"),
                    },
                )

            if sport_group == "Running" or any("run" in t.lower() for t in selected_types):
                running_details = summarize_running_details(training_df, start_date, end_date)
                if running_details:
                    st.subheader("Running focus")
                    run_cols = st.columns(4)
                    run_cols[0].metric("Avg pace", running_details["avg_pace"])
                    run_cols[1].metric(
                        "Avg run distance",
                        fmt_number(running_details["avg_run_distance_km"], " km", 1),
                    )
                    run_cols[2].metric(
                        "Est weekly volume",
                        fmt_number(running_details["estimated_weekly_volume_km"], " km", 1),
                    )
                    longest = running_details["longest_run"]
                    run_cols[3].metric("Longest run", fmt_number(longest["distance_km"], " km", 1))

                    best_cols = st.columns(3)
                    for col, label, key in [
                        (best_cols[0], "Best 5K", "best_5k"),
                        (best_cols[1], "Best 10K", "best_10k"),
                        (best_cols[2], "Best mile", "best_1_mile"),
                    ]:
                        effort = running_details.get(key)
                        col.metric(label, effort["time"] if effort else "-")

            sport_insights = [
                insight
                for insight in insight_report.get("insights", [])
                if insight.get("category") in {sport_group, "Training load", "Recovery"}
                or (sport_group == "All" and insight.get("category") in analysis.ACTIVITY_CATEGORIES)
            ]
            if sport_insights:
                st.subheader("Training insights")
                for insight in sport_insights[:4]:
                    message = f"{insight['category']}: {insight['title']} - {insight['detail']}"
                    if insight["severity"] == "warning":
                        st.warning(message)
                    elif insight["severity"] == "positive":
                        st.success(message)
                    else:
                        st.info(message)

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
            insight_report,
        )
        summary_json = json.dumps(summary, indent=2, default=str)
        summary_key = f"{provider}:{model}:{summary_json}"

        if "ai_chat_messages" not in st.session_state:
            st.session_state.ai_chat_messages = []
        if st.session_state.get("ai_chat_summary_key") != summary_key:
            st.session_state.ai_chat_messages = []
            st.session_state.ai_chat_summary_key = summary_key

        st.caption(f"Provider: {provider} / Model: {model}")
        st.subheader("Suggested focus")
        for insight in priority_insights:
            st.markdown(f"- **{insight['category']}**: {insight['detail']}")

        suggested_prompts = [
            "What are the most important signals in the insight engine report?",
            "What should I focus on for recovery this week?",
            "Summarize my training consistency, load risk, and sport-specific changes.",
        ]
        st.caption("Try asking:")
        prompt_cols = st.columns(3)
        for idx, suggested_prompt in enumerate(suggested_prompts):
            if prompt_cols[idx].button(suggested_prompt, disabled=not ai_ready, use_container_width=True):
                st.session_state.ai_chat_messages = [{"role": "user", "content": suggested_prompt}]
                with st.spinner(f"Asking {provider}..."):
                    try:
                        answer = ai_insights.chat(summary, st.session_state.ai_chat_messages)
                    except Exception as e:
                        answer = f"I couldn't get an AI response for that question: {e}"
                st.session_state.ai_chat_messages.append({"role": "assistant", "content": answer})
                st.rerun()

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
