"""Session-end reflection: after each conversation ends (see always_on.py's
_trigger_session_reflection, called both per-conversation and as a
shutdown safety net), ask cloud Claude to honestly assess what happened —
critically, whether the user corrected anything — and record it.

Two separate files, on purpose (previously one, split after real use
showed why):
  - conversation_log.md: the raw transcript, every turn, permanent record.
    Grows without bound — that's fine, it's never read back into a
    prompt, just there if the user wants to look something up.
  - learning_log.md: ONLY the distilled summary/corrections/lessons, never
    raw conversation text. This is what actually gets synced into every
    future prompt (see jarvis_respond.py's _learned_lessons_text), so it
    has to stay small and dense — a growing pile of full transcripts in
    the live system prompt would be both expensive and would eventually
    push real corrections out of the bounded recent-entries window.

learning_log.md also carries a fixed "About Jarvis" preamble — hand-written
here, not LLM-generated each session (an LLM regenerating its own
architecture description risks drifting/hallucinating about itself over
time; this is a fact sheet, not a reflection). It's what makes Jarvis
self-aware of its own three-tier structure even in a session where no
prior lessons exist yet.

The core of "real learning, not just logging": the reflection prompt
explicitly hunts for moments where the user corrected something and
turns each one into a standalone imperative rule ("never do X — user
corrected this on <date>"), not vague prose lessons that are easy to
compress away = actual likely to change future behavior.
"""
import json
import re
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from file_bridge import ACTIVITY_LOG
from jarvis_client import ask_jarvis, SSH_KEY, SSH_USER, SSH_HOST, _SSH_MULTIPLEX_OPTS, REMOTE_JARVIS_DIR

HERE = Path(__file__).parent
CONVERSATION_LOG = HERE.parent / "conversation_log.md"
LEARNING_LOG = HERE.parent / "learning_log.md"

# always_on.py triggers reflect_on_session both at the natural end of
# each conversation AND again as a shutdown safety net — and
# current_wake_start_ts only advances at the START of the NEXT wake, not
# right after a conversation ends. A normal quit shortly after finishing
# a conversation re-reflects on the exact same activity-log window that
# conversation's own end-of-turn reflection already covered — this isn't
# a rare race, it fires on ordinary exits — producing a duplicate lesson
# entry that then gets baked into every future prompt. This sidecar
# persists the real high-water mark across both call sites (and across
# process restarts) so reflection is idempotent no matter how many times
# or from where it's triggered.
REFLECTION_WATERMARK_FILE = HERE.parent / ".last_reflected_ts"


