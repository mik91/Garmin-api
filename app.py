import json

import streamlit as st
import plotly.graph_objects as go

from garmin_ai import analysis, ai_insights, config
from garmin_ai import sync as sync_module

# Palette (validated categorical order — see dataviz skill reference palette)
BLUE = "#2a78d6"
AQUA = "#1baf7a"
YELLOW = "#eda100"
VIOLET = "#4a3aa7"
MUTED = "#898781"
ACTIVITY_COLORS = {
    "Running": BLUE,
    "Swimming": AQUA,
    "Cycling": YELLOW,
    "Other": MUTED,
}

st.set_page_config(page_title="Garmin Insights", layout="wide")

st.sidebar.header("Garmin Insights")
sync_days = st.sidebar.number_input("Days to sync", min_value=1, max_value=365, value=7)
if st.sidebar.button("Sync now"):
    if not config.GARMIN_EMAIL or not config.GARMIN_PASSWORD:
        st.sidebar.error("Set GARMIN_EMAIL and GARMIN_PASSWORD in .env first.")
    else:
        with st.spinner("Syncing with Garmin Connect..."):
            try:
                start, end = sync_module.sync(days=sync_days)
                st.sidebar.success(f"Synced {sync_days} days: {start} to {end}.")
            except Exception as e:
                st.sidebar.error(f"Sync failed: {e}")

tab_overview, tab_trends, tab_activities, tab_ai = st.tabs(
    ["Overview", "Trends", "Activities", "AI Insights"]
)

daily_stats = analysis.load_daily_stats()
sleep_df = analysis.load_sleep()
activities_df = analysis.load_activities()
activities_df = analysis.ensure_activity_categories(activities_df)

with tab_overview:
    st.subheader("Today at a glance")
    if daily_stats.empty:
        st.info("No data yet. Use 'Sync now' in the sidebar to pull data from Garmin Connect.")
    else:
        latest = daily_stats.iloc[-1]
        col1, col2, col3, col4 = st.columns(4)
        col1.metric("Steps", f"{int(latest['steps']):,}" if latest["steps"] else "-")
        col2.metric("Resting HR", f"{latest['resting_hr']:.0f} bpm" if latest["resting_hr"] else "-")
        col3.metric(
            "Body Battery",
            f"{latest['body_battery_low']:.0f}-{latest['body_battery_high']:.0f}"
            if latest["body_battery_high"]
            else "-",
        )
        if not sleep_df.empty:
            last_sleep = sleep_df.iloc[-1]
            hours = (last_sleep["total_sleep_s"] or 0) / 3600
            col4.metric("Sleep", f"{hours:.1f} h")
        else:
            col4.metric("Sleep", "-")

with tab_trends:
    if daily_stats.empty:
        st.info("No data yet. Sync first.")
    else:
        st.subheader("Resting heart rate")
        hr_df = analysis.add_rolling_averages(daily_stats.dropna(subset=["resting_hr"]), "resting_hr")
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=hr_df["date"], y=hr_df["resting_hr"], name="Daily", mode="markers", marker=dict(color=MUTED, size=6)))
        fig.add_trace(go.Scatter(x=hr_df["date"], y=hr_df["resting_hr_roll7"], name="7-day avg", mode="lines", line=dict(color=BLUE, width=2)))
        fig.update_layout(yaxis_title="bpm", legend_title_text="")
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Steps")
        steps_df = daily_stats.dropna(subset=["steps"])
        fig = go.Figure(go.Bar(x=steps_df["date"], y=steps_df["steps"], marker_color=BLUE))
        fig.update_layout(yaxis_title="steps")
        st.plotly_chart(fig, use_container_width=True)

        st.subheader("Stress")
        stress_df = daily_stats.dropna(subset=["stress_avg"])
        if not stress_df.empty:
            fig = go.Figure(go.Scatter(x=stress_df["date"], y=stress_df["stress_avg"], mode="lines", line=dict(color=BLUE, width=2)))
            fig.update_layout(yaxis_title="avg stress")
            st.plotly_chart(fig, use_container_width=True)

        if not sleep_df.empty:
            st.subheader("Sleep")
            fig = go.Figure()
            fig.add_trace(go.Bar(x=sleep_df["date"], y=sleep_df["deep_s"] / 3600, name="Deep", marker_color=BLUE))
            fig.add_trace(go.Bar(x=sleep_df["date"], y=sleep_df["light_s"] / 3600, name="Light", marker_color=AQUA))
            fig.add_trace(go.Bar(x=sleep_df["date"], y=sleep_df["rem_s"] / 3600, name="REM", marker_color=YELLOW))
            fig.add_trace(go.Bar(x=sleep_df["date"], y=sleep_df["awake_s"] / 3600, name="Awake", marker_color=VIOLET))
            fig.update_layout(barmode="stack", yaxis_title="hours", legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)

