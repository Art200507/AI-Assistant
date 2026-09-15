"""Shared state file so the Tauri dashboard's 3D avatar can react to what
the voice pipeline is doing in real time. Pure local file, no network,
no new port — the dashboard just polls this file on disk.
"""
import json
import time
from pathlib import Path

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
