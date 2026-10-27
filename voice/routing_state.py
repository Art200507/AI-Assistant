"""Local-tier counterpart to the server's route_state.json (see
jarvis_respond.py's _write_route_state). "local" mode never talks to the
server at all (local_claude.py runs entirely on this Mac), so it's the one
route the server-side state file can never see — this is the Mac's own
record of that case. mobile_sync's push is what lets the phone PWA see it
too, same reasoning as voice_state.py."""
import json
import time
from pathlib import Path

import mobile_sync

STATE_PATH = Path.home() / ".jarvis-dashboard" / "routing_state.json"


def write_state(tier: str, label: str, detail: str = ""):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "tier": tier,  # currently always "local" — the server can't see this route
        "label": label,
        "detail": detail[:200],
        "status": "dispatched",
        "timestamp": time.time(),
    }
    tmp = STATE_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload))
    tmp.replace(STATE_PATH)
    mobile_sync.push_async("routing_state", payload)
