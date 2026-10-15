# Web frontend

Static HTML/CSS/JavaScript served by `garmin_ai/web_server.py`; no build step,
npm install or CDN is needed. `/plotly.js` is served from the installed Python
`plotly` package.

- `index.html`, `style.css` – page shell and styling
- `app.js` – Discoveries view, backed by `/api/discoveries`
- `dashboard.js` – Overview, Trends, Training, Calendar, Activities and AI views,
  backed by the endpoints in `garmin_ai/web_dashboard.py` and `web_extras.py`

The server binds to loopback only and serves an explicit allowlist of these files.
POST endpoints (sync, saved context, calendar corrections, chat) require JSON, a
custom header and a local Host/Origin. Discovery charts show exploratory group
comparisons, not validated predictions.
