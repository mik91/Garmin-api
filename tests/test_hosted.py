import base64
import hashlib
import sqlite3
import tempfile
import unittest
from contextlib import ExitStack, closing
from pathlib import Path
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from starlette.testclient import TestClient

from garmin_ai import auth, coach, config, db, web_dashboard
from garmin_ai.web_server import create_app

PUBLIC = "https://garmin.example.com"
PASSWORD = "correct horse battery"
REDIRECT = "https://claude.ai/api/mcp/auth_callback"
MCP_HEADERS = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json"}
INITIALIZE = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
              "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}


class HostedTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        folder = Path(self.temp.name)
        with closing(sqlite3.connect(folder / "garmin.db")) as conn, conn:
            conn.executescript(db.SCHEMA)
        self.stack = ExitStack()
        for name, value in {"HOSTED": True, "PUBLIC_URL": PUBLIC, "APP_PASSWORD_HASH": auth.hash_password(PASSWORD),
                            "DB_PATH": str(folder / "garmin.db"), "AUTH_DB_PATH": str(folder / "auth.db")}.items():
            self.stack.enter_context(patch.object(config, name, value))
        self.stack.enter_context(patch.object(web_dashboard, "start_auto_sync", return_value=None))
        self.stack.enter_context(patch.object(auth, "throttle", auth.LoginThrottle()))
        self.client = self.stack.enter_context(TestClient(create_app(), base_url=PUBLIC, follow_redirects=False))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def sign_in(self):
        response = self.client.post("/login", data={"password": PASSWORD, "next": "/"})
        self.assertEqual(response.status_code, 303)

    def test_dashboard_requires_sign_in(self):
        self.assertEqual(self.client.get("/").headers["location"], "/login?next=%2F")
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)
        self.assertEqual(self.client.get("/api/review").status_code, 401)
        self.assertEqual(self.client.post("/login", data={"password": "wrong"}).status_code, 401)
        self.sign_in()
        self.assertEqual(self.client.get("/").status_code, 200)
        self.assertEqual(self.client.get("/api/dashboard?start=2026-10-01&end=2026-10-07").status_code, 200)

    def test_login_rejects_open_redirect_and_is_throttled(self):
        response = self.client.post("/login", data={"password": PASSWORD, "next": "//evil.example/x"})
        self.assertEqual(response.headers["location"], "/")
        self.client.cookies.clear()
        for _ in range(auth.MAX_FAILURES):
            self.client.post("/login", data={"password": "wrong"})
        response = self.client.post("/login", data={"password": PASSWORD})
        self.assertEqual(response.status_code, 401)
        self.assertIn("Too many attempts", response.text)

    def test_browser_posts_need_custom_header_and_same_origin(self):
        self.sign_in()
        with patch.object(web_dashboard, "save_context", return_value={"message": "saved"}):
            headers = {"Content-Type": "application/json", "X-Garmin-Request": "local-web"}
            self.assertEqual(self.client.post("/api/goals", content="{}", headers={**headers, "Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(self.client.post("/api/goals", content="{}", headers={"Content-Type": "application/json"}).status_code, 403)
            self.assertEqual(self.client.post("/api/goals", content="{}", headers={**headers, "Origin": PUBLIC}).status_code, 200)
        self.assertEqual(self.client.post("/login", data={"password": PASSWORD}, headers={"Origin": "https://evil.example"}).status_code, 403)

    def test_oauth_flow_protects_mcp_and_api(self):
        self.assertEqual(self.client.post("/mcp", json=INITIALIZE, headers=MCP_HEADERS).status_code, 401)
        metadata = self.client.get("/.well-known/oauth-authorization-server").json()
        self.assertEqual(metadata["authorization_endpoint"], f"{PUBLIC}/authorize")
        registration = self.client.post("/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none", "client_name": "Claude",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        self.assertEqual(registration.status_code, 201, registration.text)
        client_id = registration.json()["client_id"]

        verifier = "v" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorize = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": REDIRECT, "state": "xyz",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        approve_url = authorize.headers["location"]
        self.assertTrue(approve_url.startswith(f"{PUBLIC}/oauth/approve?request="))
        approve_path = approve_url.removeprefix(PUBLIC)
        self.assertTrue(self.client.get(approve_path).headers["location"].startswith("/login?next="))

        self.sign_in()
        self.assertIn("Claude", self.client.get(approve_path).text)
        request_id = parse_qs(urlsplit(approve_url).query)["request"][0]
        allowed = self.client.post("/oauth/approve", data={"request": request_id, "decision": "allow"})
        callback = parse_qs(urlsplit(allowed.headers["location"]).query)
        self.assertEqual(callback["state"], ["xyz"])

        exchange = {"grant_type": "authorization_code", "code": callback["code"][0], "redirect_uri": REDIRECT,
                    "client_id": client_id, "code_verifier": verifier}
        tokens = self.client.post("/token", data=exchange).json()
        self.assertEqual(self.client.post("/token", data=exchange).status_code, 400)  # single use
        self.assertEqual(self.client.post("/token", data={**exchange, "code_verifier": "x" * 64}).status_code, 400)

        self.client.cookies.clear()
        bearer = {"Authorization": f"Bearer {tokens['access_token']}"}
        self.assertEqual(self.client.post("/mcp", json=INITIALIZE, headers={**MCP_HEADERS, **bearer}).status_code, 200)
        self.assertEqual(self.client.get("/api/sync-status", headers=bearer).status_code, 200)
        self.assertEqual(self.client.get("/api/sync-status", headers={"Authorization": "Bearer nope"}).status_code, 401)
        self.assertEqual(self.client.post("/api/sync", json={"days": 1}, headers=bearer).status_code, 403)
        review = self.client.get("/api/review", headers=bearer)
        self.assertEqual((review.status_code, review.json()["review"]), (200, None))  # none stored yet
        self.assertEqual(self.client.get("/api/review", headers={"Authorization": "Bearer nope"}).status_code, 401)
        with patch.object(coach, "move_request", return_value={"date": "2026-11-20"}) as move:
            moved = self.client.post("/api/plan/move", json={"schedule_id": "1", "date": "2026-11-20"}, headers=bearer)
        self.assertEqual((moved.status_code, moved.json()), (200, {"date": "2026-11-20"}))
        move.assert_called_once_with({"schedule_id": "1", "date": "2026-11-20"})

        refreshed = self.client.post("/token", data={"grant_type": "refresh_token", "client_id": client_id,
                                                     "refresh_token": tokens["refresh_token"]}).json()
        self.assertNotEqual(refreshed["access_token"], tokens["access_token"])
        self.assertEqual(self.client.get("/api/sync-status", headers=bearer).status_code, 401)  # rotated
        self.assertEqual(self.client.get("/api/sync-status",
                                         headers={"Authorization": f"Bearer {refreshed['access_token']}"}).status_code, 200)

    def test_denied_authorization_returns_error_to_client(self):
        self.sign_in()
        registration = self.client.post("/register", json={
            "redirect_uris": [REDIRECT], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]}).json()
        authorize = self.client.get("/authorize", params={
            "response_type": "code", "client_id": registration["client_id"], "redirect_uri": REDIRECT,
            "state": "s", "code_challenge": "c" * 43, "code_challenge_method": "S256"})
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
        denied = self.client.post("/oauth/approve", data={"request": request_id, "decision": "deny"})
        self.assertIn("error=access_denied", denied.headers["location"])
        replay = self.client.post("/oauth/approve", data={"request": request_id, "decision": "allow"})
        self.assertEqual(replay.status_code, 400)

    def test_android_app_redirect_scheme(self):
        app_redirect = "com.example.trio://oauth/callback"
        registration = self.client.post("/register", json={
            "redirect_uris": [app_redirect], "token_endpoint_auth_method": "none", "client_name": "Android",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"]})
        self.assertEqual(registration.status_code, 201, registration.text)
        client_id = registration.json()["client_id"]
        verifier = "a" * 64
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        authorize = self.client.get("/authorize", params={
            "response_type": "code", "client_id": client_id, "redirect_uri": app_redirect, "state": "st",
            "code_challenge": challenge, "code_challenge_method": "S256"})
        request_id = parse_qs(urlsplit(authorize.headers["location"]).query)["request"][0]
        self.sign_in()
        allowed = self.client.post("/oauth/approve", data={"request": request_id, "decision": "allow"})
        self.assertTrue(allowed.headers["location"].startswith(app_redirect + "?"))
        code = parse_qs(urlsplit(allowed.headers["location"]).query)["code"][0]
        tokens = self.client.post("/token", data={"grant_type": "authorization_code", "code": code, "client_id": client_id,
                                                  "redirect_uri": app_redirect, "code_verifier": verifier})
        self.assertEqual(tokens.status_code, 200, tokens.text)

    def test_password_hash_round_trip(self):
        stored = auth.hash_password("secret value")
        self.assertTrue(auth.verify_password("secret value", stored))
        self.assertFalse(auth.verify_password("other", stored))
        self.assertFalse(auth.verify_password("x", "not-a-hash"))


if __name__ == "__main__":
    unittest.main()
