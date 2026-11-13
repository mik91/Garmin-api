"""Garmin Insights web server. Run with python -m garmin_ai.web_server.

Local mode (default) binds to 127.0.0.1 and only answers requests addressed to it.
Hosted mode (GARMIN_HOSTED=true) is meant to sit behind an HTTPS reverse proxy: the
dashboard requires the password sign-in, /api accepts a session cookie or an OAuth
bearer token, and the remote MCP server with its OAuth endpoints is mounted at /mcp.
"""
import contextlib
import functools
import hashlib
import html
import json
import logging
import math
import os
import socket
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pandas as pd
from plotly.offline import get_plotlyjs
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.middleware.gzip import GZipMiddleware
from starlette.requests import Request
from starlette.responses import HTMLResponse, PlainTextResponse, RedirectResponse, Response
from starlette.routing import Mount, Route

from garmin_ai import coach, config, connections, hevy, web_dashboard
from garmin_ai.coaching import rolling
from garmin_ai.discoveries import recovery_observations, run_observations
from garmin_ai.goal_context import build_goal_context

log = logging.getLogger(__name__)
ASSETS = Path(__file__).resolve().parent.parent / "web" / "dist"
STATIC = {"/": ("index.html", "text/html"), "/app.js": ("app.js", "text/javascript"),
          "/dashboard.js": ("dashboard.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                    "Referrer-Policy": "same-origin", "X-Frame-Options": "DENY"}
# Routes that handle their own authentication (or need none) in hosted mode.
PUBLIC_PATHS = {"/login", "/healthz", "/oauth/approve", "/authorize", "/token", "/register", "/revoke", "/mcp"}


def build_payload(query, db_path=None):
    db_path = db_path or config.DB_PATH
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


def json_response(value, status=200):
    return Response(json.dumps(value, allow_nan=False), status, SECURITY_HEADERS,
                    media_type="application/json")


# Looked up by name on each request so the handlers can be patched in tests.
POST_ACTIONS = {
    "/api/goals": (web_dashboard, "save_context"), "/api/adherence": (web_dashboard, "save_adherence"),
    "/api/sync": (web_dashboard, "begin_sync"), "/api/chat": (web_dashboard, "chat"),
    "/api/ai-preview": (web_dashboard, "ai_preview"), "/api/activity-splits": (web_dashboard, "activity_splits"),
    "/api/connections/ai": (connections, "save_ai"), "/api/connections/ai-test": (connections, "test_ai"),
    "/api/connections/cli-login": (connections, "cli_login"), "/api/connections/sync": (connections, "save_sync"),
    "/api/connections/garmin-login": (connections, "garmin_login"),
    "/api/connections/garmin-mfa": (connections, "garmin_mfa"),
    "/api/connections/garmin-logout": (connections, "garmin_logout"),
    "/api/plan/move": (coach, "move_request"),
    "/api/rolling-plan/approve": (rolling, "approve_request"),
    "/api/rolling-plan/reject": (rolling, "reject_request"),
    "/api/rolling-plan/publish": (rolling, "publish_request"),
}
# The only writes an OAuth token (the mobile app) may make; everything else stays read-only.
TOKEN_POST_PATHS = {"/api/plan/move", "/api/rolling-plan/approve", "/api/rolling-plan/reject",
                    "/api/rolling-plan/publish"}
GET_ACTIONS = {
    "/api/dashboard": lambda query: web_dashboard.payload(query),
    "/api/activity-raw": lambda query: web_dashboard.activity_raw(query),
    "/api/connections": lambda query: connections.status(),
    "/api/sync-status": lambda query: web_dashboard.current_sync_status(),
    "/api/discoveries": lambda query: build_payload(query),
    "/api/strength": lambda query: hevy.payload(query),
    "/api/strength/exercise": lambda query: hevy.exercise_progress(query),
    "/api/plan": lambda query: coach.plan_payload(query),
    "/api/review": lambda query: web_dashboard.review_payload(query),
    "/api/rolling-plan": lambda query: _rolling_plan(query),
    "/api/rolling-plan/drift": lambda query: rolling.drift_request(query),
}


def _rolling_plan(query) -> dict:
    """GET /api/rolling-plan?start=YYYY-MM-DD&weeks=N: the stored plan to race day."""
    from datetime import date
    from garmin_ai import db
    from garmin_ai.coaching import history, rolling
    start = (query.get("start") or [None])[0]
    weeks = (query.get("weeks") or [None])[0]
    try:
        if start:
            date.fromisoformat(start)
        weeks = int(weeks) if weeks else None
    except ValueError:
        raise ValueError("start must be YYYY-MM-DD and weeks a number.") from None
    if weeks is not None and not 1 <= weeks <= 60:
        raise ValueError("weeks must be 1-60.")
    with db.get_connection() as conn:
        try:
            return rolling.plan(conn, start, weeks)
        except history.HistoryError as error:
            raise ValueError(str(error)) from None


async def api_post(request: Request):
    module, name = POST_ACTIONS[request.url.path]
    action = getattr(module, name)
    try:
        raw = await request.body()
        if not 0 < len(raw) <= 260000 or request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("Invalid request size or content type.")
        body = json.loads(raw)
        if not isinstance(body, dict):
            raise ValueError("Expected a JSON object.")
        return json_response(await run_in_threadpool(action, body))
    except (ValueError, TypeError, OverflowError) as error:
        return json_response({"error": str(error)[:300]}, 400)
    except Exception:
        log.exception("POST %s failed", request.url.path)
        return json_response({"error": "The action failed. Check the configuration and try again."}, 500)


async def api_get(request: Request):
    query = parse_qs(request.url.query)
    try:
        return json_response(await run_in_threadpool(GET_ACTIONS[request.url.path], query))
    except (ValueError, OverflowError) as error:
        return json_response({"error": str(error)[:300] or "Invalid filters or date range"}, 400)
    except hevy.NotFound as error:
        return json_response({"error": str(error)[:300]}, 404)
    except Exception:
        log.exception("GET %s failed", request.url.path)
        return json_response({"error": "Could not load Garmin history"}, 500)


def _cached(request: Request, body: bytes, mime: str) -> Response:
    """Static assets: the browser keeps a copy and revalidates it by ETag (304 when unchanged)."""
    etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {**SECURITY_HEADERS, "Cache-Control": "no-cache", "ETag": etag}
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers=headers)
    return Response(body, headers=headers, media_type=mime + "; charset=utf-8")


async def static_file(request: Request):
    # Read on every request so edits to web/dist show up without a restart.
    name, mime = STATIC[request.url.path]
    return _cached(request, (ASSETS / name).read_bytes(), mime)


@functools.cache
def _plotly_bytes() -> bytes:
    return get_plotlyjs().encode()


async def plotly_js(request: Request):
    return _cached(request, _plotly_bytes(), "text/javascript")


async def healthz(request: Request):
    return PlainTextResponse("ok")


# --- hosted sign-in and OAuth consent ------------------------------------------------------

PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>{title} · Trio</title>
<style>
:root{{color-scheme:dark;--bg:#0f1115;--card:#181b22;--line:#2a2f3a;--text:#e8eaf0;--muted:#9aa1b1;--accent:#4f8cff;--bad:#ff6b6b}}
*{{box-sizing:border-box}}body{{margin:0;min-height:100vh;display:grid;place-items:center;background:var(--bg);
color:var(--text);font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif;padding:16px}}
main{{width:100%;max-width:380px;background:var(--card);border:1px solid var(--line);border-radius:14px;padding:28px}}
h1{{font-size:20px;margin:0 0 6px}}p{{color:var(--muted);margin:0 0 18px}}label{{display:block;font-size:14px;margin-bottom:6px}}
input{{width:100%;padding:11px 12px;border-radius:9px;border:1px solid var(--line);background:var(--bg);color:var(--text);font-size:16px}}
button{{width:100%;margin-top:14px;padding:11px;border:0;border-radius:9px;background:var(--accent);color:#fff;font-size:16px;cursor:pointer}}
button.secondary{{background:transparent;border:1px solid var(--line);color:var(--text)}}.error{{color:var(--bad)}}
code{{color:var(--text)}}
</style></head><body><main>{body}</main></body></html>"""


def page(title, body, status=200):
    return HTMLResponse(PAGE.format(title=html.escape(title), body=body), status, SECURITY_HEADERS)


def _safe_next(target: str | None) -> str:
    # Only same-site paths, never "//host" or "/\\host", to avoid open redirects.
    if not target or not target.startswith("/") or target.startswith(("//", "/\\")):
        return "/"
    return target


def _login_form(next_path, error=""):
    message = f'<p class="error">{html.escape(error)}</p>' if error else "<p>Sign in to your dashboard.</p>"
    return page("Sign in", f"""<h1>Trio</h1>{message}
<form method="post" action="/login"><input type="hidden" name="next" value="{html.escape(next_path)}">
<label for="password">Password</label>
<input id="password" name="password" type="password" autocomplete="current-password" required autofocus>
<button type="submit">Sign in</button></form>""", 401 if error else 200)


async def login(request: Request):
    from garmin_ai import auth
    if request.method == "GET":
        return _login_form(_safe_next(request.query_params.get("next")))
    form = await request.form()
    next_path = _safe_next(str(form.get("next", "")))
    wait = auth.throttle.retry_after()
    if wait:
        return _login_form(next_path, f"Too many attempts. Try again in {math.ceil(wait / 60)} min.")
    password = str(form.get("password", ""))
    if not config.APP_PASSWORD_HASH or not await run_in_threadpool(auth.verify_password, password[:256]):
        auth.throttle.failed()
        return _login_form(next_path, "That password is not correct.")
    auth.throttle.succeeded()
    response = RedirectResponse(next_path, 303)
    response.set_cookie(auth.SESSION_COOKIE, auth.create_session(), max_age=auth.SESSION_TTL_S,
                        httponly=True, secure=True, samesite="lax")
    return response


async def logout(request: Request):
    from garmin_ai import auth
    auth.end_session(request.cookies.get(auth.SESSION_COOKIE))
    response = RedirectResponse("/login", 303)
    response.delete_cookie(auth.SESSION_COOKIE)
    return response


async def oauth_approve(request: Request):
    from garmin_ai import auth
    request_id = request.query_params.get("request", "")
    if request.method == "POST":
        form = await request.form()
        request_id = str(form.get("request", ""))
    if not auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
        return RedirectResponse("/login?next=" + quote(f"/oauth/approve?request={request_id}", safe=""), 303)
    pending = auth.pending_request(request_id)
    if pending is None:
        return page("Expired", "<h1>Request expired</h1><p>Start connecting again from the app.</p>", 400)
    if request.method == "POST":
        target = auth.finish_authorization(request_id, form.get("decision") == "allow")
        return RedirectResponse(target, 303) if target else page("Expired", "<h1>Request expired</h1>", 400)
    client, params = pending
    name = html.escape(client.client_name or "An app")
    destination = html.escape(urlsplit(str(params.redirect_uri)).netloc or str(params.redirect_uri))
    return page("Allow access", f"""<h1>Allow access?</h1>
<p><strong>{name}</strong> wants to read your Garmin data and move your planned workouts. It will return to <code>{destination}</code>.</p>
<form method="post" action="/oauth/approve"><input type="hidden" name="request" value="{html.escape(request_id)}">
<button name="decision" value="allow">Allow</button>
<button class="secondary" name="decision" value="deny">Deny</button></form>""")


# --- access control -------------------------------------------------------------------------

def _local_host(host: str | None) -> bool:
    return host in (f"127.0.0.1:{config.WEB_PORT}", f"localhost:{config.WEB_PORT}")


class AccessControl:
    """Local: only loopback Host headers. Hosted: sign-in required outside PUBLIC_PATHS.
    Browser POSTs to /api must carry the custom header and a same-site Origin (CSRF)."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        request = Request(scope)
        # The routed path, not request.url.path (which is rebuilt partly from the Host header).
        path, method = scope["path"], request.method
        if config.HOSTED:
            response = self._hosted(request, path, method)
        else:
            response = self._local(request, path, method)
        if response is not None:
            return await response(scope, receive, send)
        return await self.app(scope, receive, send)

    @staticmethod
    def _local(request, path, method):
        if not _local_host(request.headers.get("host")):
            return json_response({"error": "Forbidden."}, 403)
        origins = (None, f"http://127.0.0.1:{config.WEB_PORT}", f"http://localhost:{config.WEB_PORT}")
        if method == "POST" and (request.headers.get("x-garmin-request") != "local-web"
                                 or request.headers.get("origin") not in origins):
            return json_response({"error": "Request must originate from the local app."}, 403)
        return None

    @staticmethod
    def _hosted(request, path, method):
        from garmin_ai import auth
        if path in PUBLIC_PATHS or path.startswith("/.well-known/"):
            if method == "POST" and path in ("/login", "/oauth/approve") and \
                    request.headers.get("origin") not in (None, config.PUBLIC_URL):
                return json_response({"error": "Cross-site request rejected."}, 403)
            return None
        authorization = request.headers.get("authorization", "")
        if authorization.lower().startswith("bearer ") and path.startswith("/api/"):
            # OAuth clients get read access plus moving planned workouts (TOKEN_POST_PATHS). Bearer
            # tokens are not sent automatically by browsers, so no CSRF check is needed.
            if not auth.bearer_valid(authorization[7:].strip()):
                return json_response({"error": "Invalid or expired token."}, 401)
            if method == "GET" or (method == "POST" and path in TOKEN_POST_PATHS):
                return None
            return json_response({"error": "Tokens are read-only."}, 403)
        if not auth.session_valid(request.cookies.get(auth.SESSION_COOKIE)):
            if path.startswith("/api/") or path == "/logout":
                return json_response({"error": "Sign in required."}, 401)
            return RedirectResponse("/login?next=" + quote(path, safe=""), 303)
        if method == "POST" and path.startswith("/api/") and (
                request.headers.get("x-garmin-request") != "local-web"
                or request.headers.get("origin") not in (None, config.PUBLIC_URL)):
            return json_response({"error": "Request must originate from the app."}, 403)
        return None


def create_app() -> Starlette:
    if config.HOSTED and not (config.PUBLIC_URL.startswith("https://") and config.APP_PASSWORD_HASH):
        raise SystemExit("Hosted mode needs GARMIN_PUBLIC_URL (https://...) and GARMIN_APP_PASSWORD_HASH.")
    routes = [Route(path, static_file) for path in STATIC]
    routes += [Route("/plotly.js", plotly_js), Route("/healthz", healthz)]
    routes += [Route(path, api_get) for path in GET_ACTIONS]
    routes += [Route(path, api_post, methods=["POST"]) for path in POST_ACTIONS]
    mcp_app = mcp = None
    if config.HOSTED:
        from garmin_ai.mcp_server import build_server
        mcp = build_server(hosted=True)
        routes += [Route("/login", login, methods=["GET", "POST"]), Route("/logout", logout, methods=["POST"]),
                   Route("/oauth/approve", oauth_approve, methods=["GET", "POST"])]
        mcp_app = mcp.streamable_http_app()
        routes.append(Mount("/", app=mcp_app))  # /mcp plus the OAuth endpoints and metadata

    @contextlib.asynccontextmanager
    async def lifespan(app):
        note = web_dashboard.start_auto_sync()
        print(note or "Auto-sync is off (turn it on from the Connections page)", flush=True)
        if mcp_app is None:
            yield
        else:
            async with mcp.session_manager.run():
                yield

    app = Starlette(routes=routes, lifespan=lifespan)
    if not config.HOSTED:
        # Hosted mode leaves compression to the reverse proxy (Caddy's encode gzip).
        app.add_middleware(GZipMiddleware, minimum_size=1000)
    app.add_middleware(AccessControl)
    return app


def _listening_socket(host, port) -> socket.socket:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    if os.name == "nt":
        # SO_REUSEADDR on Windows lets a second server share the port and the old one keeps
        # answering, so a restarted server looks like it is running stale code.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
    else:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((host, port))
    return sock


if __name__ == "__main__":
    import uvicorn
    from garmin_ai import db
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    with db.get_connection():
        pass
    app = create_app()
    try:
        sock = _listening_socket(config.BIND_HOST, config.WEB_PORT)
    except OSError:
        raise SystemExit(f"Port {config.WEB_PORT} is already in use. Stop the other Garmin Insights server first.")
    shown = config.PUBLIC_URL if config.HOSTED else f"http://127.0.0.1:{config.WEB_PORT}"
    print(f"Garmin Insights: {shown}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning", proxy_headers=config.HOSTED)).run(sockets=[sock])
