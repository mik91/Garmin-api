"""Account and AI provider connections managed from the web Connections page."""
import json
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from pathlib import Path

from garmin_ai import ai_insights, config

MFA_TIMEOUT_S = 600
MODEL_PATTERN = re.compile(r"^[A-Za-z0-9._:/\[\]-]{0,100}$")
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

_lock = threading.Lock()
_pending_mfa = {}
_login_processes = {}


def _token_file() -> Path:
    path = Path(config.GARMIN_TOKENSTORE)
    return path if path.suffix == ".json" else path / "garmin_tokens.json"


def garmin_status() -> dict:
    from garmin_ai import db

    last_synced = None
    with suppress(Exception), db.get_connection() as conn:
        last_synced = db.get_sync_state(conn).get("last_synced_at")
    with _lock:
        awaiting_mfa = bool(_pending_mfa) and time.time() - _pending_mfa["started"] < MFA_TIMEOUT_S
    return {
        "connected": _token_file().exists(),
        "email": config.GARMIN_EMAIL or "",
        "display_name": config.GARMIN_DISPLAY_NAME or "",
        "last_synced_at": last_synced,
        "awaiting_mfa": awaiting_mfa,
        "auto_sync_interval_min": config.GARMIN_AUTO_SYNC_INTERVAL_MIN,
        "auto_sync_days": config.GARMIN_AUTO_SYNC_DAYS,
    }


