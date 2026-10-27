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
# not to — strip the asterisks, keep the word. Asterisks only, not
# underscores: underscores are also valid emphasis syntax in markdown, but
# stripping them turned out to actively corrupt real content once RAG
# started answering technical questions — "CMAKE_BUILD_TYPE" has a
# `_BUILD_` substring that looks exactly like underscore-emphasis to a
# generic regex, and got mangled to "CMAKEBUILDTYPE". Underscore-emphasis
# from Groq was never actually observed (the asterisk case was), so this
# narrows to the case that's real without breaking the case that's common
# in exactly the kind of content RAG retrieves (flags, env vars, code).
_MD_EMPHASIS_RE = re.compile(r"(\*{1,2})([^*]+)\1")


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

# The server's regular `claude auth login` OAuth session expired mid-
# session (reported live: "Failed to authenticate: OAuth session expired
# and could not be refreshed", independently reproduced with a plain
# `claude -p` call outside any of this code). Replaced with a long-lived
# token from `claude setup-token` (1 year, vs. a regular session that can
# expire without warning) — but that mechanism needs an explicit
# CLAUDE_CODE_OAUTH_TOKEN env var, it doesn't touch the stored-credentials
# file `claude auth login` uses. Read fresh from a chmod-600 file on the
# server on every call (never embedded here, never transmitted again from
# this Mac) — same secrets-handling pattern as the SSH key itself.
REMOTE_OAUTH_TOKEN_FILE = os.environ.get("JARVIS_REMOTE_OAUTH_TOKEN_FILE", "~/.claude_oauth_token")

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
    "force_reasoning=d.get('force_reasoning', False), "
    "force_simple=d.get('force_simple', False), "
    "allow_background=d.get('allow_background', True)))"
) % REMOTE_JARVIS_DIR

_REMOTE_CMD = (
    f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; '
    f'export CLAUDE_CODE_OAUTH_TOKEN="$(cat {REMOTE_OAUTH_TOKEN_FILE} 2>/dev/null)"; '
    f'python3 -c "{_REMOTE_PY}"'
)

# "give me an update" / "how's the task going" — reads real status from a
# background task (see jarvis_respond.start_background_task /
# check_task_status), not a guess. Longer SSH timeout than the usual
# calls isn't needed here — check_task_status itself is fast (reads a
# log file, one short Groq summarization call), it's only the task it's
# reporting ON that might be long-running.
_TASK_STATUS_REMOTE_PY = (
    "import sys, json; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "d = json.loads(sys.stdin.read()); "
    "print(jr.check_task_status(task_id=d.get('task_id'), session_id=d.get('session_id')))"
) % REMOTE_JARVIS_DIR
_TASK_STATUS_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_TASK_STATUS_REMOTE_PY}"'

# Local Claude (local_claude.py) runs entirely on this Mac and never talks
# to the server — so without this, local-tier turns are invisible to the
# fast/cloud tiers' shared memory. Confirmed live as a real bug, not
# theoretical: Groq confidently claimed "memory is shared between the
# tiers" and then had zero record of a real local-tier coding session when
# actually asked. This pushes the turn into the same shared transcript
# jarvis_respond.py's record_external_turn() reads/writes, fire-and-forget
# from always_on.py right after speaking starts — no reason to make the
# user wait on this SSH call before hearing the answer.
_RECORD_REMOTE_PY = (
    "import sys, json; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "d = json.loads(sys.stdin.read()); "
    "jr.record_external_turn(d['session_id'], d['user_input'], d['answer'], d['route'])"
) % REMOTE_JARVIS_DIR
_RECORD_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_RECORD_REMOTE_PY}"'

# RAG over knowledge/ (rag.py, server-side) — TF-IDF retrieval, not
# neural embeddings (this server's Python 3.8 can't get a compatible
# onnxruntime for fastembed; TF-IDF is genuinely adequate at this
# corpus's small scale, not a compromise nobody would notice).
_RAG_QUERY_REMOTE_PY = (
    "import sys; sys.path.insert(0, %r); "
    "import rag; "
    "print(rag.answer_from_knowledge(sys.stdin.read()))"
) % REMOTE_JARVIS_DIR
_RAG_QUERY_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_RAG_QUERY_REMOTE_PY}"'

_RAG_BUILD_REMOTE_PY = (
    "import sys; sys.path.insert(0, %r); "
    "import rag; "
    "print(rag.build_index())"
) % REMOTE_JARVIS_DIR
_RAG_BUILD_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_RAG_BUILD_REMOTE_PY}"'