def _load_reflection_watermark() -> float:
    try:
        return float(REFLECTION_WATERMARK_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0.0


def _save_reflection_watermark(ts: float):
    try:
        REFLECTION_WATERMARK_FILE.write_text(str(ts))
    except OSError:
        pass  # best-effort — worst case, one future call re-reflects a window, same as before this fix

# How many recent dated lesson entries get pushed into every future system
# prompt — the full learning_log.md keeps every entry ever written as a
# permanent record, but only a bounded recent slice is worth spending
# prompt tokens on every single request, forever, as the file grows over
# months. Corrections matter more than this cap running out, though — see
# _recent_digest, which keeps ALL "### Corrections" content regardless of
# how far back it is, and only caps the general summary/lessons text.
LESSONS_IN_CONTEXT = 8
MAX_CONTEXT_CHARS = 4000

ABOUT_JARVIS = (
    "Jarvis is a personal AI voice assistant with three tiers sharing one "
    "identity: a fast-response tier (Groq, openai/gpt-oss-20b) for quick "
    "questions; a cloud deep-reasoning tier (Claude, Sonnet 5, on a remote "
    "server) with real Bash/file/web/Calendar/Gmail access; and a local "
    "tier (Claude, Sonnet 5, running directly on the user's own Mac) with "
    "real Bash/file access to the user's actual machine. The user switches "
    "between tiers by voice command (e.g. 'activate Claude', 'switch to "
    "local machine', 'switch back'), decided by an AI intent classifier, "
    "not exact phrase matching. Jarvis keeps a persistent learning log "
    "across sessions — real mistakes the user corrects get turned into "
    "durable rules so they are not repeated, not just logged and "
    "forgotten. This is a standing fact about what Jarvis is, true in "
    "every session regardless of what's happened recently.\n\n"
    "Camera/biometric features (face enrollment, face recognition, "
    "vision description, the startup owner-verification gate) are "
    "currently disabled by the user's own request — off by default, not "
    "wired into any voice command right now. Do not claim or imply a "
    "biometric check has happened, is happening, or is required; if asked "
    "about identity verification, say plainly that camera features are "
    "off for now. This can change if the user re-enables them, so treat "
    "it as current state, not a permanent limitation."
)


def _session_entries(since_ts: float):
    if not ACTIVITY_LOG.exists():
        return []
    entries = []
    for line in ACTIVITY_LOG.read_text().splitlines():
        if not line.strip():
            continue
        try:
            e = json.loads(line)
        except Exception:
            continue
        if e.get("timestamp", 0) >= since_ts:
            entries.append(e)
    return entries


def _format_entries(entries):
    out = []
    for e in entries:
        if e.get("type") == "file_fix":
            out.append(f"[file fix] {e.get('file', '?')}: {e.get('summary', e.get('error', ''))}")
        else:
            tier = e.get("tier", "?")
            out.append(f"[{tier}] You: {e.get('user', '')} | Jarvis: {e.get('assistant', '')}")
    return "\n".join(out)


def _append_conversation_log(when: str, transcript: str):
    entry = f"\n## {when}\n\n{transcript}\n"
    with open(CONVERSATION_LOG, "a") as f:
        f.write(entry)


def _ensure_learning_log_preamble():
    """Writes the fixed About-Jarvis header once, if the file doesn't
    already start with it — never duplicated on repeat calls."""
    if LEARNING_LOG.exists() and LEARNING_LOG.read_text().startswith("# About Jarvis"):
        return
    preamble = f"# About Jarvis\n\n{ABOUT_JARVIS}\n"
    existing = LEARNING_LOG.read_text() if LEARNING_LOG.exists() else ""
    LEARNING_LOG.write_text(preamble + "\n" + existing)


def reflect_on_session(since_ts: float):
    """Writes the raw transcript to conversation_log.md, asks for an
    honest reflection (with real emphasis on catching corrections), writes
    ONLY that reflection to learning_log.md, then syncs a bounded recent
    digest to the server. Does nothing if nothing happened this session —
    an empty reflection isn't a lesson.

    since_ts is advanced to the persisted watermark if that's more
    recent — see REFLECTION_WATERMARK_FILE above — so a second call
    covering an already-reflected window (per-conversation end, then the
    shutdown safety net moments later) is a clean no-op, not a
    duplicate."""
    since_ts = max(since_ts, _load_reflection_watermark())
    entries = _session_entries(since_ts)
    if not entries:
        return

    transcript = _format_entries(entries)
    when = time.strftime("%Y-%m-%d %H:%M:%S")
    _append_conversation_log(when, transcript)

    instruction = (
        "This is a real transcript of one voice-assistant session that "
        "just ended (you are Jarvis, reflecting on your own performance "
        "for your own future reference — not talking to the user right "
        "now). Write exactly these three sections:\n\n"
        "### Summary\n"
        "1-2 sentences on what this session was actually about — concrete, "
        "not generic ('discussed X, did Y'), so a future you skimming many "
        "of these can tell sessions apart.\n\n"
        "### Corrections\n"
        "THE MOST IMPORTANT PART. Look specifically for any point where "
        "the user corrected you, told you something was wrong, expressed "
        "frustration, or had to redirect you after a bad answer/action. "
        "For each one, write a standalone imperative rule for your future "
        "self, not a vague observation — format: 'Never/Always <specific "
        "behavior> — corrected by the user when <what happened>.' Be "
        "concrete enough that you'd recognize the same situation "
        "happening again. If genuinely nothing was corrected this "
        "session, write exactly 'None this session' — do not invent a "
        "correction just to fill the section.\n\n"
        "### Other lessons\n"
        "Anything else worth remembering — went well, or a real mistake "
        "the user didn't explicitly correct but you can tell was wrong "
        "(e.g. a fabricated fact). Skip generic advice like 'be more "
        "careful'. If there's nothing beyond what Corrections already "
        "covered, write 'Nothing further.'\n\n"
        f"Transcript:\n{transcript}"
    )
    # allow_background=False: this needs the real, complete reflection
    # text synchronously — a "still working, sir" placeholder would get
    # written into the learning log as if it were a genuine reflection,
    # silently corrupting it instead of producing a real one.
    reflection = ask_jarvis(instruction, force_reasoning=True, allow_background=False, timeout=120)
    # Third ask_jarvis failure string ("Something went wrong on the server
    # side: ...") was missing here — the same transport-failure class as
    # the other two, just uncaught, so it could get written into the
    # learning log as if it were a genuine reflection.
    if (
        not reflection
        or reflection.startswith("I couldn't reach")
        or reflection.startswith("The server didn't")
        or reflection.startswith("Something went wrong on the server side")
    ):
        return  # don't write a broken/empty reflection as if it were real

    _ensure_learning_log_preamble()
    entry = f"\n## {when}\n\n{reflection}\n"
    with open(LEARNING_LOG, "a") as f:
        f.write(entry)

    # Bumped to "now", not to the since_ts that was passed in — activity
    # logged during this reflection's own ~seconds-to-tens-of-seconds
    # Claude round-trip would otherwise fall just before the watermark
    # and get silently skipped by the next reflection.
    _save_reflection_watermark(time.time())
    sync_lessons_now()


_CORRECTIONS_RE = re.compile(r"### Corrections\s*\n(.*?)(?=\n### |\Z)", re.DOTALL)

# Cap on the dedicated corrections block itself — the comment on
# LESSONS_IN_CONTEXT above always claimed corrections survive "regardless
# of how far back" they are, but the code actually just took the last 8
# dated sections and then blindly kept the trailing MAX_CONTEXT_CHARS of
# that — a correction from 9 sessions back, or one pushed out by a few
# verbose recent sessions, silently vanished from every future prompt
# despite the comment's promise. Corrections are rare in practice (most
# sessions log "None this session"), so this ceiling is generous; it only
# bites if corrections themselves somehow balloon, in which case keeping
# the most RECENT ones (truncate from the front) beats losing all of them.
MAX_CORRECTIONS_CHARS = 3000


def _extract_correction(section_text: str) -> str:
    """Returns the body of this section's ### Corrections block, or ''
    if there's none / it's the literal 'None this session' placeholder."""
    m = _CORRECTIONS_RE.search(section_text)
    if not m:
        return ""
    body = m.group(1).strip()
    if not body or body.lower().startswith("none this session"):
        return ""
    return body


def _recent_digest() -> str:
    """About-Jarvis preamble (always included, self-awareness shouldn't
    depend on there being recent sessions) + every real correction ever
    logged (never aged out by the recency window) + the most recent
    dated entries for general narrative context, capped by character
    count so THAT part doesn't grow the live system prompt without
    bound."""
    if not LEARNING_LOG.exists():
        return ABOUT_JARVIS
    text = LEARNING_LOG.read_text()
    # First section (before the first "\n## ") is the "# About Jarvis"
    # preamble block written by _ensure_learning_log_preamble — always
    # keep it in full, it's short and constant, not part of either cap.
    parts = text.split("\n## ")
    preamble = parts[0].strip() or f"# About Jarvis\n\n{ABOUT_JARVIS}"
    dated_sections = [s for s in parts[1:] if s.strip()]

    correction_blocks = []
    for section in dated_sections:
        date_line, _, rest = section.partition("\n")
        body = _extract_correction(rest)
        if body:
            correction_blocks.append(f"## {date_line.strip()}\n{body}")
    corrections_text = "\n\n".join(correction_blocks)
    if len(corrections_text) > MAX_CORRECTIONS_CHARS:
        corrections_text = corrections_text[-MAX_CORRECTIONS_CHARS:]

    recent = dated_sections[-LESSONS_IN_CONTEXT:]
    dated_text = "\n## ".join([""] + recent) if recent else ""
    narrative_budget = max(0, MAX_CONTEXT_CHARS - len(corrections_text))
    if len(dated_text) > narrative_budget:
        dated_text = dated_text[-narrative_budget:] if narrative_budget else ""

    out = [preamble]
    if corrections_text:
        out.append("# Corrections — never aged out, apply regardless of session recency\n\n" + corrections_text)
    if dated_text.strip():
        out.append("# Recent sessions\n" + dated_text)
    return "\n\n".join(out).strip()


def sync_lessons_now(timeout: int = 15) -> int:
    """Pushes the bounded recent digest to the server as a plain file
    jarvis_respond.py reads at request time. Returns the number of dated
    lesson entries synced (0 if none beyond the fixed preamble, or if the
    sync failed) — callers that want to confirm this actually happened
    (e.g. always_on.py at startup) can report that number. Best-effort,
    never raises."""
    digest = _recent_digest()
    count = digest.count("\n## ")
    if not digest or count == 0:
        # Still worth syncing just the About-Jarvis preamble even with
        # zero session entries — self-awareness shouldn't wait on there
        # being lessons yet — but report 0 "lessons" to the caller.
        pass
    remote_py = (
        "import sys; sys.path.insert(0, %r); "
        "open('/home/ubuntu/.jarvis_learned_lessons.md', 'w').write(sys.stdin.read())"
    ) % REMOTE_JARVIS_DIR
    try:
        result = subprocess.run(
            [
                "ssh", "-i", SSH_KEY,
                "-o", "ConnectTimeout=8", "-o", "BatchMode=yes",
                "-o", "StrictHostKeyChecking=accept-new",
                *_SSH_MULTIPLEX_OPTS,
                f"{SSH_USER}@{SSH_HOST}", f'python3 -c "{remote_py}"',
            ],
            input=digest, capture_output=True, text=True, timeout=timeout,
        )
        return count if result.returncode == 0 else 0
    except Exception:
        return 0


if __name__ == "__main__":
    since_ts = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0
    reflect_on_session(since_ts)