with tab_activities:
    if activities_df.empty:
        st.info("No activities yet. Sync first.")
    else:
        st.subheader("Activity volume by type")
        default_categories = [
            c for c in ["Running", "Swimming", "Cycling"] if c in activities_df["category"].unique()
        ]
        selected_categories = st.multiselect(
            "Activity types",
            options=list(analysis.ACTIVITY_CATEGORIES),
            default=default_categories or list(analysis.ACTIVITY_CATEGORIES),
        )
        filtered_activities = activities_df[activities_df["category"].isin(selected_categories)]

        if filtered_activities.empty:
            st.info("No activities match the selected types.")
        else:
            summary_cols = st.columns(len(selected_categories) or 1)
            for col, category in zip(summary_cols, selected_categories):
                category_df = filtered_activities[filtered_activities["category"] == category]
                distance_km = category_df["distance_m"].sum(skipna=True) / 1000
                duration_h = category_df["duration_s"].sum(skipna=True) / 3600
                col.metric(category, f"{distance_km:.1f} km", f"{duration_h:.1f} h")

            weekly = (
                filtered_activities.assign(week=filtered_activities["start_time"].dt.to_period("W").dt.start_time)
                .groupby(["week", "category"], as_index=False)
                .agg(distance_m=("distance_m", "sum"), duration_s=("duration_s", "sum"))
            )

            fig = go.Figure()
            for category in selected_categories:
                category_weekly = weekly[weekly["category"] == category]
                if category_weekly.empty:
                    continue
                fig.add_trace(
                    go.Bar(
                        x=category_weekly["week"],
                        y=category_weekly["distance_m"] / 1000,
                        name=category,
                        marker_color=ACTIVITY_COLORS.get(category, MUTED),
                    )
                )
            fig.update_layout(barmode="group", yaxis_title="km", legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)

            fig = go.Figure()
            for category in selected_categories:
                category_weekly = weekly[weekly["category"] == category]
                if category_weekly.empty:
                    continue
                fig.add_trace(
                    go.Bar(
                        x=category_weekly["week"],
                        y=category_weekly["duration_s"] / 3600,
                        name=category,
                        marker_color=ACTIVITY_COLORS.get(category, MUTED),
                    )
                )
            fig.update_layout(barmode="group", yaxis_title="hours", legend_title_text="")
            st.plotly_chart(fig, use_container_width=True)

        st.subheader("Activity history")
        st.dataframe(
            activities_df[["start_time", "category", "type", "duration_s", "distance_m", "calories", "avg_hr", "max_hr"]]
            .sort_values("start_time", ascending=False),
            use_container_width=True,
        )

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

        summary = analysis.build_summary()
        summary_json = json.dumps(summary, indent=2, default=str)
        summary_key = f"{provider}:{model}:{summary_json}"

        if "ai_summary" not in st.session_state:
            st.session_state.ai_summary = None
        if st.session_state.get("ai_summary_key") != summary_key:
            st.session_state.ai_summary = None
            st.session_state.ai_summary_key = summary_key

        st.caption(f"Provider: {provider} / Model: {model}")
        with st.expander("Aggregated summary that will be sent"):
            st.json(summary)

        if st.button("Generate insights", disabled=not ai_ready):
            with st.spinner(f"Asking {provider} for a summary..."):
                st.session_state.ai_summary = ai_insights.generate_summary(summary)
                st.session_state.ai_summary_key = summary_key

        if st.session_state.ai_summary:
            st.markdown(st.session_state.ai_summary)

        st.divider()
        st.subheader("Ask a question about your data")
        question = st.text_input("Question")
        if st.button("Ask", disabled=not ai_ready) and question:
            with st.spinner("Thinking..."):
                answer = ai_insights.ask(summary, question)
            st.markdown(answer)