KNOWLEDGE_DIR = Path(__file__).parent.parent / "knowledge"
KNOWLEDGE_DIR.mkdir(exist_ok=True)
REMOTE_KNOWLEDGE_DIR = "/home/ubuntu/jarvis-workspace/knowledge"


def ask_knowledge_base(query: str, timeout: int = 30) -> str:
    """Retrieval-grounded answer from knowledge/ — never raises; returns
    a spoken-safe error message instead, same pattern as ask_jarvis."""
    if not query or not query.strip():
        return "I didn't catch a question for the knowledge base."
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _RAG_QUERY_REMOTE_CMD,
            ],
            input=query, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "The knowledge base search didn't respond in time."
    except Exception as e:
        return f"I couldn't reach the knowledge base: {e}"
    if result.returncode != 0:
        return "Something went wrong searching the knowledge base."
    return _clean(result.stdout) or "I got an empty result from the knowledge base."


def sync_and_reindex_knowledge(timeout: int = 60) -> str:
    """Uploads every local knowledge/ file to the server and rebuilds the
    index — the "update my knowledge base" command, so newly dropped
    files (matching the inbox/ drop-folder convention) actually become
    searchable, not just sitting there unused."""
    files = [f for f in KNOWLEDGE_DIR.iterdir() if f.is_file() and not f.name.startswith(".")]
    if not files:
        return "There's nothing in the local knowledge folder to sync, sir."
    try:
        # Wipe + recreate before uploading, not just mkdir -p — plain scp
        # only ever adds/overwrites files, so a document deleted locally
        # used to stay on the remote (and in the index) forever, with no
        # way to tell it apart from a real current document. The local
        # knowledge/ set is always small, so a full wipe-and-reupload
        # each sync is cheap and guarantees the remote exactly mirrors
        # what's actually here right now.
        subprocess.run(
            [
                "ssh", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", f"rm -rf {REMOTE_KNOWLEDGE_DIR} && mkdir -p {REMOTE_KNOWLEDGE_DIR}",
            ],
            check=True, capture_output=True, timeout=15,
        )
        subprocess.run(
            [
                "scp", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS,
                *[str(f) for f in files],
                f"{SSH_USER}@{SSH_HOST}:{REMOTE_KNOWLEDGE_DIR}/",
            ],
            check=True, capture_output=True, timeout=timeout,
        )
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _RAG_BUILD_REMOTE_CMD,
            ],
            capture_output=True, text=True, timeout=timeout,
        )
        count = result.stdout.strip()
        return f"Knowledge base updated, sir — {len(files)} file(s) synced, {count} chunks indexed."
    except subprocess.CalledProcessError as e:
        return f"I couldn't sync the knowledge base: {e}"
    except Exception as e:
        return f"Something went wrong updating the knowledge base: {e}"


PERSONA_DIR = Path(__file__).parent.parent / "persona"
PERSONA_DIR.mkdir(exist_ok=True)
REMOTE_PERSONA_FILE = "/home/ubuntu/.jarvis_persona_docs.md"

# Deliberately NOT the RAG path — persona/style documents need to shape
# EVERY response, not just ones where the user explicitly asks to search
# a knowledge base. Concatenated into one plain file and read by
# jarvis_respond.py's _persona_docs_text() on every single request, same
# mechanism as the learned-lessons injection (_learned_lessons_text) —
# proven pattern for "standing content every turn should see," not a
# new one invented for this.
_PERSONA_SYNC_REMOTE_PY = (
    "open(%r, 'w').write(__import__('sys').stdin.read())"
) % REMOTE_PERSONA_FILE
_PERSONA_SYNC_REMOTE_CMD = f'python3 -c "{_PERSONA_SYNC_REMOTE_PY}"'


def sync_persona_docs(timeout: int = 30) -> str:
    """Concatenates every file in persona/ and pushes it to the server as
    one plain file, always included in every request going forward —
    the "update my persona" command."""
    files = sorted(f for f in PERSONA_DIR.iterdir() if f.is_file() and not f.name.startswith("."))
    if not files:
        return "There's nothing in the local persona folder to sync, sir."
    combined = "\n\n".join(f.read_text(errors="ignore") for f in files)
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY, *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _PERSONA_SYNC_REMOTE_CMD,
            ],
            input=combined, capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode != 0:
            return "I couldn't sync the persona documents, sir."
        return f"Persona updated, sir — {len(files)} document(s) synced, effective immediately."
    except Exception as e:
        return f"Something went wrong updating the persona: {e}"


