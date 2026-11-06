import os
import subprocess
import shutil
import time
import json
import re
import tempfile
from pathlib import Path
from datetime import datetime

import requests

BASE = Path(__file__).parent
PENDING = BASE / "tasks/pending"
IN_PROGRESS = BASE / "tasks/in_progress"
COMPLETED = BASE / "tasks/completed"
FAILED = BASE / "tasks/failed"
NEEDS_APPROVAL = BASE / "tasks/needs_approval"
LOG_FILE = BASE / "orchestrator.log"
RESULTS_FILE = BASE / "task_results.jsonl"

CLAUDE_BIN = "/home/ubuntu/.local/bin/claude"
CODEX_BIN = "/usr/bin/codex"
AGY_BIN = "/home/ubuntu/.local/bin/agy"

OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

QUOTA_ERROR_PATTERNS = re.compile(
    r"usage limit|rate limit|rate.limited|quota|too many requests|"
    r"429|exceeded.*limit|resets at|please try again later|capacity",
    re.IGNORECASE,
)

# Fail-safe keyword net for destructive operations. If either this or the
# LLM check flags a task, it is routed to needs_approval — safety checks
# should never rely solely on a 3B model's judgment.
DESTRUCTIVE_PATTERNS = re.compile(
    r"\brm\s+-rf\b|\brm\s+-r\b|\bdelete\b|\bdrop\s+table\b|\bdrop\s+database\b|"
    r"\bshutdown\b|\breboot\b|\bmkfs\b|\bdd\s+if=|\bsudo\b|\bformat\b|"
    r"\bforce.push\b|\bgit\s+push\s+.*--force\b|\bchmod\s+777\b|\bkill\b|"
    r"\btruncate\b|\bwipe\b|\buninstall\b|\bfactory\s+reset\b|"
    r"\biptables\b|\bufw\b|\bsystemctl\s+(stop|disable|mask)\b",
    re.IGNORECASE,
)

for d in (PENDING, IN_PROGRESS, COMPLETED, FAILED, NEEDS_APPROVAL):
    d.mkdir(parents=True, exist_ok=True)


def log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    line = f"[{timestamp}] {message}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def record_result(task_name, task_content, status, summary, worker=None):
    entry = {
        "task": task_name,
        "instruction": task_content,
        "status": status,
        "summary": summary,
        "worker": worker,
        "timestamp": datetime.now().isoformat(),
    }
    with open(RESULTS_FILE, "a") as f:
        f.write(json.dumps(entry) + "\n")


def ollama_generate(prompt, timeout=60):
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=timeout,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        log(f"Ollama call failed: {e}")
        return None


def classify_outcome_keyword(summary_text):
    lower = summary_text.lower()
    skip_signals = ["doesn't currently", "no change needed", "not applicable",
                     "already handles", "can't crash", "n/a", "not necessary",
                     "no action needed", "nothing to change"]
    if any(signal in lower for signal in skip_signals):
        return "skipped"
    return "done"


def classify_outcome(task_content, summary_text):
    prompt = (
        "You are classifying the outcome of an automated coding task.\n"
        f"Task instruction: {task_content}\n"
        f"Agent's summary of what happened: {summary_text}\n\n"
        "Classify the outcome as exactly one word: done, skipped, or failed.\n"
        "- done: the task was actually carried out / the requested change was made.\n"
        "- skipped: no change was needed or the task did not apply.\n"
        "- failed: the agent tried but could not complete the task, or hit an error.\n"
        "Answer with only one word."
    )
    response = ollama_generate(prompt)
    if response:
        word = response.strip().lower().split()[0].strip(".,!\"'") if response.strip() else ""
        if word in ("done", "skipped", "failed"):
            return word
        log(f"Ollama classify_outcome gave unparseable response: {response!r}, falling back to keyword match")
    return classify_outcome_keyword(summary_text)


def is_destructive(task_content):
    if DESTRUCTIVE_PATTERNS.search(task_content):
        return True, "matched destructive keyword pattern"

    prompt = (
        "You are a safety gate for an automated agent that is only allowed to "
        "read files, edit files, and create files.\n"
        f"Instruction: {task_content}\n\n"
        "Does this instruction involve deleting files, running destructive or "
        "irreversible shell commands, modifying system configuration, or any "
        "other irreversible change? Answer with only one word: YES or NO."
    )
    response = ollama_generate(prompt)
    if response is None:
        # Ollama unreachable — fail safe and require human approval.
        return True, "ollama safety check unavailable, failing safe"
    answer = response.strip().upper()
    if answer.startswith("YES"):
        return True, "ollama safety check flagged this instruction as destructive"
    return False, None


