"""Shared by voice_state.py and routing_state.py: fire-and-forget push of
Mac-local state to the mobile API's /api/mac_state, so the phone PWA (no
filesystem access to this Mac at all) can find out what's happening here.
Never raises, never blocks the caller — a dead network or a server that
isn't up yet should mean "the PWA doesn't see it," never "the voice loop
breaks."
"""
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

MOBILE_API_URL = "https://163-192-207-84.sslip.io/api/mac_state"
MOBILE_TOKEN_PATH = Path.home() / ".jarvis_mobile_api_token"
_token = None
_token_loaded = False


def _get_token():
    global _token, _token_loaded
    if not _token_loaded:
        try:
            _token = MOBILE_TOKEN_PATH.read_text().strip()
        except FileNotFoundError:
            _token = None
        _token_loaded = True
    return _token


def _push(kind: str, payload: dict):
    token = _get_token()
    if not token:
        return
    body = json.dumps({"kind": kind, "payload": payload}).encode()
    req = urllib.request.Request(
        MOBILE_API_URL, data=body, method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        urllib.request.urlopen(req, timeout=5).read()
    except (urllib.error.URLError, OSError):
        pass


def push_async(kind: str, payload: dict):
    threading.Thread(target=_push, args=(kind, payload), daemon=True).start()