def check_task_status(session_id: str = None, task_id: str = None, timeout: int = 20) -> str:
    """The "give me an update" / "how's the task going" voice command —
    reads real status from the server (jarvis_respond.check_task_status),
    not a guess. Omit task_id to get the most recent background task for
    this session (or the most recent one at all, if none matches)."""
    payload = json.dumps({"session_id": session_id, "task_id": task_id})
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _TASK_STATUS_REMOTE_CMD,
            ],
            input=payload, capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return "Checking on the task didn't respond in time, sir."
    except Exception as e:
        return f"I couldn't check the task status: {e}"
    if result.returncode != 0:
        return "Something went wrong checking the task status."
    return _clean(result.stdout) or "No status to report, sir."


# Lets local_claude.py (runs entirely on this Mac) see what the fast/cloud
# tiers said earlier in the SAME conversation before it ran — the mirror
# image of escalate_to_claude's own non_cloud_turns prefix server-side.
# Just reads the already-stored shared transcript, no `claude` CLI
# involved, so no OAuth token needed here.
_CROSS_TIER_CONTEXT_REMOTE_PY = (
    "import sys; sys.path.insert(0, %r); "
    "import jarvis_respond as jr; "
    "print(jr.get_cross_tier_context_text(sys.stdin.read().strip()))"
) % REMOTE_JARVIS_DIR
_CROSS_TIER_CONTEXT_REMOTE_CMD = f'export PATH="{REMOTE_EXTRA_PATH}:$PATH"; python3 -c "{_CROSS_TIER_CONTEXT_REMOTE_PY}"'


def fetch_cross_tier_context(session_id: str, timeout: int = 10) -> str:
    """Returns a 'Recent conversation so far: ...' prefix of whatever the
    fast/cloud tiers said earlier in this conversation, or '' on any
    failure (never raises — the caller should treat a failure here the
    same as "nothing to add," not block the local turn on it)."""
    if not session_id:
        return ""
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _CROSS_TIER_CONTEXT_REMOTE_CMD,
            ],
            input=session_id, capture_output=True, text=True, timeout=timeout,
        )
    except Exception:
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def record_local_turn(session_id: str, user_input: str, answer: str, route: str = "local", timeout: int = 15) -> bool:
    """Syncs one local-tier turn into the server's shared transcript.
    Returns False (never raises) on any failure — this is best-effort
    memory sync, not something that should ever break a voice turn."""
    if not session_id:
        return False
    payload = json.dumps({"session_id": session_id, "user_input": user_input, "answer": answer, "route": route})
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", _RECORD_REMOTE_CMD,
            ],
            input=payload, capture_output=True, text=True, timeout=timeout,
        )
        return result.returncode == 0
    except Exception:
        return False

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


def ask_jarvis(
    query_text: str,
    session_id: str = None,
    force_reasoning: bool = False,
    force_simple: bool = False,
    allow_background: bool = True,
    timeout: int = 90,
) -> str:
    """Send query_text to the server's jarvis_respond logic, return its answer.

    session_id: pass the SAME uuid across every turn of one voice
    conversation (see always_on.py) so Claude actually remembers earlier
    turns instead of answering each one in isolation. Omit for a one-off,
    stateless query (e.g. the push-to-talk script).

    force_reasoning: skip classify() and go straight to Claude — the
    manual "activate claude" override in always_on.py, so you always know
    which model you're talking to instead of it silently switching.

    force_simple: skip classify() and go straight to the fast tier —
    used for the "read me the logs" activity-summary command, which
    embeds real log data directly in query_text and just needs a quick
    grounded summary, not a slow reasoning-tier round trip.

    allow_background: for cloud-tier (reasoning) requests, lets the
    server detach a genuinely long task into the background and report
    "still working" instead of blocking (see jarvis_respond.
    escalate_to_claude). Default True for normal voice turns. Pass False
    only when the caller needs the complete, final answer synchronously
    no matter how long it takes — learning.py's session reflection is the
    one real example; a "still working" placeholder would corrupt the
    learning log instead of recording a real reflection."""
    if not query_text or not query_text.strip():
        return "I didn't catch anything."

    payload = json.dumps({
        "query": query_text,
        "session_id": session_id,
        "force_reasoning": force_reasoning,
        "force_simple": force_simple,
        "allow_background": allow_background,
    })

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
