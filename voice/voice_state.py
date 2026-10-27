"""Shared state file so the Dashboard's 3D avatar can react to what the
voice pipeline is doing in real time. The local file (read directly by
the Electron app, same machine) is still the fast path — mobile_sync's
push is purely additive, for the phone PWA, which has no filesystem
access to this Mac at all and can only find out what's happening here
through something this Mac tells the server.
"""
import json
import time
from pathlib import Path

import mobile_sync

STATE_PATH = Path.home() / ".jarvis-dashboard" / "voice_state.json"


def write_state(state: str, amplitude: float = 0.0):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "state": state,  # idle | listening | thinking | speaking
        "amplitude": max(0.0, min(1.0, amplitude)),
        "updated_at": time.time(),
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(STATE_PATH)  # atomic, avoids the dashboard reading a half-written file
    mobile_sync.push_async("voice_state", payload)
