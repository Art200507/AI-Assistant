"""Lets Jarvis actually touch files on THIS Mac — the piece that was
missing before: read a local file, hand it to cloud-Claude (which has real
Bash/Read/Write access, but only on the server) to find and fix bugs, pull
the fixed version back down, apply it locally, and log what happened.

Voice-dictating exact file paths is unreliable (Whisper mangling
"/Users/atharva/Projects/...") so this uses a drop-folder instead: put a
file in inbox/, then say "check the inbox" / "fix that file". always_on.py
detects that intent locally (see _FILE_FIX_RE) and calls fix_file() instead
of the normal ask_jarvis() text round-trip.

Safety: every fix backs up the original first (backups/, timestamped) —
this never loses your original, even if the "fix" is wrong. The download
lands in a temp file and only replaces the original on a clean success.
"""
import json
import shutil
import subprocess
import time
from pathlib import Path

from jarvis_client import ask_jarvis, SSH_KEY, SSH_USER, SSH_HOST, _SSH_MULTIPLEX_OPTS

HERE = Path(__file__).parent
INBOX_DIR = HERE.parent / "inbox"
BACKUPS_DIR = HERE.parent / "backups"
ACTIVITY_LOG = HERE.parent / "activity.log"
INBOX_DIR.mkdir(exist_ok=True)
BACKUPS_DIR.mkdir(exist_ok=True)

REMOTE_INCOMING = "/home/ubuntu/jarvis-workspace/incoming"
REMOTE_FIXED = "/home/ubuntu/jarvis-workspace/fixed"

# Files this large probably aren't meant for a voice-triggered "fix my bugs"
# pass — refuse rather than silently uploading something huge/binary.
MAX_FILE_BYTES = 512 * 1024


def _ssh_run(remote_cmd: str, timeout: int = 20):
    return subprocess.run(
        ["ssh", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS, f"{SSH_USER}@{SSH_HOST}", remote_cmd],
        capture_output=True, text=True, timeout=timeout,
    )


def _scp_up(local_path: Path, remote_path: str, timeout: int = 30):
    subprocess.run(
        ["scp", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS, str(local_path), f"{SSH_USER}@{SSH_HOST}:{remote_path}"],
        check=True, capture_output=True, timeout=timeout,
    )


def _scp_down(remote_path: str, local_path: Path, timeout: int = 30):
    subprocess.run(
        ["scp", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS, f"{SSH_USER}@{SSH_HOST}:{remote_path}", str(local_path)],
        check=True, capture_output=True, timeout=timeout,
    )


def latest_inbox_file():
    files = [f for f in INBOX_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        return None
    return max(files, key=lambda f: f.stat().st_mtime)


def log_activity(entry: dict):
    entry["timestamp"] = time.time()
    entry["when"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(ACTIVITY_LOG, "a") as f:
        f.write(json.dumps(entry) + "\n")


def fix_file(local_path, session_id=None) -> str:
    """Backs up local_path, uploads it, asks Claude (real Bash/Write access,
    server-side) to find and fix bugs, downloads the result, applies it —
    and logs every step. Returns the spoken summary."""
    if local_path is None:
        return "There's nothing in the inbox, sir. Drop a file in there first."

    local_path = Path(local_path)
    if not local_path.exists():
        return f"I can't find {local_path.name}, sir."
    if local_path.stat().st_size > MAX_FILE_BYTES:
        return f"{local_path.name} is larger than I'd want to send over for a voice-triggered fix, sir — best done directly."

    backup_path = BACKUPS_DIR / f"{local_path.stem}.{int(time.time())}{local_path.suffix}"
    shutil.copy2(local_path, backup_path)

    remote_incoming = f"{REMOTE_INCOMING}/{local_path.name}"
    remote_fixed = f"{REMOTE_FIXED}/{local_path.name}"

    try:
        _ssh_run(f"mkdir -p {REMOTE_INCOMING} {REMOTE_FIXED}")
        _scp_up(local_path, remote_incoming)
    except subprocess.CalledProcessError as e:
        log_activity({"file": str(local_path), "backup": str(backup_path), "error": f"upload failed: {e}"})
        return f"I couldn't get {local_path.name} up to the server, sir."

    instruction = (
        f"Read the file at {remote_incoming}. Find and fix any real bugs you "
        f"see — don't invent problems that aren't there, and don't rewrite "
        f"style/formatting unless it's an actual bug. Write the corrected "
        f"version to {remote_fixed} (same file, just with the bugs fixed; "
        f"if you genuinely find nothing wrong, copy it there unchanged). "
        f"Then tell me, in 2-4 spoken sentences, exactly what you found and "
        f"changed — or say plainly that you found nothing wrong."
    )
    summary = ask_jarvis(instruction, session_id=session_id)

    tmp_path = local_path.with_suffix(local_path.suffix + ".jarvis-download")
    try:
        _scp_down(remote_fixed, tmp_path)
        tmp_path.replace(local_path)
    except subprocess.CalledProcessError as e:
        tmp_path.unlink(missing_ok=True)
        log_activity({
            "file": str(local_path), "backup": str(backup_path),
            "summary": summary, "error": f"download failed: {e}",
        })
        return f"I looked at {local_path.name} but couldn't retrieve the fixed version, sir. What I found: {summary}"

    log_activity({
        "file": str(local_path), "backup": str(backup_path), "summary": summary,
    })
    return summary


if __name__ == "__main__":
    import sys
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else latest_inbox_file()
    print(fix_file(target))
