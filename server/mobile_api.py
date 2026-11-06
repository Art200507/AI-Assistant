"""HTTPS API for the JARVIS PWA — the one new thing that makes "access
jarvis from my phone, from anywhere" actually possible. Everything the
Electron Dashboard does via direct Node (SSH out, local file reads) a
browser simply cannot do — no SSH, no filesystem access — so this is the
network-reachable equivalent, running directly ON the Oracle server
(already the one machine in this whole setup with a public IP) instead of
relaying through the Mac.

Auth is a single shared-secret bearer token (see mobile_api_token.txt,
chmod 600, generated once by deploy) — not OAuth-grade, but this is a
single-user personal tool behind HTTPS, and the real line of defense is
"an attacker needs both the exact URL and this token," not public-facing
at all without it. Every route requires it; there is no unauthenticated
read, not even status.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

from flask import Flask, jsonify, request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import jarvis_respond as jr  # noqa: E402

app = Flask(__name__)

TOKEN_FILE = Path.home() / ".jarvis-mobile-api" / "token.txt"
MAC_STATE_DIR = Path.home() / ".jarvis-mobile-api" / "mac-state"
MAC_STATE_DIR.mkdir(parents=True, exist_ok=True)
MOBILE_COMMAND_FILE = Path.home() / ".jarvis-mobile-api" / "command.json"

VOICE_STATE_STALE_S = 20  # same threshold/reasoning as electron/main.cjs's fetchVoiceState


def _load_token():
    try:
        return TOKEN_FILE.read_text().strip()
    except FileNotFoundError:
        return None


_TOKEN = _load_token()


@app.before_request
def _check_auth():
    if _TOKEN is None:
        return jsonify({"ok": False, "error": "server has no token configured"}), 500
    auth = request.headers.get("Authorization", "")
    if auth != f"Bearer {_TOKEN}":
        return jsonify({"ok": False, "error": "unauthorized"}), 401


@app.route("/api/background_tasks")
def background_tasks():
    try:
        tasks = json.loads(jr.list_background_tasks())
        return jsonify({"ok": True, "error": None, "tasks": tasks})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "tasks": []})


@app.route("/api/projects_log")
def projects_log():
    try:
        content = jr.PROJECTS_LOG_FILE.read_text()
    except FileNotFoundError:
        content = ""
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "content": ""})
    return jsonify({"ok": True, "error": None, "content": content})


@app.route("/api/service_status")
def service_status():
    try:
        out = subprocess.run(
            ["systemctl", "is-active", "jarvis-orchestrator"],
            capture_output=True, text=True, timeout=10,
        )
        return jsonify({"ok": True, "active": out.stdout.strip(), "error": None})
    except Exception as e:
        return jsonify({"ok": False, "active": "unknown", "error": str(e)})


def _read_json_safe(path: Path):
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


@app.route("/api/voice_state")
def voice_state():
    # Mirrored from the Mac by voice_state.py's fire-and-forget push (see
    # that file) — this server never had this data before; it only knows
    # what the Mac has told it, same staleness logic as the Electron app
    # applies to the same underlying problem (a dead writer freezing the
    # UI on whatever state it last reported).
    parsed = _read_json_safe(MAC_STATE_DIR / "voice_state.json") or {}
    state = parsed.get("state", "idle")
    updated_at = parsed.get("updated_at", 0)
    age_s = time.time() - updated_at
    if state != "idle" and age_s > VOICE_STATE_STALE_S:
        return jsonify({"state": "idle", "amplitude": 0, "updated_at": updated_at, "stale": True})
    return jsonify({"state": state, "amplitude": parsed.get("amplitude", 0), "updated_at": updated_at})


@app.route("/api/routing_state")
def routing_state():
    # Same merge-by-timestamp logic as electron/main.cjs's fetchRoutingState
    # — local (Mac-mirrored) vs server (route_state.json, written directly
    # by respond() since this process IS the server) — whichever is newer
    # wins, since "local" mode never touches route_state.json at all.
    local = _read_json_safe(MAC_STATE_DIR / "routing_state.json") or {}
    remote = _read_json_safe(jr.ROUTE_STATE_FILE) or {}
    local_ts = local.get("timestamp", 0)
    remote_ts = remote.get("timestamp", 0)
    if local_ts > remote_ts:
        return jsonify({
            "tier": local.get("tier", ""), "label": local.get("label", ""),
            "detail": local.get("detail", ""), "status": local.get("status", ""),
            "timestamp": local_ts, "source": "local-mac",
        })
    if remote_ts > 0:
        return jsonify({
            "tier": remote.get("route", ""), "label": remote.get("label", ""),
            "detail": remote.get("preview", ""), "status": remote.get("status", ""),
            "timestamp": remote_ts, "source": "server",
        })
    return jsonify({
        "tier": "unknown", "label": "No routing activity yet", "detail": "",
        "status": "idle", "timestamp": 0, "source": "none",
    })


@app.route("/api/mac_state", methods=["POST"])
def receive_mac_state():
    """voice_state.py / routing_state.py push here (fire-and-forget) every
    time local state changes on the Mac — this is how this server, which
    has no filesystem access to the Mac, finds out about Mac-local state
    at all."""
    data = request.get_json(silent=True) or {}
    kind = data.get("kind")
    if kind not in ("voice_state", "routing_state"):
        return jsonify({"ok": False, "error": "bad kind"}), 400
    (MAC_STATE_DIR / f"{kind}.json").write_text(json.dumps(data.get("payload", {})))
    return jsonify({"ok": True})


@app.route("/api/chat", methods=["POST"])
def chat():
    """The actual answer to "even if the Mac is off, I should be able to
    reach Jarvis from anywhere" — always_on.py (and therefore the voice
    path and the activate/interrupt/keep_listening commands above) needs
    the Mac's own mic/speaker and cannot exist if that machine is off.
    This calls jr.respond() directly, in-process, same function every
    voice turn already goes through via SSH — just a text entry point
    that never touches the Mac at all, so it works regardless of whether
    that machine is even powered on.

    session_id is the PWA's own, generated client-side once and reused
    across a chat thread (see mobile-bridge.js) — deliberately separate
    from the Mac's per-process conversation_id, since a phone chat and a
    spoken conversation are genuinely different threads of conversation,
    not the same one continued."""
    data = request.get_json(silent=True) or {}
    message = (data.get("message") or "").strip()
    session_id = data.get("session_id")
    if not message:
        return jsonify({"ok": False, "error": "empty message", "answer": ""}), 400
    try:
        answer = jr.respond(message, session_id=session_id, allow_background=True)
        return jsonify({"ok": True, "error": None, "answer": answer})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e), "answer": ""})


@app.route("/api/command", methods=["POST"])
def send_command():
    """Mirrors electron/main.cjs's send_dashboard_command, but for a phone
    that can't write to the Mac's local command.json directly — writes
    here instead, and always_on.py's DashboardCommandWatcher polls this
    file over SSH (slower than its 120ms local-file poll, since this is a
    real network round trip — see always_on.py for the actual interval)
    in addition to the local one."""
    data = request.get_json(silent=True) or {}
    command = data.get("command")
    if command not in ("activate", "interrupt", "keep_listening"):
        return jsonify({"ok": False, "error": "unknown command"}), 400
    import uuid
    payload = {"command": command, "id": str(uuid.uuid4()), "ts": time.time()}
    MOBILE_COMMAND_FILE.write_text(json.dumps(payload))
    return jsonify({"ok": True})


if __name__ == "__main__":
    # Bound to localhost only — nginx terminates TLS and reverse-proxies
    # to this; this process is never directly internet-reachable.
    app.run(host="127.0.0.1", port=8787)