def is_quota_error(stderr_text):
    return bool(QUOTA_ERROR_PATTERNS.search(stderr_text or ""))


def run_claude(task_content):
    # Same env gap fixed in jarvis_respond.py's _claude_subprocess_env:
    # this systemd unit sets PATH but never CLAUDE_CODE_OAUTH_TOKEN, so
    # `claude` would fail auth ("OAuth session expired") the moment this
    # dormant service is reactivated. Opportunistic one-line reuse of
    # the same fix, not a new mechanism.
    env = os.environ.copy()
    try:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = (Path.home() / ".claude_oauth_token").read_text().strip()
    except OSError:
        pass
    result = subprocess.run(
        [CLAUDE_BIN, "--allowedTools", "Read,Edit", "-p", task_content],
        capture_output=True, text=True, timeout=600, env=env,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def run_codex(task_content):
    with tempfile.NamedTemporaryFile(mode="r", suffix=".txt", delete=False) as tmp:
        out_path = tmp.name
    try:
        result = subprocess.run(
            [CODEX_BIN, "exec", "--skip-git-repo-check",
             "-o", out_path, task_content],
            capture_output=True, text=True, timeout=600, cwd=BASE,
        )
        last_message = ""
        try:
            last_message = Path(out_path).read_text().strip()
        except Exception:
            pass
        summary = last_message or result.stdout.strip()
        return result.returncode == 0, summary, result.stderr.strip()
    finally:
        Path(out_path).unlink(missing_ok=True)


def run_antigravity(task_content):
    result = subprocess.run(
        [AGY_BIN, "-p", task_content, "--mode", "accept-edits"],
        capture_output=True, text=True, timeout=600, cwd=BASE,
    )
    return result.returncode == 0, result.stdout.strip(), result.stderr.strip()


def execute_with_fallback(task_content):
    """Try claude, falling back to codex (on quota errors) then antigravity
    (on any codex failure). Returns (worker, success, summary, error)."""
    ok, out, err = run_claude(task_content)
    if ok:
        return "claude", True, out, None
    if not is_quota_error(err):
        return "claude", False, out, err
    log("claude appears rate-limited/quota-exhausted, falling back to codex")

    ok, out, err = run_codex(task_content)
    if ok:
        return "codex", True, out, None
    log(f"codex fallback failed ({err[:200]}), falling back to antigravity")

    ok, out, err = run_antigravity(task_content)
    if ok:
        return "antigravity", True, out, None
    return "antigravity", False, out, err


def git_commit(task_name, task_content, outcome):
    try:
        subprocess.run(["git", "add", "-A"], cwd=BASE, capture_output=True, timeout=30)
        commit_msg = f"[{outcome}] {task_name}: {task_content[:60]}"
        result = subprocess.run(
            ["git", "commit", "-m", commit_msg],
            cwd=BASE, capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            log(f"Git commit created: {commit_msg}")
        else:
            log(f"Git commit skipped (likely no changes): {task_name}")
    except Exception as e:
        log(f"Git commit failed: {e}")


def process_task(task_path: Path):
    task_content = task_path.read_text().strip()
    in_progress_path = IN_PROGRESS / task_path.name
    shutil.move(str(task_path), str(in_progress_path))
    log(f"Started: {task_path.name} — {task_content}")

    destructive, reason = is_destructive(task_content)
    if destructive:
        approval_path = NEEDS_APPROVAL / task_path.name
        shutil.move(str(in_progress_path), str(approval_path))
        log(f"NEEDS_APPROVAL: {task_path.name} — {reason}")
        record_result(task_path.name, task_content, "needs_approval", reason, worker=None)
        return

    worker, success, output, error = execute_with_fallback(task_content)

    if success:
        outcome = classify_outcome(task_content, output)
    else:
        outcome = "failed"
        output = error

    if outcome == "failed":
        shutil.move(str(in_progress_path), str(FAILED / task_path.name))
    else:
        shutil.move(str(in_progress_path), str(COMPLETED / task_path.name))

    log(f"{outcome.upper()} ({worker}): {task_path.name} — {output}")
    record_result(task_path.name, task_content, outcome, output, worker=worker)
    git_commit(task_path.name, task_content, outcome)


def watch_loop():
    log("Orchestrator started. Watching tasks/pending...")
    while True:
        pending_tasks = list(PENDING.glob("*.txt"))
        for task_path in pending_tasks:
            process_task(task_path)
        time.sleep(5)


if __name__ == "__main__":
    watch_loop()
