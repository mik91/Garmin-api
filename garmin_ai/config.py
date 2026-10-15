import json
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GARMIN_TOKENSTORE = str(PROJECT_ROOT / os.environ.get("GARMIN_TOKENSTORE", ".garmin_tokens"))


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


GARMIN_AUTO_SYNC_ON_STARTUP = _env_bool("GARMIN_AUTO_SYNC_ON_STARTUP")
GARMIN_AUTO_SYNC_DAYS = int(os.environ.get("GARMIN_AUTO_SYNC_DAYS", "7"))
# Minutes between background syncs while the web server runs; 0 turns it off.
GARMIN_AUTO_SYNC_INTERVAL_MIN = int(os.environ.get("GARMIN_AUTO_SYNC_INTERVAL_MIN", "60"))

# AI insights run through the locally installed CLIs, so they use the user's
# Claude or ChatGPT subscription instead of a paid API key.
AI_PROVIDER = os.environ.get("AI_PROVIDER", "claude").strip().lower()
# Empty means the CLI's own default model.
AI_MODEL = os.environ.get("AI_MODEL", "").strip()
CLAUDE_CLI = os.environ.get("CLAUDE_CLI", "claude")
CODEX_CLI = os.environ.get("CODEX_CLI", "codex")
AI_CLI_TIMEOUT_S = int(os.environ.get("AI_CLI_TIMEOUT_S", "300"))

WEB_PORT = int(os.environ.get("GARMIN_WEB_PORT", "8502"))

DB_PATH = str(PROJECT_ROOT / os.environ.get("GARMIN_DB_PATH", "data/garmin.db"))

# Values saved from the web Connections page override the environment.
SETTINGS_PATH = PROJECT_ROOT / os.environ.get("GARMIN_SETTINGS_PATH", "data/settings.json")
EDITABLE_SETTINGS = {
    "AI_PROVIDER": str,
    "AI_MODEL": str,
    "GARMIN_EMAIL": str,
    "GARMIN_DISPLAY_NAME": str,
    "GARMIN_AUTO_SYNC_DAYS": int,
    "GARMIN_AUTO_SYNC_INTERVAL_MIN": int,
}
GARMIN_DISPLAY_NAME = ""


def load_settings() -> dict:
    try:
        saved = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(saved, dict):
        return {}
    return {
        key: value for key, value in saved.items()
        if key in EDITABLE_SETTINGS and isinstance(value, EDITABLE_SETTINGS[key]) and not isinstance(value, bool)
    }


def save_settings(updates: dict) -> None:
    """Persist updates to the settings file and apply them to this process."""
    unknown = set(updates) - set(EDITABLE_SETTINGS)
    if unknown:
        raise ValueError(f"Unknown settings: {', '.join(sorted(unknown))}")
    saved = {**load_settings(), **updates}
    SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    temp = SETTINGS_PATH.with_suffix(".tmp")
    temp.write_text(json.dumps(saved, indent=2), encoding="utf-8")
    temp.replace(SETTINGS_PATH)
    globals().update(updates)


globals().update(load_settings())
