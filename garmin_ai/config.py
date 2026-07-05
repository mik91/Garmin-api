import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent.parent

GARMIN_EMAIL = os.environ.get("GARMIN_EMAIL")
GARMIN_PASSWORD = os.environ.get("GARMIN_PASSWORD")
GARMIN_TOKENSTORE = str(PROJECT_ROOT / os.environ.get("GARMIN_TOKENSTORE", ".garmin_tokens"))

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")

DB_PATH = str(PROJECT_ROOT / os.environ.get("GARMIN_DB_PATH", "data/garmin.db"))
