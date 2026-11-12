"""Single-user authentication for hosted mode.

- The dashboard signs in with one password (stored only as a scrypt hash) and gets a
  server-side session cookie.
- The remote MCP server (and later the mobile app) uses OAuth 2.1 with PKCE, implemented
  through the MCP SDK's authorization-server provider; approving a client requires the
  same password.

Everything secret (sessions, codes, tokens) is stored hashed in data/auth.db, separate
from garmin.db so data tools such as query_database can never see it.

Create the password hash with:  python -m garmin_ai.auth hash-password
"""
import base64
import hashlib
import hmac
import json
import secrets
import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    RegistrationError,
    TokenError,
    construct_redirect_uri,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

from garmin_ai import config

SESSION_COOKIE = "garmin_session"
SESSION_TTL_S = 30 * 24 * 3600
ACCESS_TTL_S = 3600
REFRESH_TTL_S = 60 * 24 * 3600
CODE_TTL_S = 300
PENDING_TTL_S = 600
MAX_CLIENTS = 25
MAX_FAILURES, FAILURE_WINDOW_S = 5, 15 * 60
OWNER = "owner"

SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_clients (client_id TEXT PRIMARY KEY, info_json TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_pending (request_id TEXT PRIMARY KEY, client_id TEXT NOT NULL,
    params_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_codes (code_hash TEXT PRIMARY KEY, data_json TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS oauth_tokens (token_hash TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL,
    data_json TEXT NOT NULL, expires_at REAL NOT NULL);
"""


# --- passwords --------------------------------------------------------------------------

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**15, r=8, p=1, maxmem=64 * 1024 * 1024)
    # ":" separators, so the value can sit in .env / Compose files without "$" escaping.
    return "scrypt:" + base64.b64encode(salt).decode() + ":" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str = "") -> bool:
    stored = stored or config.APP_PASSWORD_HASH
    try:
        scheme, salt, digest = stored.split(":")
        if scheme != "scrypt":
            return False
        expected = base64.b64decode(digest)
        actual = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt), n=2**15, r=8, p=1,
                                maxmem=64 * 1024 * 1024)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


class LoginThrottle:
    """Single user, so failures are counted globally rather than per address."""

    def __init__(self, clock=time.time):
        self._clock, self._failures, self._lock = clock, [], threading.Lock()

    def retry_after(self) -> int:
        """Seconds until another attempt is allowed (0 = allowed now)."""
        with self._lock:
            now = self._clock()
            self._failures = [t for t in self._failures if now - t < FAILURE_WINDOW_S]
            if len(self._failures) < MAX_FAILURES:
                return 0
            return int(self._failures[0] + FAILURE_WINDOW_S - now) + 1

    def failed(self):
        with self._lock:
            self._failures.append(self._clock())

    def succeeded(self):
        with self._lock:
            self._failures.clear()


throttle = LoginThrottle()


# --- storage ----------------------------------------------------------------------------

def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _connect():
    Path(config.AUTH_DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(config.AUTH_DB_PATH)
    conn.executescript(SCHEMA)
    return closing(conn)


def _purge(conn):
    now = time.time()
    for table in ("sessions", "oauth_pending", "oauth_codes", "oauth_tokens"):
        conn.execute(f"DELETE FROM {table} WHERE expires_at < ?", (now,))


# --- dashboard sessions -----------------------------------------------------------------

def create_session() -> str:
    token = secrets.token_urlsafe(32)
    with _connect() as conn, conn:
        _purge(conn)
        conn.execute("INSERT INTO sessions VALUES (?, ?)", (_hash(token), time.time() + SESSION_TTL_S))
    return token


def session_valid(token: str | None) -> bool:
    if not token:
        return False
    with _connect() as conn:
        row = conn.execute("SELECT expires_at FROM sessions WHERE token_hash = ?", (_hash(token),)).fetchone()
    return bool(row) and row[0] > time.time()


def end_session(token: str | None):
    if token:
        with _connect() as conn, conn:
            conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_hash(token),))


def bearer_valid(token: str | None) -> bool:
    """An OAuth access token, as used by the mobile app on /api routes."""
    return bool(token) and _load_token(token, "access") is not None


# --- OAuth ------------------------------------------------------------------------------

def _load_token(token: str, kind: str) -> dict | None:
    with _connect() as conn:
        row = conn.execute("SELECT data_json, expires_at FROM oauth_tokens WHERE token_hash = ? AND kind = ?",
                           (_hash(token), kind)).fetchone()
    if not row or row[1] < time.time():
        return None
    return json.loads(row[0])


def pending_request(request_id: str) -> tuple[OAuthClientInformationFull, AuthorizationParams] | None:
    with _connect() as conn:
        row = conn.execute("SELECT client_id, params_json, expires_at FROM oauth_pending WHERE request_id = ?",
                           (request_id,)).fetchone()
        if not row or row[2] < time.time():
            return None
        client = conn.execute("SELECT info_json FROM oauth_clients WHERE client_id = ?", (row[0],)).fetchone()
    if not client:
        return None
    return (OAuthClientInformationFull.model_validate_json(client[0]),
            AuthorizationParams.model_validate_json(row[1]))


def finish_authorization(request_id: str, approved: bool) -> str | None:
    """Consume a pending authorization; returns the redirect back to the client."""
    pending = pending_request(request_id)
    with _connect() as conn, conn:
        conn.execute("DELETE FROM oauth_pending WHERE request_id = ?", (request_id,))
    if pending is None:
        return None
    client, params = pending
    if not approved:
        return construct_redirect_uri(str(params.redirect_uri), error="access_denied", state=params.state)
    code = secrets.token_urlsafe(32)
    data = AuthorizationCode(
        code="", scopes=params.scopes or [], expires_at=time.time() + CODE_TTL_S, client_id=client.client_id,
        code_challenge=params.code_challenge, redirect_uri=params.redirect_uri,
        redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly, resource=params.resource,
        subject=OWNER)
    with _connect() as conn, conn:
        conn.execute("INSERT INTO oauth_codes VALUES (?, ?, ?)",
                     (_hash(code), data.model_dump_json(), data.expires_at))
    return construct_redirect_uri(str(params.redirect_uri), code=code, state=params.state)


class OAuthProvider:
    """MCP SDK authorization-server provider backed by auth.db."""

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        with _connect() as conn:
            row = conn.execute("SELECT info_json FROM oauth_clients WHERE client_id = ?", (client_id,)).fetchone()
        return OAuthClientInformationFull.model_validate_json(row[0]) if row else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        with _connect() as conn, conn:
            if conn.execute("SELECT COUNT(*) FROM oauth_clients").fetchone()[0] >= MAX_CLIENTS:
                raise RegistrationError("invalid_client_metadata", "Too many registered clients.")
            conn.execute("INSERT INTO oauth_clients VALUES (?, ?, ?)",
                         (client_info.client_id, client_info.model_dump_json(), time.time()))

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        request_id = secrets.token_urlsafe(24)
        with _connect() as conn, conn:
            _purge(conn)
            conn.execute("INSERT INTO oauth_pending VALUES (?, ?, ?, ?)",
                         (request_id, client.client_id, params.model_dump_json(), time.time() + PENDING_TTL_S))
        return f"{config.PUBLIC_URL}/oauth/approve?request={request_id}"

    async def load_authorization_code(self, client, authorization_code: str) -> AuthorizationCode | None:
        with _connect() as conn:
            row = conn.execute("SELECT data_json FROM oauth_codes WHERE code_hash = ?",
                               (_hash(authorization_code),)).fetchone()
        if not row:
            return None
        code = AuthorizationCode.model_validate_json(row[0])
        if code.client_id != client.client_id:
            return None
        return code.model_copy(update={"code": authorization_code})

    def _issue(self, client_id: str, scopes: list[str], resource: str | None, grant_id: str | None = None) -> OAuthToken:
        grant_id = grant_id or secrets.token_urlsafe(16)
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        now = int(time.time())
        rows = [
            ("access", access, AccessToken(token="", client_id=client_id, scopes=scopes, resource=resource,
                                           expires_at=now + ACCESS_TTL_S, subject=OWNER)),
            ("refresh", refresh, RefreshToken(token="", client_id=client_id, scopes=scopes, resource=resource,
                                              expires_at=now + REFRESH_TTL_S, subject=OWNER)),
        ]
        with _connect() as conn, conn:
            _purge(conn)
            for kind, token, data in rows:
                conn.execute("INSERT INTO oauth_tokens VALUES (?, ?, ?, ?, ?)",
                             (_hash(token), kind, grant_id, data.model_dump_json(), data.expires_at))
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL_S,
                          refresh_token=refresh, scope=" ".join(scopes) or None)

    async def exchange_authorization_code(self, client, authorization_code: AuthorizationCode) -> OAuthToken:
        with _connect() as conn, conn:
            deleted = conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?",
                                   (_hash(authorization_code.code),)).rowcount
        if not deleted:  # codes are single-use
            raise TokenError("invalid_grant", "Authorization code already used.")
        return self._issue(client.client_id, authorization_code.scopes, authorization_code.resource)

    async def load_refresh_token(self, client, refresh_token: str) -> RefreshToken | None:
        data = _load_token(refresh_token, "refresh")
        if data is None or data["client_id"] != client.client_id:
            return None
        return RefreshToken.model_validate({**data, "token": refresh_token})

    async def exchange_refresh_token(self, client, refresh_token: RefreshToken, scopes: list[str]) -> OAuthToken:
        with _connect() as conn, conn:
            row = conn.execute("SELECT grant_id FROM oauth_tokens WHERE token_hash = ? AND kind = 'refresh'",
                               (_hash(refresh_token.token),)).fetchone()
            if not row:
                raise TokenError("invalid_grant", "Refresh token is no longer valid.")
            # Rotate: the old access and refresh tokens of this grant stop working.
            conn.execute("DELETE FROM oauth_tokens WHERE grant_id = ?", (row[0],))
        return self._issue(client.client_id, scopes or refresh_token.scopes, refresh_token.resource, row[0])

    async def load_access_token(self, token: str) -> AccessToken | None:
        data = _load_token(token, "access")
        return AccessToken.model_validate({**data, "token": token}) if data else None

    async def revoke_token(self, token) -> None:
        with _connect() as conn, conn:
            row = conn.execute("SELECT grant_id FROM oauth_tokens WHERE token_hash = ?", (_hash(token.token),)).fetchone()
            if row:
                conn.execute("DELETE FROM oauth_tokens WHERE grant_id = ?", (row[0],))


def revoke_all():
    """Sign out everywhere: every dashboard session and every OAuth grant."""
    with _connect() as conn, conn:
        conn.execute("DELETE FROM sessions")
        conn.execute("DELETE FROM oauth_tokens")
        conn.execute("DELETE FROM oauth_codes")


if __name__ == "__main__":
    import getpass
    import sys

    if sys.argv[1:] != ["hash-password"]:
        raise SystemExit("Usage: python -m garmin_ai.auth hash-password")
    first = getpass.getpass("New dashboard password: ")
    if len(first) < 12:
        raise SystemExit("Use at least 12 characters.")
    if getpass.getpass("Repeat it: ") != first:
        raise SystemExit("The passwords did not match.")
    print("Set this as GARMIN_APP_PASSWORD_HASH:\n" + hash_password(first))
