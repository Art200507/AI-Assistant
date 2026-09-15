"""Bridge to the cloud Jarvis brain (jarvis_respond.py) over SSH.

Query text is sent over stdin, never interpolated into a shell command —
so a misheard transcription can't be interpreted as shell syntax on the
remote end.
"""
import os
import re
import subprocess

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]|\r")


def _clean(text: str) -> str:
    return _ANSI_RE.sub("", text).strip()

SSH_HOST = os.environ.get("JARVIS_SSH_HOST", "163.192.207.84")
SSH_USER = os.environ.get("JARVIS_SSH_USER", "ubuntu")
SSH_KEY = os.environ.get("JARVIS_SSH_KEY", os.path.expanduser("~/.ssh/jarvis-oracle.key"))
REMOTE_JARVIS_DIR = os.environ.get("JARVIS_REMOTE_DIR", "/home/ubuntu/JARVIS")

# Runs jarvis_respond.respond() on the server, reading the query from stdin
# so we never have to shell-quote arbitrary transcribed text.
_REMOTE_PY = (
    "import sys; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "print(jr.respond(sys.stdin.read().strip()))"
) % REMOTE_JARVIS_DIR


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
                f"{SSH_USER}@{SSH_HOST}",
                f"python3 -c \"{_REMOTE_PY}\"",
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
