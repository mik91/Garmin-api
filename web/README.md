# Discoveries interface prototype

Start from the project root with `python -m garmin_ai.web_server`, then open
http://127.0.0.1:8502. The original Streamlit application remains on port 8501.

This is a separate HTML/CSS/JavaScript frontend and a local Python JSON API.
It reuses the existing discovery calculations and the installed Plotly library;
no frontend installation, CDN, cloud upload, or API key is needed. The server
binds only to localhost, reads SQLite in read-only mode, and serves an explicit
allowlist of assets. This prototype is intended for local use.

The three views share the date and run filters. Click either plot or choose a
date in the observation selector for details. In the efficiency and recovery
views, zooming the date axis updates the companion graph. Sleep versus pace
has an hours axis, so only its point selection is linked. Comparisons and CSV
exports always use the full filtered date range, not the zoomed viewport.

The static files are in `web/dist`; `/plotly.js` comes from the Python package,
and `/api/discoveries` computes current observations from the local database.
The charts show exploratory group comparisons, not validated predictions.

The goal panel supports distance, finish time (slider or H:MM:SS input), and
1–52 weeks remaining. Defaults are a half marathon, an example 1:45 finish,
and five weeks; settings persist in this browser's local storage. Target
changes are calculated in the frontend and do not alter the race model.
The three question cards use history through today, independently of the
chart filters. The race estimate describes current ability; the timeframe
does not extrapolate future gains. Matched-session and recovery comparisons
are exploratory, and source activities are selected by their race/time-trial
labels rather than manual verification.


## Main application

`start.ps1` now starts this interface. Pass `-Legacy` for Streamlit. The additional
`dashboard.js` surface provides Overview, Trends, Training, Calendar, Activities,
and AI insights using `web_dashboard.py`. POST endpoints support manual sync,
saved context, adherence corrections, and user-initiated chat. Requests require
JSON, a custom header, and an allowed local Host/Origin; no cross-origin API is
provided. The server binds only to loopback. Data stays local except when the
user explicitly starts Garmin sync or an AI request. Do not deploy this local
server as a public service.
