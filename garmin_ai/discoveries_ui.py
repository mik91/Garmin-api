import pandas as pd
import plotly.express as px
import streamlit as st

from garmin_ai.discoveries import compare_groups, recovery_observations, run_observations


def _evidence(frame, name):
    with st.expander("Inspect supporting observations"):
        st.dataframe(frame, hide_index=True, use_container_width=True)
        st.download_button("Download observations", frame.to_csv(index=False), file_name=f"{name}.csv", mime="text/csv", key=f"download_{name}")


def _comparison(frame, group, metric, first, second, unit):
    result = compare_groups(frame, group, metric, first, second)
    st.caption(f"{first}: {result['first_n']} days · {second}: {result['second_n']} days")
    if result["difference"] is None:
        st.info("Not enough observations for a comparison. Each group needs at least 5 days; try a longer date range or broader run filters.")
    else:
        delta = result["difference"]
        st.write(f"Median {metric.replace('_', ' ')} was **{abs(delta):.2f} {unit} {'higher' if delta >= 0 else 'lower'}** for {first.lower()} than for {second.lower()}.")
        st.caption("Exploratory association · 5 days is a display minimum, not evidence of statistical reliability.")


def render(activities, sleep, daily, start, end):
    st.subheader("Discoveries")
    st.write("Explore how your sleep, training, and recovery move together. Every comparison includes the observations behind it.")
    st.caption(f"Using the dashboard date range: {start:%b %d, %Y} – {end:%b %d, %Y}. Choose All data in the sidebar for more history.")
    st.info("These comparisons describe your recorded history. They do not establish cause or predict your next session. Weather, workout purpose, routes, and other factors may explain a pattern.")
    with st.expander("Choose comparable runs", expanded=True):
        a, b, c = st.columns(3)
        distance = a.slider("Distance (km)", 1, 50, (3, 20), key="discovery_distance")
        hr = b.slider("Average heart rate (bpm)", 70, 210, (110, 165), key="discovery_hr")
        climb = c.slider("Maximum climb (m/km)", 0, 100, 20, key="discovery_climb")
        st.caption("Standard runs and treadmill runs only; earliest qualifying run each day. Missing heart rate or elevation is excluded. Narrow the ranges to improve comparability; route and workout intensity are not fully controlled.")
    runs = run_observations(activities, sleep, start, end, distance, hr, climb)
    st.caption(f"{len(runs)} qualifying run days · {runs['sleep_hours'].notna().sum()} with verified preceding sleep")

    st.markdown("### Sleep and the following run")
    st.write("On days after longer sleep, was your pace different within the selected run filters?")
    paired = runs.dropna(subset=["sleep_hours"]).copy()
    if paired.empty:
        st.info("No qualifying runs with a verified sleep end before the run. Broaden the date range or sync more history.")
    else:
        median_sleep = paired["sleep_hours"].median()
        paired["sleep_group"] = paired["sleep_hours"].map(lambda value: "Longer sleep" if value > median_sleep else "Other nights")
        st.caption(f"Longer sleep means more than {median_sleep:.2f} hours, the median among these paired run days. Lower min/km means faster. Sleep is joined on Garmin's wake-up date and verified using its local end timestamp.")
        _comparison(paired, "sleep_group", "pace_min_km", "Longer sleep", "Other nights", "min/km")
        st.plotly_chart(px.scatter(paired, x="sleep_hours", y="pace_min_km", color="sleep_group", hover_data=["date", "avg_hr", "distance_km"], labels={"sleep_hours": "Preceding sleep (hours)", "pace_min_km": "Pace (min/km)", "sleep_group": "Sleep"}), use_container_width=True)
        _evidence(paired, "sleep_and_runs")

    st.divider()
    st.markdown("### Recovery after higher-load days")
    lag = st.selectbox("Recovery observation", [1, 2], format_func=lambda value: f"{value} calendar day{'s' if value == 2 else ''} later", key="discovery_lag")
    st.caption("Higher load means above the 75th percentile of total Garmin activity load on recorded active days in this range, across all sports. Changes compare the activity-day morning with the selected later morning. Missing days are never filled. Intervening exercise, especially over two days, can affect the result.")
    recovery = recovery_observations(activities, daily, start, end, lag)
    metric = st.selectbox("Recovery metric", ["hrv_change", "resting_hr_change"], format_func=lambda value: "Overnight HRV change (ms)" if value == "hrv_change" else "Resting heart rate change (bpm)", key="discovery_recovery_metric")
    _comparison(recovery, "group", metric, "Higher load", "Other active days", "ms" if metric == "hrv_change" else "bpm")
    if recovery[metric].notna().any():
        st.plotly_chart(px.box(recovery.dropna(subset=[metric]), x="group", y=metric, points="all", hover_data=["date", "outcome_date", "load"], labels={"group": "Activity day", metric: "Change (ms)" if metric == "hrv_change" else "Change (bpm)"}), use_container_width=True)
    _evidence(recovery, "recovery_after_load")

    st.divider()
    st.markdown("### Running efficiency over time")
    st.write("Has pace changed within your selected heart-rate, distance, and climbing ranges?")
    midpoint = pd.Timestamp(start) + (pd.Timestamp(end) - pd.Timestamp(start)) / 2
    trend = runs.copy()
    trend["period"] = trend["date"].map(lambda value: "Later half" if value > midpoint else "Earlier half")
    st.caption(f"The date range is divided at {midpoint:%b %d, %Y}. This is a filtered pace comparison, not a direct fitness measurement. Compare the heart rates and distances in the evidence table before interpreting a change.")
    _comparison(trend, "period", "pace_min_km", "Later half", "Earlier half", "min/km")
    if not trend.empty:
        st.plotly_chart(px.scatter(trend, x="date", y="pace_min_km", color="avg_hr", hover_data=["distance_km", "climb_m_per_km"], labels={"date": "Run date", "pace_min_km": "Pace (min/km)", "avg_hr": "Avg HR"}), use_container_width=True)
    _evidence(trend, "running_efficiency")
    st.caption("To follow a pattern: download today's evidence, keep the same filters, and revisit after more sessions. New observations may strengthen or reverse the comparison.")