def _run_cli(path: str, *args: str, timeout: int = 20) -> subprocess.CompletedProcess | None:
    try:
        return subprocess.run(
            [path, *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, env=ai_insights._cli_env(), timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _cli_status(provider: str) -> dict:
    command = config.CLAUDE_CLI if provider == "claude" else config.CODEX_CLI
    status = {"label": ai_insights.PROVIDERS[provider], "command": command, "installed": False,
              "version": "", "logged_in": False, "account": "", "signing_in": False}
    with _lock:
        process = _login_processes.get(provider)
        status["signing_in"] = process is not None and process.poll() is None
    path = shutil.which(command)
    if path is None:
        return status
    status["installed"] = True
    version = _run_cli(path, "--version")
    if version and version.returncode == 0:
        status["version"] = version.stdout.strip().splitlines()[0] if version.stdout.strip() else ""
    if provider == "claude":
        auth = _run_cli(path, "auth", "status")
        with suppress(AttributeError, ValueError):
            info = json.loads(auth.stdout)
            status["logged_in"] = bool(info.get("loggedIn"))
            status["account"] = info.get("email") or info.get("subscriptionType") or ""
    else:
        auth = _run_cli(path, "login", "status")
        if auth:
            text = (auth.stdout + auth.stderr).strip()
            status["logged_in"] = auth.returncode == 0 and "not logged in" not in text.lower()
            status["account"] = text.splitlines()[0] if status["logged_in"] and text else ""
    return status


def ai_status() -> dict:
    try:
        provider = ai_insights.provider_name()
    except ai_insights.AIProviderError:
        provider = ""
    with ThreadPoolExecutor(max_workers=2) as pool:
        providers = dict(zip(ai_insights.PROVIDERS, pool.map(_cli_status, ai_insights.PROVIDERS)))
    return {"provider": provider, "model": config.AI_MODEL, "providers": providers}


def status(_body=None) -> dict:
    return {"garmin": garmin_status(), "ai": ai_status()}


def save_ai(body: dict) -> dict:
    provider = body.get("provider")
    model = body.get("model", "")
    if provider not in ai_insights.PROVIDERS:
        raise ValueError("Choose Claude or Codex.")
    if not isinstance(model, str) or not MODEL_PATTERN.match(model.strip()):
        raise ValueError("Model names may only contain letters, numbers and . _ : / - [ ]")
    config.save_settings({"AI_PROVIDER": provider, "AI_MODEL": model.strip()})
    return status()


def test_ai(_body=None) -> dict:
    if not ai_insights.is_configured():
        raise ValueError(ai_insights.missing_config_message())
    started = time.monotonic()
    answer = ai_insights.ping()
    return {"answer": answer, "seconds": round(time.monotonic() - started, 1)}


def cli_login(body: dict) -> dict:
    """Start the CLI's own browser sign-in flow; the page polls status until it finishes."""
    provider = body.get("provider")
    if provider not in ai_insights.PROVIDERS:
        raise ValueError("Choose Claude or Codex.")
    command = config.CLAUDE_CLI if provider == "claude" else config.CODEX_CLI
    path = shutil.which(command)
    if path is None:
        raise ValueError(f"{ai_insights.PROVIDERS[provider]} is not installed.")
    args = ["auth", "login", "--claudeai"] if provider == "claude" else ["login"]
    with _lock:
        running = _login_processes.get(provider)
        if running is None or running.poll() is not None:
            _login_processes[provider] = subprocess.Popen(
                [path, *args], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL, env=ai_insights._cli_env(),
            )
    return {"started": True}


def save_sync(body: dict) -> dict:
    interval, days = body.get("interval_min"), body.get("days")
    for value, low, high, label in ((interval, 0, 1440, "Auto-sync interval"), (days, 1, 365, "Auto-sync days")):
        if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
            raise ValueError(f"{label} must be between {low} and {high}.")
    config.save_settings({"GARMIN_AUTO_SYNC_INTERVAL_MIN": interval, "GARMIN_AUTO_SYNC_DAYS": days})
    return status()


def _friendly(error: Exception) -> str:
    from garmin_ai import sync

    message = sync.friendly_error(error)
    if "401" in message or "authentication failed" in message.lower():
        return "Garmin rejected the email or password."
    if "mfa" in message.lower() and "invalid" in message.lower():
        return "That MFA code was not accepted. Check it and try again."
    return message.splitlines()[0][:300]


def _finish_garmin_login(client, email: str) -> dict:
    client.client.dump(config.GARMIN_TOKENSTORE)
    with suppress(Exception):
        client._load_profile_and_settings()
    name = getattr(client, "full_name", None) or getattr(client, "display_name", None) or ""
    config.save_settings({"GARMIN_EMAIL": email, "GARMIN_DISPLAY_NAME": str(name)})
    return status()


def garmin_login(body: dict) -> dict:
    from garminconnect import Garmin

    email, password = body.get("email"), body.get("password")
    if not isinstance(email, str) or not EMAIL_PATTERN.match(email.strip()):
        raise ValueError("Enter your Garmin Connect email.")
    if not isinstance(password, str) or not 0 < len(password) <= 256:
        raise ValueError("Enter your Garmin Connect password.")
    email = email.strip()
    client = Garmin(email, password, return_on_mfa=True)
    try:
        result, _ = client.login()
    except Exception as error:
        raise ValueError(_friendly(error)) from error
    if result == "needs_mfa":
        with _lock:
            _pending_mfa.clear()
            _pending_mfa.update(client=client, email=email, started=time.time())
        return {**status(), "mfa_required": True}
    return _finish_garmin_login(client, email)


def garmin_mfa(body: dict) -> dict:
    code = body.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"\d{4,10}", code.strip()):
        raise ValueError("Enter the numeric code Garmin sent you.")
    with _lock:
        pending = dict(_pending_mfa)
    if not pending or time.time() - pending["started"] > MFA_TIMEOUT_S:
        raise ValueError("The sign-in expired. Enter your email and password again.")
    try:
        pending["client"].resume_login(None, code.strip())
    except Exception as error:
        raise ValueError(_friendly(error)) from error
    with _lock:
        _pending_mfa.clear()
    return _finish_garmin_login(pending["client"], pending["email"])


def garmin_logout(_body=None) -> dict:
    with _lock:
        _pending_mfa.clear()
    token = _token_file()
    if token.exists():
        token.unlink()
    config.save_settings({"GARMIN_DISPLAY_NAME": ""})
    return status()
