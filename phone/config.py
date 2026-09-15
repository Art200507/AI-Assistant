"""Twilio phone layer config. Loads from .env if present (via python-dotenv-free
manual parsing, so we don't need an extra dependency for something this small).

ACTIVATION CHECK: is_configured() is False until real Twilio credentials are
supplied. Every entry point in this scaffold checks it and refuses to run
for real (not just "would fail" — it actively raises/exits) until then.
"""
import os
from pathlib import Path

HERE = Path(__file__).parent
ENV_FILE = HERE / ".env"


def _load_dotenv(path: Path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip())


_load_dotenv(ENV_FILE)

TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID", "")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN", "")
TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER", "")
JARVIS_OWNER_PHONE_NUMBER = os.environ.get("JARVIS_OWNER_PHONE_NUMBER", "")
PUBLIC_BASE_URL = os.environ.get("PUBLIC_BASE_URL", "")

_PLACEHOLDER_MARKERS = ("xxxxxxxx", "your_auth_token_here", "")


def is_configured() -> bool:
    values = [
        TWILIO_ACCOUNT_SID,
        TWILIO_AUTH_TOKEN,
        TWILIO_PHONE_NUMBER,
        JARVIS_OWNER_PHONE_NUMBER,
        PUBLIC_BASE_URL,
    ]
    if any(not v for v in values):
        return False
    if any(marker in v for v in values for marker in ("xxxxxxxx", "your_auth_token_here", "your-ngrok")):
        return False
    return True


def require_configured():
    if not is_configured():
        raise SystemExit(
            "Twilio is not configured yet. Copy phone/.env.example to phone/.env, "
            "sign up for Twilio (https://www.twilio.com/try-twilio), and fill in "
            "real values. This scaffold will not run against Twilio until then."
        )
