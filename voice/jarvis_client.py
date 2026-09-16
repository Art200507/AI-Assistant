"""Bridge to the cloud Jarvis brain (jarvis_respond.py) over SSH.

Query text is sent over stdin, never interpolated into a shell command —
so a misheard transcription can't be interpreted as shell syntax on the
remote end.
"""
import json
import os
import re
import subprocess
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")
# Claude's web-search answers often end with a "Sources: [x](url), [y](url)"
# line — fine to read on a screen, unbearable read aloud. Strip the whole
# trailing sources block, and any markdown link syntax that slips through
# elsewhere, down to just its visible text (never speak a raw URL).
_SOURCES_BLOCK_RE = re.compile(r"\n{0,2}sources?:.*$", re.IGNORECASE | re.DOTALL)
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(https?://[^\)]+\)")
_BARE_URL_RE = re.compile(r"https?://\S+")
_BACKTICK_RE = re.compile(r"`([^`]*)`")
# Groq's model uses markdown emphasis (*word*, **word**) despite being told
# not to — strip the asterisks/underscores, keep the word.
_MD_EMPHASIS_RE = re.compile(r"(\*{1,2}|_{1,2})([^*_]+)\1")


def _clean(text: str) -> str:
    text = _ANSI_RE.sub("", text)
    text = _SOURCES_BLOCK_RE.sub("", text)
    text = _MD_LINK_RE.sub(r"\1", text)
    text = _BARE_URL_RE.sub("", text)
    text = _BACKTICK_RE.sub(r"\1", text)
    text = _MD_EMPHASIS_RE.sub(r"\2", text)
    return text.strip()

SSH_HOST = os.environ.get("JARVIS_SSH_HOST", "163.192.207.84")
SSH_USER = os.environ.get("JARVIS_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("JARVIS_SSH_KEY", os.path.expanduser("~/.ssh/jarvis-oracle.key"))
REMOTE_JARVIS_DIR = os.environ.get("JARVIS_REMOTE_DIR", "/home/ubuntu/JARVIS")

# Non-interactive SSH commands don't source ~/.profile or ~/.bashrc, so the
# server's default PATH is missing /home/ubuntu/.local/bin — which is where
# `claude` actually lives there. Without this, any query that needs to
# escalate to claude -p (jarvis_respond.escalate_to_claude) fails with
# "claude: command not found", while plain lookup queries (which only need
# `ollama`, already on the default PATH) work fine — a confusing split.
REMOTE_EXTRA_PATH = os.environ.get("JARVIS_REMOTE_EXTRA_PATH", "/home/ubuntu/.local/bin")

# Runs jarvis_respond.respond() on the server, reading the query (and an
# optional per-conversation session_id, for real multi-turn memory via
# claude --resume) from stdin as JSON — never shell-quoting arbitrary
# transcribed text, and avoiding a second SSH round-trip just to pass two
# fields.
_REMOTE_PY = (
    "import sys, json; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "d = json.loads(sys.stdin.read()); "
    "print(jr.respond(d['query'], session_id=d.get('session_id'), "
    "force_reasoning=d.get('force_reasoning', False)))"
) % REMOTE_JARVIS_DIR

_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_REMOTE_PY}"'

# Reuse one SSH connection across turns instead of paying a full TCP+auth
# handshake every time (~0.44s cold vs ~0.07s over an existing connection —
# a real chunk of the "it's so slow" latency was just SSH reconnecting on
# every single question). ControlPersist keeps the master alive for 10
# minutes of inactivity so a whole back-and-forth conversation rides one
# connection.
_SSH_CM_DIR = Path.home() / ".ssh" / "cm"
_SSH_CM_DIR.mkdir(parents=True, exist_ok=True)
_SSH_MULTIPLEX_OPTS = [
    "-o", "ControlMaster=auto",
    "-o", f"ControlPath={_SSH_CM_DIR}/%r@%h:%p",
    "-o", "ControlPersist=600",
]


def ask_jarvis(query_text: str, session_id: str = None, force_reasoning: bool = False, timeout: int = 90) -> str:
    """Send query_text to the server's jarvis_respond logic, return its answer.

    session_id: pass the SAME uuid across every turn of one voice
    conversation (see always_on.py) so Claude actually remembers earlier
    turns instead of answering each one in isolation. Omit for a one-off,
    stateless query (e.g. the push-to-talk script).

    force_reasoning: skip classify() and go straight to Claude — the
    manual "activate claude" override in always_on.py, so you always know
    which model you're talking to instead of it silently switching."""
    if not query_text or not query_text.strip():
        return "I didn't catch anything."

    payload = json.dumps({"query": query_text, "session_id": session_id, "force_reasoning": force_reasoning})

    try:
        result = subprocess.run(
            [
                "ssh",
                "-i", SSH_KEY,
                "-o", "ConnectTimeout=8",
                "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}",
                _REMOTE_CMD,
            ],
            input=payload,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "The server didn't respond in time."
    except Exception as e:
        return f"I couldn't reach the server: {e}"

    if result.returncode != 0:
        err = result.stderr.strip() or "unknown error"
        return f"Something went wrong on the server side: {err[:200]}"

    answer = _clean(result.stdout)
    return answer or "The server gave me an empty response."


if __name__ == "__main__":
    import sys
    q = " ".join(sys.argv[1:]) or "how many tasks have run so far?"
    print(ask_jarvis(q))
