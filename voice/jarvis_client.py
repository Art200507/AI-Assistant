"""Bridge to the cloud Jarvis brain (jarvis_respond.py) over SSH.

Query text is sent over stdin, never interpolated into a shell command —
so a misheard transcription can't be interpreted as shell syntax on the
remote end.
"""
import os
import re
import subprocess
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()

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

# Runs jarvis_respond.respond() on the server, reading the query from stdin
# so we never have to shell-quote arbitrary transcribed text.
_REMOTE_PY = (
    "import sys; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "print(jr.respond(sys.stdin.read().strip()))"
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


def ask_jarvis(query_text: str, timeout: int = 90) -> str:
    """Send query_text to the server's jarvis_respond logic, return its answer."""
    if not query_text or not query_text.strip():
        return "I didn't catch anything."

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
            input=query_text,
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
