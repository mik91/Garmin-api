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

DB_PATH = str(PROJECT_ROOT / os.environ.get("GARMIN_DB_PATH", "data/garmin.db"))
