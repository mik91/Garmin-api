"""Main local Garmin interface. Run with python -m garmin_ai.web_server."""
import json
import os
from contextlib import closing
import math
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pandas as pd
from plotly.offline import get_plotlyjs

from garmin_ai.config import DB_PATH, WEB_PORT
from garmin_ai.discoveries import run_observations, recovery_observations
from garmin_ai.goal_context import build_goal_context
from garmin_ai import connections, web_dashboard

ASSETS = Path(__file__).resolve().parent.parent / "web" / "dist"


def build_payload(query, db_path=DB_PATH):
    with closing(sqlite3.connect(Path(db_path).resolve().as_uri() + "?mode=ro", uri=True)) as conn:
        activities = pd.read_sql_query("SELECT * FROM activities ORDER BY start_time", conn)
        sleep = pd.read_sql_query("SELECT * FROM sleep ORDER BY date", conn)
        daily = pd.read_sql_query("SELECT * FROM daily_stats ORDER BY date", conn)
    dates = list(daily.get("date", [])) + list(activities.get("start_time", pd.Series(dtype=str)).str[:10])
    lower, upper = (min(dates), max(dates)) if dates else (pd.Timestamp.today().date().isoformat(),) * 2
    start = pd.Timestamp(query.get("start", [lower])[0])
    end = pd.Timestamp(query.get("end", [upper])[0])
    if pd.isna(start) or pd.isna(end) or start.tzinfo or end.tzinfo or start > end:
        raise ValueError("Choose a valid date range.")
    def number(name, default, low, high):
        value = float(query.get(name, [default])[0])
        if not math.isfinite(value) or not low <= value <= high:
            raise ValueError(f"Invalid {name} filter.")
        return value
    distance = (number("min_km", 3, 1, 100), number("max_km", 20, 1, 100))
    hr = (number("min_hr", 110, 40, 230), number("max_hr", 165, 40, 230))
    if distance[0] > distance[1] or hr[0] > hr[1]:
        raise ValueError("Minimum filters must not exceed maximum filters.")
    climb = number("climb", 20, 0, 200)
    lag = number("lag", 1, 1, 2)
    if lag not in (1, 2):
        raise ValueError("Recovery lag must be 1 or 2 days.")
    runs = run_observations(activities, sleep, start, end, distance, hr, climb)
    # Enrich only with the daily values required for the selected-day panel.
    stats = daily.reindex(columns=["date", "hrv_overnight", "resting_hr", "stress_avg", "training_readiness"]).copy()
    stats["date"] = pd.to_datetime(stats["date"])
    runs = runs.merge(stats, on="date", how="left")
    recovery = recovery_observations(activities, daily, start, end, int(lag))
    def records(frame):
        return json.loads(frame.to_json(orient="records", date_format="iso"))
    return {"bounds": {"start": lower, "end": upper}, "start": start.date().isoformat(),
            "end": end.date().isoformat(), "runs": records(runs), "recovery": records(recovery),
            "total_activities": len(activities), "goal_context": build_goal_context(activities, daily)}



class LocalServer(ThreadingHTTPServer):
    # On Windows SO_REUSEADDR lets a second server bind the same port and the old
    # one keeps answering, so a restarted server looks like it is running stale code.
    allow_reuse_address = os.name != "nt"


class Handler(BaseHTTPRequestHandler):
    def local_host(self):
        return self.headers.get("Host") in (f"127.0.0.1:{WEB_PORT}", f"localhost:{WEB_PORT}")

    def json_response(self, value, status=200):
        body = json.dumps(value, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Custom header + strict origin/host checks block cross-site local actions.
        if not self.local_host() or self.headers.get("X-Garmin-Request") != "local-web" or self.headers.get("Origin") not in (None, f"http://127.0.0.1:{WEB_PORT}", f"http://localhost:{WEB_PORT}"):
            self.json_response({"error": "Request must originate from the local app."}, 403)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 260000 or self.headers.get_content_type() != "application/json":
                raise ValueError("Invalid request size or content type.")
            body = json.loads(self.rfile.read(length))
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            actions = {"/api/goals": web_dashboard.save_context, "/api/adherence": web_dashboard.save_adherence,
                       "/api/sync": web_dashboard.begin_sync, "/api/chat": web_dashboard.chat, "/api/ai-preview": web_dashboard.ai_preview, "/api/activity-splits": web_dashboard.activity_splits,
                       "/api/connections/ai": connections.save_ai, "/api/connections/ai-test": connections.test_ai,
                       "/api/connections/cli-login": connections.cli_login, "/api/connections/sync": connections.save_sync,
                       "/api/connections/garmin-login": connections.garmin_login, "/api/connections/garmin-mfa": connections.garmin_mfa,
                       "/api/connections/garmin-logout": connections.garmin_logout}
            action = actions.get(urlsplit(self.path).path)
            if action is None:
                self.json_response({"error": "Unknown action."}, 404)
                return
            self.json_response(action(body))
        except (ValueError, TypeError, OverflowError) as error:
            self.json_response({"error": str(error)[:300]}, 400)
        except Exception:
            self.json_response({"error": "The action failed. Check the local configuration and try again."}, 500)

    def do_GET(self):
        if not self.local_host():
            self.send_error(403)
            return
        route = urlsplit(self.path)
        try:
            if route.path == "/api/dashboard":
                self.json_response(web_dashboard.payload(parse_qs(route.query)))
                return
            elif route.path == "/api/activity-raw":
                self.json_response(web_dashboard.activity_raw(parse_qs(route.query)))
                return
            elif route.path == "/api/connections":
                self.json_response(connections.status())
                return
            elif route.path == "/api/sync-status":
                with web_dashboard.sync_lock:
                    status = dict(web_dashboard.sync_status)
                self.json_response(status)
                return
            elif route.path == "/api/discoveries":
                body = json.dumps(build_payload(parse_qs(route.query)), allow_nan=False).encode()
                mime = "application/json"
            elif route.path == "/plotly.js":
                body, mime = get_plotlyjs().encode(), "text/javascript"
            elif route.path in ("/", "/app.js", "/dashboard.js", "/style.css"):
                name = {"/": "index.html", "/app.js": "app.js", "/dashboard.js": "dashboard.js", "/style.css": "style.css"}[route.path]
                body = (ASSETS / name).read_bytes()
                mime = "text/html" if name.endswith("html") else "text/css" if name.endswith("css") else "text/javascript"
            else:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", mime + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)
        except (ValueError, OverflowError):
            self.send_error(400, "Invalid filters or date range")
        except Exception:
            self.send_error(500, "Could not load local Garmin history")


if __name__ == "__main__":
    from garmin_ai import db
    with db.get_connection():
        pass
    print(f"Garmin Insights: http://127.0.0.1:{WEB_PORT}", flush=True)
    note = web_dashboard.start_auto_sync()
    print(note or "Auto-sync is off (turn it on from the Connections page)", flush=True)
    try:
        server = LocalServer(("127.0.0.1", WEB_PORT), Handler)
    except OSError:
        raise SystemExit(f"Port {WEB_PORT} is already in use. Stop the other Garmin Insights server first.")
    server.serve_forever()
