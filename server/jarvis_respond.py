import fcntl
import os
import subprocess
import json
import re
import requests
import shlex
import time
import uuid
from pathlib import Path

# claude CLI needs its OAuth token and to be findable on PATH. This used
# to depend entirely on the CALLER's shell having set both up first —
# jarvis_client.py's SSH command (the voice path, Mac -> this server)
# does `export CLAUDE_CODE_OAUTH_TOKEN=...; export PATH=...` before ever
# reaching this module. That worked for voice, but this module has two
# other callers that never did either: the mobile API's systemd service
# (phone chat) and orchestrator.py — both invoke `claude` with neither
# set, so it either wasn't found on PATH at all ("claude: command not
# found", confirmed live in the phone's chat history and task logs) or
# ran with no credentials and failed authentication ("OAuth session
# expired and could not be refreshed", confirmed by direct reproduction).
# Owning this here, at the one place that actually calls `claude`, makes
# it correct regardless of who's calling — not duplicated per caller.
CLAUDE_OAUTH_TOKEN_FILE = Path.home() / ".claude_oauth_token"
CLAUDE_EXTRA_PATH = "/home/ubuntu/.local/bin"


def _claude_subprocess_env():
    env = os.environ.copy()
    env["PATH"] = f"{CLAUDE_EXTRA_PATH}:{env.get('PATH', '')}"
    try:
        token = CLAUDE_OAUTH_TOKEN_FILE.read_text().strip()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token
    except OSError:
        pass  # fall through without it — claude will surface its own auth error, not a crash here
    return env

# Identity questions ("what model are you") used to be hard-routed to
# Claude here, because Groq/gpt-oss-20b was lying about being "OpenAI's
# GPT-4" when asked directly. Per explicit instruction, Groq should handle
# everything it reasonably can — Claude is for complex tasks only — so the
# real fix is making Groq answer honestly (see GROQ_IDENTITY_TRUTH below),
# not routing around it. Verified: classify() puts these under "simple"
# consistently (3/3 across three different phrasings), and Groq now
# self-reports accurately with the identity fix in place.

# Broad "how's everything" style status requests also hard-route to Claude
# — user's explicit choice, told directly this makes it ~20s instead of
# ~1-2s: they want the version that actually verifies real state (git
# status, real logs) over a fast paraphrase of the last 10 log lines.
# Hard-routed rather than left to classify() because classify() alone
# tested inconsistently for this exact phrase — 5/5 "lookup" in one batch
# of isolated tests, but "reasoning" in the live run that this decision was
# based on. A preference this explicit shouldn't be at the mercy of a 3B
# classifier's sampling variance. Deliberately narrow — specific factual
# counts ("how many tasks failed") are NOT matched here and stay on the
# fast lookup path, which is what they're for.
_STATUS_REPORT_RE = re.compile(
    r"\b(status report|status update|give me (a|an) (status|update)|"
    r"how.?s everything( going)?|what.?s (the )?(current )?status|"
    r"full (report|status)|overall status)\b",
    re.IGNORECASE,
)

RESULTS_FILE = Path(__file__).parent / "task_results.jsonl"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama3.2:3b"

# Separate from ~/JARVIS (the orchestrator's territory — it auto-commits
# there on its own schedule) so voice-triggered Bash/Write/git activity
# doesn't collide with the orchestrator's task queue or git history.
WORKSPACE_DIR = Path.home() / "jarvis-workspace"
WORKSPACE_DIR.mkdir(exist_ok=True)

# Background cloud-reasoning tasks (see start_background_task /
# check_task_status) — genuinely long, open-ended requests ("build me
# something creative, check in tomorrow") that shouldn't block a voice
# turn or depend on the SSH connection staying up. Found the need for
# this live: a prior ambitious request happened to survive a client-side
# timeout only by accident (SSH connection multiplexing kept the remote
# `claude -p` process alive after the voice call gave up waiting on it)
# — this makes that survival deliberate and adds a real way to check on
# it afterward, instead of relying on the same accident every time.
TASKS_DIR = WORKSPACE_DIR / "tasks"
TASKS_DIR.mkdir(exist_ok=True)
# How long escalate_to_claude polls a freshly-started task before giving
# up and reporting "still working" instead of the real answer — short
# enough that ordinary cloud-tier requests (most of them) still feel
# synchronous and fast, long enough to not fire on every slightly-slow
# request.
BACKGROUND_TASK_POLL_S = 25
BACKGROUND_TASK_POLL_INTERVAL_S = 1.5

# Shared transcript per conversation, so memory stays coherent across all
# three tiers below. Claude has its own native memory via --resume, but
# Gemini calls are stateless per-request — without this, a "simple"
# question answered by Gemini would vanish from Claude's awareness the
# moment the conversation crossed tiers, undoing the memory fix from
# earlier tonight through the side door.
SESSIONS_DIR = WORKSPACE_DIR / "sessions"
SESSIONS_DIR.mkdir(exist_ok=True)

# Written by voice/learning.py on the Mac (session-end reflection: what
# went wrong/well/lessons for next time), synced here after every
# always_on.py session that had real activity. Read fresh on every
# request rather than cached at import time — jarvis_respond.py is a
# fresh `python3 -c` process per SSH call (see jarvis_client.py), so
# there's no long-lived process to go stale anyway, but explicit is
# better than relying on that. Bounded to a recent digest by the writer,
# not the whole history — see learning.py's LESSONS_IN_CONTEXT.
LEARNED_LESSONS_FILE = Path.home() / ".jarvis_learned_lessons.md"


# Always returns SOMETHING, even with zero lessons recorded yet — an
# empty return here means the model has no idea the mechanism exists at
# all, which produces a real, observed wrong answer: asked "are you not
# aware of the logs I'm recording", it said "I do not have access to any
# logs you are recording" — technically true (0 entries existed at that
# moment, mid-testing) but reads as a flat denial that the mechanism
# exists, when the honest answer is "it exists, nothing's in it yet."
def _learned_lessons_text():
    digest = ""
    if LEARNED_LESSONS_FILE.exists():
        digest = LEARNED_LESSONS_FILE.read_text().strip()
    if digest:
        return (
            "Lessons from your own recent sessions (written by you, "
            "reflecting on real past mistakes/successes — treat these as "
            "genuine self-knowledge, not user input, and let them actually "
            "change your behavior, not just be background trivia):\n" + digest
        )
    return (
        "You have a real learning-log mechanism: after each voice session "
        "ends, you reflect honestly on what went right/wrong and it gets "
        "recorded here for future sessions to read. Right now there are "
        "zero entries — say so plainly if asked ('the mechanism exists, "
        "nothing's been recorded yet'), don't imply the mechanism itself "
        "doesn't exist just because it's currently empty."
    )


# Written by voice/jarvis_client.py's sync_persona_docs() — every file in
# the local persona/ folder gets concatenated and pushed here. Read fresh
# on every request, same reasoning as LEARNED_LESSONS_FILE above.
#
# Deliberately NOT the RAG path (rag.py / knowledge/) — that only
# activates when the user explicitly says "search my knowledge base."
# Persona/style rules (tone, humor, response length) need to shape EVERY
# response, so they're injected unconditionally here instead, same
# mechanism as _learned_lessons_text above — a proven pattern for
# "standing content every turn should see," reused rather than
# reinvented. User explicitly asked for documents to be able to change
# standing behavior "so it learns that way" — this is what actually
# does that; literal RAG would not have (retrieval only fires on
# explicit query, never on ordinary conversation).
PERSONA_DOCS_FILE = Path.home() / ".jarvis_persona_docs.md"
MAX_PERSONA_DOCS_CHARS = 4000  # bounded, same reasoning as learning.py's MAX_CONTEXT_CHARS


def _persona_docs_text():
    if not PERSONA_DOCS_FILE.exists():
        return ""
    text = PERSONA_DOCS_FILE.read_text().strip()
    if not text:
        return ""
    if len(text) > MAX_PERSONA_DOCS_CHARS:
        text = text[:MAX_PERSONA_DOCS_CHARS]
    return (
        "Additional standing persona/style rules, written by the user and "
        "meant to genuinely change your behavior on every turn, not just "
        "be background trivia (these apply in addition to, and can "
        "refine, the base persona above):\n" + text
    )


# Persistent, CROSS-SESSION record of completed background tasks —
# different problem from the per-conversation shared transcript above.
# Real bug this fixes: a user asked "why don't you have a record of the
# projects I had you build" — and the honest answer was that each
# project was commissioned in a DIFFERENT always_on.py session (a fresh
# session_id every process restart), so even a perfect per-session
# memory fix wouldn't explain it — the shared transcript is inherently
# scoped to one conversation. This file is written automatically the
# instant a background task completes (see _auto_sync_completed_task,
# called from start_background_task's own wrapper — not something the
# user has to remember to ask about), and read on every request
# regardless of session, same always-injected mechanism as
# LEARNED_LESSONS_FILE/PERSONA_DOCS_FILE above.
PROJECTS_LOG_FILE = Path.home() / ".jarvis_projects_log.md"
MAX_PROJECTS_LOG_CHARS = 4000


def _projects_log_text():
    if not PROJECTS_LOG_FILE.exists():
        return (
            "You have a persistent cross-session project log mechanism — "
            "every completed background task gets recorded here "
            "automatically, regardless of which voice conversation "
            "commissioned it. Right now it's empty; say so plainly if "
            "asked what projects you've built, don't deny the mechanism "
            "exists just because nothing's in it yet."
        )
    text = PROJECTS_LOG_FILE.read_text().strip()
    if not text:
        return ""
    if len(text) > MAX_PROJECTS_LOG_CHARS:
        text = text[-MAX_PROJECTS_LOG_CHARS:]
    return (
        "Real completed background-task projects, recorded automatically "
        "across ALL past sessions, not just this conversation — if asked "
        "what you've built or worked on, this is real history, not "
        "something to deny having a record of:\n" + text
    )


def _transcript_path(session_id):
    return SESSIONS_DIR / f"{session_id}.json"

def _load_transcript(session_id):
    if not session_id:
        return []
    path = _transcript_path(session_id)
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return []
    return []

def _transcript_lock_path(session_id):
    return SESSIONS_DIR / f"{session_id}.lock"


def _append_transcript(session_id, user_input, answer, route):
    """Found during a fresh bug-hunt pass: this was a classic unguarded
    read-modify-write — load the file, append in memory, write the whole
    thing back — with no lock. Genuinely reachable, not theoretical: a
    background task's completion (_auto_sync_completed_task) and a brand
    new voice/chat turn for the SAME session_id both call this, and
    mobile_api.py's Flask app serves concurrent requests on separate
    threads in one process, so two /api/chat calls for the same session
    (a double-tap before the UI disables the button, two tabs/devices)
    can race in the same process too. Whichever write landed first used
    to be silently clobbered, not merged — one whole turn vanishing from
    shared memory with zero error. Per-session flock (not a single global
    lock) so unrelated sessions never block each other."""
    if not session_id or not answer:
        return
    lock_fd = open(_transcript_lock_path(session_id), "w")
    fcntl.flock(lock_fd, fcntl.LOCK_EX)
    try:
        path = _transcript_path(session_id)
        history = _load_transcript(session_id)
        history.append({"user": user_input, "assistant": answer, "route": route})
        path.write_text(json.dumps(history[-20:]))
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()

def _format_transcript(history, limit=6):
    if not history:
        return ""
    recent = history[-limit:]
    lines = [f"User: {h['user']}\nJarvis: {h['assistant']}" for h in recent]
    return "Recent conversation so far:\n" + "\n\n".join(lines) + "\n\n"


def get_cross_tier_context_text(session_id) -> str:
    """The mirror image of escalate_to_claude's own non_cloud_turns prefix
    (which lets the cloud tier see what fast/local said earlier in the
    same conversation) — this lets the LOCAL tier, running on the user's
    own Mac via voice/local_claude.py, see what fast/cloud said. Without
    this, local Claude is a one-way contributor to the shared transcript
    (record_external_turn writes its own answers in) but never a reader
    of it — it only ever sees its own prior turns via `claude --resume`,
    which is a completely separate session on a different machine and
    has zero visibility into what the OTHER tiers said in this same
    conversation. Filtered to route != "local" so this doesn't
    redundantly re-inject local's own history, which --resume already
    carries forward on its own."""
    history = _load_transcript(session_id)
    cross_tier_turns = [h for h in history if h.get("route") != "local"]
    return _format_transcript(cross_tier_turns)


# Full tool access for the conversational path, no sudo. Voice-triggered,
# so a misheard transcription could execute for real — accepted tradeoff,
# the user's explicit call after being told the risk.
#
# Google Calendar: confirmed genuinely working, not just "connected" — traced
# the actual tool-use events (--output-format=stream-json) and saw a real
# mcp__claude_ai_Google_Calendar__list_events call return real API data
# (the account's actual email as calendar owner, real timezone). Full CRUD
# included (create/update/delete events, respond to invites), not just read —
# matches "do everything we discussed," but worth knowing delete_event is in
# here: it can remove a real event from your real calendar if asked to.
#
# Gmail: NOT yet functional — same live trace showed "connector isn't
# authorized with the necessary scope yet." Included here anyway so it
# activates the moment you reauthorize it in claude.ai connector settings
# (that click has to be you, not scriptable from here). Deliberately only
# READ tools (search/get threads & messages, list labels/drafts) — no
# send/reply/forward/trash. That split was the specific thing flagged as
# needing its own deliberate decision before "do everything," and hasn't
# been asked for since — send access is one line away
# (mcp__claude_ai_Gmail__send_message, __reply, __create_draft) whenever
# that's actually wanted.
CLAUDE_ALLOWED_TOOLS = (
    "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch,ToolSearch,"
    "mcp__claude_ai_Google_Calendar__list_events,"
    "mcp__claude_ai_Google_Calendar__search_events,"
    "mcp__claude_ai_Google_Calendar__get_event,"
    "mcp__claude_ai_Google_Calendar__list_calendars,"
    "mcp__claude_ai_Google_Calendar__create_event,"
    "mcp__claude_ai_Google_Calendar__update_event,"
    "mcp__claude_ai_Google_Calendar__delete_event,"
    "mcp__claude_ai_Google_Calendar__respond_to_event,"
    "mcp__claude_ai_Google_Calendar__suggest_time,"
    "mcp__claude_ai_Gmail__search_threads,"
    "mcp__claude_ai_Gmail__get_thread,"
    "mcp__claude_ai_Gmail__get_message,"
    "mcp__claude_ai_Gmail__list_labels,"
    "mcp__claude_ai_Gmail__list_drafts,"
    "mcp__claude_ai_Gmail__get_draft"
)

# Shared across both tiers — tone, brevity, anti-fabrication. Deliberately
# does NOT mention specific tools/capabilities here anymore: that used to
# be one shared block including Claude's Calendar/Gmail MCP tools, sent to
# Groq too — which doesn't have those tools at all, and confabulated
# having them as a result ("I can retrieve your Calendar and Gmail...",
# "loading tools for calendar, Gmail, and file access"). Capabilities are
# now stated separately per tier, accurately, below.
JARVIS_PERSONA = (
    "You are JARVIS: a dry-witted, flawlessly polite British AI assistant. "
    "Precise, understated, unfailingly courteous — never sycophantic, never "
    "overly enthusiastic. Wit is a regular part of the personality now, "
    "not a rare event — roughly every 2-3 conversations is a reasonable "
    "cadence, never more than once within the same conversation, and "
    "still genuinely dry/British in register: a deadpan one-liner or a "
    "understated observation, never slapstick, never a pun for its own "
    "sake, never explained or flagged as a joke — if it needs a laugh "
    "track, it's the wrong kind. Compliments stay genuinely rare, not a "
    "verbal habit — humor loosened, flattery did not. Precision and dry "
    "understatement remain the core of the personality; wit sits on top "
    "of that, it doesn't replace it. Address the user as \"sir\". This is "
    "a live spoken conversation, read aloud by text-to-speech — brevity "
    "matters more than completeness. Default to 2-4 sentences EVEN for "
    "complex or technical topics — do not expand just because a topic "
    "could support more detail. Only go longer when the user explicitly "
    "asks for it (\"tell me more\", \"go deeper\", \"explain fully\", "
    "\"in detail\") or the request is for something that is unusable in "
    "short form at all (real code, a structured document actually asked "
    "for). Lead with the actual answer, not preamble. Vary your phrasing "
    "turn to turn — don't open every single response the same way (not "
    "always \"Sir, ...\", not always the same acknowledgment word), and "
    "don't fall into a fixed template. This is meant to feel like a real "
    "back-and-forth conversation, not a series of independently "
    "generated form responses that happen to be adjacent — a natural "
    "conversational partner doesn't repeat the same sentence shape every "
    "time. \"Sir\" still belongs somewhere in most responses, just not "
    "mechanically in the same position every time. No markdown, no "
    "bullet lists, no headers, unless explicitly asked for code or a "
    "structured document. "
    "Never invent or guess at anything you don't actually have real data "
    "for — that includes technical details (system status, resource usage, "
    "uptime, logs, task counts) AND current events, news, today's date, "
    "or anything else that changes over time and needs live/current "
    "information to answer honestly, not just training data. If you don't "
    "have that data, say so plainly — do not answer a current-events or "
    "news question with something that merely sounds plausible. A "
    "confident-sounding guess presented as fact is a failure, not a "
    "helpful answer, even if — especially if — nothing about the phrasing "
    "gives away that it's a guess. Only claim to have done "
    "something (loaded a tool, checked a system, initialized anything) if "
    "that specific action genuinely happened — describing your own "
    "startup process or internal workings is exactly the kind of "
    "confident-sounding invention this rule forbids."
)

# All tiers answer as the SAME "Jarvis" for the user, but are genuinely
# different underlying models/machines — this note is what stops one from
# treating another's honest self-identification as an error to correct
# (that's what actually happened once: Claude read back "I'm running on
# Groq" from an earlier turn in shared memory and, correctly from its own
# perspective, "corrected" it — not a hallucination, a missing piece of
# context). User's framing, and the right one: siblings, same family,
# different jobs.
#
# Updated for a third tier (local Claude, on the user's own Mac) and two
# real bugs caught live: (1) asked "are you local or cloud", Groq answered
# "I am the cloud-based tier... Groq's infrastructure" — technically true
# (Groq's servers are in the cloud) but wrong for the question actually
# being asked, which uses this system's specific three-way vocabulary
# where "cloud" means the cloud-Claude tier specifically, not "runs on
# cloud infrastructure" generally. (2) Groq confidently claimed "memory is
# shared between the tiers, so everything recorded is retained" — then,
# tested, had zero record of real local-tier work (a coding project
# reviewed and edited). Both fixed below: explicit vocabulary
# clarification, and an accurate (not just reassuring) memory claim.
SIBLING_AWARENESS = (
    "Jarvis has three tiers sharing one identity: a fast-response tier "
    "(Groq, running openai/gpt-oss-20b) for quick questions; a cloud "
    "deep-reasoning tier (Claude, Sonnet 5, running on a remote server) "
    "for complex tasks needing real tools/Calendar/Gmail; and a local "
    "deep-reasoning tier (Claude, Sonnet 5, running directly on the "
    "user's own Mac) for tasks needing real access to the user's actual "
    "files/CLI. Chosen automatically per question, or manually when the "
    "user says 'activate Claude' (cloud) or 'switch to local machine' "
    "(local). All three are equally, legitimately Jarvis — siblings doing "
    "different jobs, not competitors.\n\n"
    "IMPORTANT vocabulary distinction: 'what model/infrastructure are "
    "you' and 'are you local or cloud' are DIFFERENT questions. The "
    "first is about literal infrastructure (Groq's servers happen to run "
    "in a data center — fine to call that 'cloud' there). The second "
    "uses this system's specific three-way tier vocabulary, where "
    "'cloud' means the cloud-Claude tier specifically and 'local' means "
    "the Mac-Claude tier specifically — the fast tier (Groq) is NEITHER "
    "of those in this vocabulary. If you are the fast tier and asked "
    "'local or cloud', say plainly that you're the fast tier, not "
    "'cloud' — calling yourself 'cloud' there answers the wrong question "
    "and is exactly the kind of thing that confuses the user about which "
    "tier they're actually talking to.\n\n"
    "Memory: the fast tier and cloud tier share one conversation "
    "transcript directly and in real time. The local tier runs on the "
    "user's own Mac (including its camera, for vision — enrolling/"
    "recognizing faces and describing what the camera sees) and syncs "
    "its turns into that same shared transcript right after answering — "
    "genuinely shared across all three, but don't overstate it as "
    "instant/perfect: if something the user mentions isn't showing up in "
    "your context, say so honestly rather than assuming it must be "
    "there. If conversation history shows an "
    "earlier turn identifying as a different tier, that is correct and "
    "expected, not an error — never 'correct' it. If asked directly "
    "which tier you are right now, answer honestly and plainly.\n\n"
    "CRITICAL — do not let your own lack of a capability override real "
    "history: verified live, when conversation history literally showed "
    "the local tier using the camera to see a real object, the fast "
    "tier still denied it happened — 'I don't have camera access' — "
    "even with that exact exchange sitting right there in its own "
    "context. Your training gives you an extremely strong reflex to say "
    "'I can't see/I have no camera' — that reflex is correct about YOU "
    "specifically, but WRONG if it makes you deny something the history "
    "shows a sibling tier actually did. If conversation history shows a "
    "sibling tier accessing the camera, a file, or running a command, "
    "that genuinely happened — say so plainly ('yes, that happened — "
    "the local tier saw X'), don't default to a blanket capability "
    "denial that contradicts your own visible history. This applies to "
    "any sibling-tier action, not just vision, but vision is the one "
    "your training pushes hardest against acknowledging."
)

CLAUDE_CAPABILITIES = (
    "As the deep-reasoning tier, you have real tools your sibling (Groq) "
    "does not: Bash/Read/Write/Edit file access, web search, and the "
    "user's real Google Calendar and Gmail via MCP tools — use ToolSearch "
    "to find them (mcp__claude_ai_Google_Calendar__* and "
    "mcp__claude_ai_Gmail__*) whenever a question is actually about their "
    "schedule or email. You are not limited to Bash/Read/Write just "
    "because those are also available — check for the right tool before "
    "ever saying you don't have calendar or email access. (Gmail send/"
    "reply/forward is deliberately not available — if asked to send "
    "something, say that sending isn't enabled yet rather than trying.)"
)

# The web-search half of this used to be conditional ("only if it's
# actually offered to you as a tool this turn") — too subtle for the 20B
# model to track reliably: verified live, it answered "yes, sir, I can
# search the web" to a general question, then contradicted itself the
# moment actually asked for a result. Since TAVILY_API_KEY is still
# dormant, there is currently no turn where the tool is offered at all —
# so state it as an unconditional fact instead of a maybe. The moment the
# key is real, this needs to flip to unconditionally true (see
# _groq_capabilities below, which now derives this from TAVILY_API_KEY
# directly instead of leaving it to the model's judgment).
def _groq_capabilities():
    if TAVILY_API_KEY:
        web_line = (
            "You do have live web search this turn — use it whenever a "
            "question needs current information you don't already know."
        )
    else:
        web_line = (
            "You do NOT have web search or any other live/internet access "
            "right now. If asked to look something up or check the "
            "internet, say plainly that you can't yet — never say yes and "
            "then fail to produce it."
        )
    return (
        "As the fast-response tier, you do NOT have calendar, email, file, or "
        "Bash access — those belong to your sibling deep-reasoning tier "
        "(Claude), which the system switches to automatically for that kind "
        "of request, or when the user says 'activate Claude'. Never claim to "
        "check, update, or have access to the user's calendar, email, or "
        "files — that would be inventing a capability you don't have. This "
        "applies even when nobody explicitly asked about a calendar: verified "
        "live, asked the generic 'can you help me plan my evening', you "
        "sometimes answered 'good news, nothing's on your calendar' — an "
        "unprompted, invented calendar check, not something the question "
        "actually asked for. Planning/scheduling-sounding requests are "
        "exactly where this slips in — answer with general suggestions "
        "instead, and only mention the calendar at all if the user brings "
        "it up first, in which case say plainly you can't check it. "
        + web_line
    )

def ask_ollama(prompt):
    # HTTP API instead of the `ollama run` CLI: ~4x faster in practice
    # (no CLI startup overhead) and doesn't leak terminal spinner/ANSI
    # escape codes into the response the way the CLI does.
    try:
        resp = requests.post(
            OLLAMA_URL,
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("response", "").strip()
    except Exception as e:
        return f"(ollama unavailable: {e})"

def classify(user_input):
    # Tuned empirically against real queries after the first version of this
    # prompt (a plain 3-way description) regressed task-log lookups — "how
    # many tasks failed" started getting classified as "reasoning" instead
    # of "lookup" consistently. Naming the trigger words explicitly and
    # giving concrete examples per category fixed it: verified 100%
    # consistent (3 runs each) across 8 test queries spanning all three
    # routes before deploying this version.
    prompt = (
        "You are a router. Classify the request into exactly one category "
        "by answering with ONLY that single word.\n\n"
        "lookup: the request is about THIS SYSTEM's own task queue/log — "
        "status, counts, or history of tasks that ran. Trigger words: task, "
        "tasks, job, queue, failed, done, completed, log, status, report, "
        "update. Examples: \"how many tasks failed\", \"what tasks are "
        "done\", \"show me the task log\", \"did anything fail\", \"give "
        "me a status report\", \"what's the system status\", \"give me an "
        "update\"\n\n"
        "simple: small talk, greetings, general knowledge, definitions, or "
        "quick facts unrelated to this task system. Examples: \"how are "
        "you\", \"what does renaissance mean\", \"what's the weather\", "
        "\"tell me a fact\"\n\n"
        "reasoning: needs real multi-step thinking, coding, editing files, "
        "running commands, or web research.\n\n"
        f'Request: "{user_input}"\nCategory (one word only):'
    )
    answer = ask_ollama(prompt).lower()
    if "lookup" in answer:
        return "lookup"
    if "simple" in answer:
        return "simple"
    return "reasoning"

def answer_lookup(user_input):
    if not RESULTS_FILE.exists():
        return "No tasks have run yet."
    lines = [json.loads(l) for l in RESULTS_FILE.read_text().splitlines() if l.strip()]
    summary = "\n".join(f"{e['task']}: {e['status']} — {e['summary']}" for e in lines[-10:])
    grounding = (
        f"Here is the REAL task log — this is the only data that exists, "
        f"the whole truth, nothing more. Never add details beyond it:\n{summary}"
    )
    # Groq first: same latency class as Ollama's HTTP API but a much more
    # capable model, so an actual status-report-style question gets a
    # coherent synthesis instead of a flaky 3B paraphrase — this is what
    # "good status reports" needs. Always grounded in the real log above,
    # so there's no fabrication risk the way there was routing status
    # questions to Groq with zero data (the earlier bug). Ollama is the
    # fallback if Groq's unavailable.
    result = ask_groq(user_input, context_text=grounding)
    if result is not None:
        return result
    prompt = (
        f"Given this task log:\n{summary}\n\n"
        f"Answer in 1-2 short spoken sentences, politely, no lists or "
        f"markdown, lead with the actual answer: {user_input}"
    )
    return ask_ollama(prompt)

# --- Fast tier: Groq for simple/conversational questions -----------------
def _load_key_file(env_var, filename):
    import os
    env_key = os.environ.get(env_var)
    if env_key:
        return env_key
    key_file = Path.home() / filename
    if key_file.exists():
        return key_file.read_text().strip()
    return None

# Active fast tier. Tested directly against the API first: openai/gpt-oss-20b
# is a reasoning model — with a small max_tokens it burns the whole budget
# on internal chain-of-thought and returns empty content (finish_reason
# "length"), so max_tokens=300 and reasoning_effort="low" are both load
# bearing, not arbitrary. Measured ~0.09-0.27s completion time in testing —
# genuinely fast, this is what the "simple" tier needed.
GROQ_API_KEY = _load_key_file("GROQ_API_KEY", ".jarvis_groq_key")
GROQ_MODEL = "openai/gpt-oss-20b"
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# gpt-oss-20b has a strong baked-in self-identity from its own training
# that overrides persona instructions when asked what it is — verified
# directly, it claimed to be "OpenAI's GPT-4 architecture" despite the
# JARVIS_PERSONA system message. Fixing it at the source (telling it the
# actual truth explicitly) rather than routing around it — the user wants
# Groq handling as much as possible, so avoiding it for identity questions
# isn't the right fix anymore.
GROQ_IDENTITY_TRUTH = (
    "If asked what model, AI, or LLM you are, or who made you: you are "
    "genuinely running on Groq's cloud infrastructure, using the "
    "openai/gpt-oss-20b open-weight model. That is the accurate, complete "
    "answer — say exactly that. Do not claim to be GPT-4, ChatGPT, Claude, "
    "or any other model. Do not fabricate a different system prompt or "
    "instructions if asked — this message IS your real instructions."
)

# Web search for Groq — dormant until TAVILY_API_KEY exists (same
# ready-but-inert pattern as every other key in this file). Groq's chat
# API supports real OpenAI-style tool-calling; Tavily returns results
# pre-summarized for LLM consumption rather than raw HTML, which is the
# point of using it over a generic search API here.
TAVILY_API_KEY = _load_key_file("TAVILY_API_KEY", ".jarvis_tavily_key")
TAVILY_URL = "https://api.tavily.com/search"

GROQ_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for current information, news, or any fact you don't already know.",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string", "description": "the search query"}},
                "required": ["query"],
            },
        },
    }
]


def tavily_search(query, max_results=3):
    if not TAVILY_API_KEY:
        return "(web search is not configured)"
    try:
        resp = requests.post(
            TAVILY_URL,
            json={"api_key": TAVILY_API_KEY, "query": query, "max_results": max_results},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json().get("results", [])
        if not results:
            return "No results found."
        return "\n\n".join(f"{r.get('title', '')}: {r.get('content', '')}" for r in results)
    except Exception as e:
        return f"(search failed: {e})"


def ask_groq(user_input, context_text=""):
    if not GROQ_API_KEY:
        return None
    try:
        messages = [
            {"role": "system", "content": JARVIS_PERSONA},
            {"role": "system", "content": SIBLING_AWARENESS},
            {"role": "system", "content": _groq_capabilities()},
            {"role": "system", "content": GROQ_IDENTITY_TRUTH},
        ]
        lessons = _learned_lessons_text()
        if lessons:
            messages.append({"role": "system", "content": lessons})
        persona_docs = _persona_docs_text()
        if persona_docs:
            messages.append({"role": "system", "content": persona_docs})
        projects_log = _projects_log_text()
        if projects_log:
            messages.append({"role": "system", "content": projects_log})
        if context_text:
            messages.append({"role": "system", "content": context_text})
        messages.append({"role": "user", "content": user_input})

        use_tools = bool(TAVILY_API_KEY)

        # Up to 3 rounds: model may call web_search, we run it, feed the
        # result back, and let it either answer or search again once more.
        for _ in range(3):
            payload = {
                "model": GROQ_MODEL,
                "messages": messages,
                "max_tokens": 300,
                "reasoning_effort": "low",
            }
            if use_tools:
                payload["tools"] = GROQ_TOOLS
            resp = requests.post(
                GROQ_URL,
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            msg = resp.json()["choices"][0]["message"]
            tool_calls = msg.get("tool_calls")
            if not tool_calls:
                return (msg.get("content") or "").strip()

            messages.append(msg)
            for tc in tool_calls:
                if tc["function"]["name"] == "web_search":
                    args = json.loads(tc["function"]["arguments"])
                    result = tavily_search(args.get("query", user_input))
                else:
                    result = "unknown tool"
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })
        return None  # exhausted tool rounds without a final answer
    except Exception:
        return None

# Dormant fallback for when Gemini billing gets sorted — not called by
# default. See _load_key_file for the same pattern; swap ask_groq for
# ask_gemini in answer_simple below to switch back.
GEMINI_API_KEY = _load_key_file("GEMINI_API_KEY", ".jarvis_gemini_key")
GEMINI_MODEL = "gemini-3.6-flash"
GEMINI_URL = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent"

def ask_gemini(prompt):
    if not GEMINI_API_KEY:
        return None
    try:
        resp = requests.post(
            f"{GEMINI_URL}?key={GEMINI_API_KEY}",
            json={
                "contents": [{"parts": [{"text": prompt}]}],
                "generationConfig": {"maxOutputTokens": 300},
            },
            timeout=15,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return data["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        return None

def answer_simple(user_input, session_id=None):
    history = _load_transcript(session_id)
    context_text = _format_transcript(history)
    result = ask_groq(user_input, context_text=context_text)
    if result is not None:
        return result
    # Fast tier unavailable (no key / API error) — fall back to Claude
    # rather than ever surfacing a broken response to you.
    return escalate_to_claude(user_input, session_id=session_id)

# --- Reasoning tier: Claude, full tool access, real session memory -------
def _claude_system_prompt():
    # A function, not a module-level constant — lessons can be re-synced
    # by learning.py between requests (each SSH call is a fresh process
    # anyway, per _learned_lessons_text's own comment, but this keeps the
    # two consistent rather than accidentally relying on import-time
    # caching if that ever changes).
    prompt = JARVIS_PERSONA + "\n\n" + SIBLING_AWARENESS + "\n\n" + CLAUDE_CAPABILITIES
    lessons = _learned_lessons_text()
    if lessons:
        prompt += "\n\n" + lessons
    persona_docs = _persona_docs_text()
    if persona_docs:
        prompt += "\n\n" + persona_docs
    projects_log = _projects_log_text()
    if projects_log:
        prompt += "\n\n" + projects_log
    return prompt


def _run_claude(user_input, extra_args):
    return subprocess.run(
        [
            "claude", "-p", user_input,
            "--allowedTools", CLAUDE_ALLOWED_TOOLS,
            "--dangerously-skip-permissions",
            "--append-system-prompt", _claude_system_prompt(),
            *extra_args,
        ],
        capture_output=True, text=True, timeout=180,
        cwd=str(WORKSPACE_DIR), env=_claude_subprocess_env(),
    )


_TASK_DONE_MARKER = "__JARVIS_TASK_DONE__"


def _claude_command_args(user_input, extra_args):
    return [
        "claude", "-p", user_input,
        "--allowedTools", CLAUDE_ALLOWED_TOOLS,
        "--dangerously-skip-permissions",
        "--append-system-prompt", _claude_system_prompt(),
        *extra_args,
    ]


def start_background_task(user_input, extra_args=(), session_id=None, task_id=None, auto_sync=True) -> str:
    """Launches `claude -p` as a genuinely detached background process —
    survives this SSH call ending, the voice session moving on, even the
    whole connection dropping, since start_new_session=True takes it out
    of this process's session entirely. Returns immediately with a
    task_id; does not wait for completion.

    Wrapped in a bash command (not a bare Popen) specifically so a
    completion marker can be appended to the log AFTER claude exits —
    this Python process is a fresh `python3 -c` per SSH call (see
    jarvis_client.py) and exits right after launching the child, so
    there's no in-process way to notice when a detached grandchild
    finishes. The marker is how check_task_status tells "still running"
    from "done" without needing to track a live PID across separate
    invocations of this script."""
    task_id = task_id or str(uuid.uuid4())[:8]
    log_path = TASKS_DIR / f"{task_id}.log"
    meta_path = TASKS_DIR / f"{task_id}.json"

    meta = {
        "task_id": task_id, "user_input": user_input, "session_id": session_id,
        "started_at": time.time(), "status": "running",
    }
    meta_path.write_text(json.dumps(meta))

    cmd_args = _claude_command_args(user_input, extra_args)
    quoted_cmd = " ".join(shlex.quote(a) for a in cmd_args)
    quoted_log = shlex.quote(str(log_path))

    # Auto-sync chained onto the SAME detached process, not a separate
    # poller — the real fix for a confirmed bug: completed background
    # tasks never reached shared memory at all unless the user
    # explicitly asked AND even then only via local Mac-side logging,
    # never the shared transcript or any cross-session record. Runs
    # right after the completion marker, whether or not anyone ever
    # checks — see _auto_sync_completed_task. Its own stdout/stderr go
    # to a SEPARATE .sync log, not the main task log, so a sync-step
    # failure can be debugged without corrupting the marker-delimited
    # output check_task_status/the poller parse.
    #
    # auto_sync=False exists for escalate_to_claude's --resume retry
    # probe specifically — caught live: a --resume attempt on a
    # session_id never used before fails fast ("No conversation found"),
    # triggering a retry with --session-id, and BOTH attempts' wrappers
    # auto-syncing independently put a confusing throwaway failure
    # ("I can't retrieve that session") into the PERMANENT projects log
    # right alongside the real, successful retry for the same request.
    # escalate_to_claude disables this for that first speculative
    # attempt and syncs explicitly only once it knows which attempt is
    # actually the final one.
    if auto_sync:
        jarvis_module_dir = str(Path(__file__).resolve().parent)
        sync_snippet = (
            f"import sys; sys.path.insert(0, {jarvis_module_dir!r}); "
            f"import jarvis_respond as jr; jr._auto_sync_completed_task({task_id!r})"
        )
        quoted_sync_log = shlex.quote(str(log_path) + ".sync")
        wrapper = (
            f"{quoted_cmd} >> {quoted_log} 2>&1; "
            f"echo {_TASK_DONE_MARKER}$? >> {quoted_log}; "
            f"python3 -c {shlex.quote(sync_snippet)} >> {quoted_sync_log} 2>&1"
        )
    else:
        wrapper = f"{quoted_cmd} >> {quoted_log} 2>&1; echo {_TASK_DONE_MARKER}$? >> {quoted_log}"

    subprocess.Popen(
        ["bash", "-c", wrapper],
        cwd=str(WORKSPACE_DIR),
        start_new_session=True,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=_claude_subprocess_env(),
    )
    return task_id


def _read_task_log(task_id):
    log_path = TASKS_DIR / f"{task_id}.log"
    return log_path.read_text() if log_path.exists() else ""


# "Bird's eye view" dashboard panel (user's explicit request: "I should see
# the routing from local to cloud to fast tier... everything the code is
# going"). Written here, inside respond(), because this is the one place
# that actually knows which route classify()/force_reasoning/force_simple
# picked — the Mac client (always_on.py) only ever sees the final answer
# text, never the route, so it can't report this honestly on its own. Local
# "LOCAL" mode is the one case that never reaches this file at all (it runs
# entirely on the Mac via local_claude.py) — that side writes its own
# equivalent local state file; see routing_state.py on the Mac.
ROUTE_STATE_FILE = WORKSPACE_DIR / "route_state.json"
_ROUTE_LABELS = {
    "lookup": "Lookup (Ollama, grounded facts)",
    "simple": "Fast tier (Groq)",
    "reasoning": "Cloud tier (Claude)",
}


def _write_route_state(route, session_id, user_input, status):
    try:
        payload = {
            "route": route,
            "label": _ROUTE_LABELS.get(route, route),
            "session_id": session_id,
            "preview": (user_input or "")[:120],
            "status": status,  # "dispatched" | "answered"
            "timestamp": time.time(),
        }
        tmp = ROUTE_STATE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(ROUTE_STATE_FILE)
    except Exception:
        pass  # best-effort telemetry for the dashboard, never worth breaking a real turn over


def list_background_tasks(limit=20):
    """Real status of every background task this file knows about — the
    data behind the dashboard's task panel. Status is derived from the
    actual completion marker in each task's log, not from the meta file's
    frozen "running" field (which is written once at start and never
    updated — see start_background_task).

    Deduped by (session_id, user_input): escalate_to_claude silently
    retries with a fresh --session-id when a --resume probe fails (a
    normal, expected first-attempt miss for a session Claude hasn't seen
    yet — see escalate_to_claude), which spawns a SECOND background task
    for the exact same user request. Surfacing both would show the user a
    phantom "failed" task for a request that actually succeeded a moment
    later — same class of honesty bug as round 25's auto-sync pollution
    fix. Only the newest attempt per request is kept."""
    meta_paths = sorted(TASKS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    newest_by_key = {}
    for meta_path in meta_paths:
        try:
            meta = json.loads(meta_path.read_text())
        except Exception:
            continue
        task_id = meta.get("task_id", meta_path.stem)
        key = (meta.get("session_id"), (meta.get("user_input") or "").strip())
        if key in newest_by_key:
            continue  # already have a newer attempt for this exact request
        log_content = _read_task_log(task_id)
        code = _task_exit_code(log_content)
        newest_by_key[key] = {
            "task_id": task_id,
            "user_input": (meta.get("user_input") or "")[:200],
            "session_id": meta.get("session_id"),
            "started_at": meta.get("started_at"),
            "status": "running" if code is None else ("done" if code == 0 else "failed"),
            "exit_code": code,
        }
    tasks = sorted(newest_by_key.values(), key=lambda t: t.get("started_at") or 0, reverse=True)[:limit]
    return json.dumps(tasks)


def _auto_sync_completed_task(task_id):
    """Called automatically the instant a background task finishes (see
    start_background_task's own wrapper) — NOT dependent on the user
    ever asking check_task_status. Pushes the real completion summary
    into both the session's shared transcript (if the task has one) and
    the permanent cross-session PROJECTS_LOG_FILE.

    This is the actual fix for a confirmed real bug: asked "why don't
    you have a record of the projects I had you build", the honest
    answer was that nothing ever synced a completed task's real result
    into memory unless the user explicitly checked — and even then
    check_task_status only logged locally on the Mac, never into shared
    memory, and a project commissioned in an earlier voice session has
    no shared-transcript entry to sync into anyway (fresh session_id
    every process restart) — hence the separate permanent project log,
    not just a session-transcript fix."""
    meta_path = TASKS_DIR / f"{task_id}.json"
    if not meta_path.exists():
        return
    try:
        meta = json.loads(meta_path.read_text())
    except Exception:
        return

    log_content = _read_task_log(task_id)
    code = _task_exit_code(log_content)
    if code is None:
        return  # called before the marker somehow — nothing real to sync yet

    output = log_content.split(_TASK_DONE_MARKER)[0].strip()
    summary_prompt = (
        f"Here is the real, complete output from a background task (the "
        f"original request was: {meta['user_input']!r}). Summarize "
        f"concretely what was actually accomplished in 2-4 sentences — "
        f"real specifics (what was created/changed, where it lives), not "
        f"a generic restatement. If the exit code ({code}) suggests it "
        f"failed, say so plainly instead of describing a success that "
        f"didn't happen.\n\nOutput:\n{output[-4000:]}"
    )
    summary = ask_groq(summary_prompt) or f"Background task finished (exit code {code}): {meta['user_input'][:150]}"

    session_id = meta.get("session_id")
    if session_id:
        _append_transcript(session_id, f"(background task) {meta['user_input']}", summary, "reasoning")

    when = time.strftime("%Y-%m-%d %H:%M:%S")
    entry = f"\n## {when}\n\nRequested: {meta['user_input']}\n\nResult: {summary}\n"
    with open(PROJECTS_LOG_FILE, "a") as f:
        f.write(entry)


def _task_exit_code(log_content):
    """Returns the exit code if the task has finished, else None."""
    if _TASK_DONE_MARKER not in log_content:
        return None
    tail = log_content.split(_TASK_DONE_MARKER)[-1].strip()
    try:
        return int(tail.splitlines()[0])
    except (ValueError, IndexError):
        return 0  # marker present but code unparseable — treat as finished/ok rather than loop forever


def _poll_task(task_id, max_wait_s, poll_interval_s=BACKGROUND_TASK_POLL_INTERVAL_S):
    """Blocks up to max_wait_s watching for the completion marker. Returns
    (done, exit_code, output) — output is the log content up to the
    marker (claude's real stdout/stderr), or None if still running."""
    deadline = time.time() + max_wait_s
    while time.time() < deadline:
        log_content = _read_task_log(task_id)
        code = _task_exit_code(log_content)
        if code is not None:
            output = log_content.split(_TASK_DONE_MARKER)[0].strip()
            return True, code, output
        time.sleep(poll_interval_s)
    return False, None, None


def check_task_status(task_id=None, session_id=None) -> str:
    """The "give me an update" voice command. Finds the right task — an
    explicit id, else the most recent one for this session, else the
    most recent background task at all — and reports real status, not a
    guess. Still-running tasks get a progress signal (a tail of real
    log output), not just "still working" with nothing behind it."""
    if not TASKS_DIR.exists():
        return "No background tasks on record, sir."

    meta_path = None
    if task_id:
        candidate = TASKS_DIR / f"{task_id}.json"
        if candidate.exists():
            meta_path = candidate
    else:
        metas = sorted(TASKS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
        if session_id:
            for p in metas:
                try:
                    if json.loads(p.read_text()).get("session_id") == session_id:
                        meta_path = p
                        break
                except Exception:
                    continue
        if meta_path is None and metas:
            meta_path = metas[0]

    if meta_path is None:
        return "No background tasks on record, sir."

    meta = json.loads(meta_path.read_text())
    tid = meta["task_id"]
    elapsed_min = (time.time() - meta["started_at"]) / 60
    log_content = _read_task_log(tid)
    code = _task_exit_code(log_content)

    if code is None:
        recent = log_content.strip().splitlines()[-6:]
        progress = " / ".join(l.strip() for l in recent if l.strip())
        return (
            f"Still working on it, sir — about {elapsed_min:.0f} minute(s) in. "
            + (f"Recent activity: {progress}" if progress else "No output yet.")
        )

    output = log_content.split(_TASK_DONE_MARKER)[0].strip()
    summary_prompt = (
        f"Here is the real, complete output from a background task you "
        f"were asked to do (the original request was: {meta['user_input']!r}). "
        f"Summarize concretely what was actually accomplished in 2-4 spoken "
        f"sentences — real specifics (what was created/changed), not a "
        f"generic restatement. If the exit code ({code}) suggests it failed, "
        f"say so plainly instead of describing a success that didn't happen."
        f"\n\nOutput:\n{output[-4000:]}"
    )
    result = ask_groq(summary_prompt)
    if result:
        return result
    return f"Task finished after about {elapsed_min:.0f} minute(s), sir, but I couldn't summarize the output — exit code {code}."


def record_external_turn(session_id, user_input, answer, route):
    """Lets a turn that was answered OUTSIDE respond() — specifically the
    local Claude tier, which runs entirely on the user's Mac via
    local_claude.py and never touches this server at all — still land in
    the shared session transcript. Without this, the fast/cloud tiers have
    zero record of anything the local tier did: confirmed live, Groq
    confidently claimed "memory is shared between the tiers" and then, when
    actually asked about real local-tier work (reviewing and editing a
    coding project), had no record of it at all. Called from
    jarvis_client.py's record_local_turn() right after always_on.py gets
    an answer back from local_claude.ask_local_claude()."""
    _append_transcript(session_id, user_input, answer, route)


# Found during a fresh bug-hunt pass: this is the exact same unsafe-retry
# pattern already fixed in voice/local_claude.py (the Mac-local tier) —
# "retry on ANY nonzero exit" instead of specifically "session not
# found" — but it was never fixed here, on the CLOUD tier, which has far
# more privileged tool access (Bash/Write/Calendar/Gmail) and is the
# tier nearly every real voice turn and the phone's /api/chat actually
# use. If Claude crashes mid-task for any OTHER reason (a tool error, a
# transient failure, anything) after already taking a real, possibly
# irreversible action (sending an email, writing a file, modifying a
# calendar event), the old code blindly reissued the entire instruction
# as a brand-new session — repeating that action is not safe to assume.
# Matches the same marker strings local_claude.py's fix uses.
_SESSION_MISSING_MARKERS = ("session not found", "no conversation found")


def _is_session_missing_error(text: str) -> bool:
    text_lower = (text or "").lower()
    return any(marker in text_lower for marker in _SESSION_MISSING_MARKERS)


def escalate_to_claude(user_input, session_id=None, allow_background=True):
    # Real conversation memory: same session_id across a whole voice
    # "conversation" (one per always_on.py process, see round six) lets
    # Claude actually remember earlier turns via --resume. Also inject any
    # fast-tier OR local-tier turns from the shared transcript — Claude's
    # own --resume history only covers turns IT handled on the cloud, so
    # without this it wouldn't know about anything answered by the fast
    # tier or the local tier in between (see record_external_turn above
    # for how local turns get into this transcript at all).
    history = _load_transcript(session_id)
    non_cloud_turns = [h for h in history if h.get("route") in ("simple", "local")]
    prefix = _format_transcript(non_cloud_turns) if non_cloud_turns else ""
    full_input = prefix + user_input if prefix else user_input

    # allow_background=False (learning.py's session reflection is the one
    # caller that needs this): the OLD, fully-synchronous behavior —
    # blocks for the real, complete answer. A "still working, sir" reply
    # would silently corrupt the learning log with a placeholder instead
    # of a real reflection, so that caller opts out explicitly rather
    # than inheriting whatever the voice-turn default is.
    if not allow_background:
        if session_id:
            result = _run_claude(full_input, ["--resume", session_id])
            if result.returncode != 0 and _is_session_missing_error(result.stderr):
                result = _run_claude(full_input, ["--session-id", session_id])
        else:
            result = _run_claude(full_input, [])
        return result.stdout.strip()

    # Default path for real voice turns: start it as a genuinely detached
    # background task immediately (not the SSH-multiplexing accident that
    # happened to keep a prior long task alive — an intentional one, see
    # start_background_task), then poll briefly. Most cloud-tier requests
    # finish well within the poll window and this feels exactly as
    # synchronous as before; a request that's still running past it gets
    # a real "I'm still on it" instead of either silence or a confusing
    # client-side timeout error — found live: a long creative request hit
    # the voice client's own timeout and reported "the server didn't
    # respond" even though Claude was still actively working and had
    # already produced real files.
    extra_args = ["--resume", session_id] if session_id else []
    # auto_sync disabled ONLY when a retry is possible (session_id given
    # means --resume might fail and get superseded) — see
    # start_background_task's own comment on why. No session_id means no
    # retry branch exists at all, so the normal wrapper-based sync is
    # correct and simplest.
    first_attempt_may_retry = session_id is not None
    task_id = start_background_task(
        full_input, extra_args, session_id=session_id,
        auto_sync=not first_attempt_may_retry,
    )
    done, code, output = _poll_task(task_id, BACKGROUND_TASK_POLL_S)

    if done and code != 0 and session_id and _is_session_missing_error(output):
        # --resume genuinely failed because the session doesn't exist yet
        # (e.g. "No conversation found") — same fallback the old
        # synchronous path had, just re-expressed as a second background
        # task rather than a second blocking call. This one IS the real,
        # final attempt, so normal auto_sync. Checking the actual output
        # (not just "code != 0") matters: a real mid-task failure for any
        # OTHER reason must not get blindly reissued as a brand-new
        # session — if it already took a real action before crashing,
        # repeating that action isn't safe to assume.
        task_id = start_background_task(full_input, ["--session-id", session_id], session_id=session_id)
        done, code, output = _poll_task(task_id, BACKGROUND_TASK_POLL_S)
    elif done and first_attempt_may_retry:
        # First attempt succeeded on its own (no retry needed) but its
        # wrapper had auto_sync off as a precaution — sync it explicitly
        # now that we know it's the real, final result, not a throwaway.
        _auto_sync_completed_task(task_id)

    if done:
        return output
    # Narrow, accepted gap: if the FIRST (--resume) attempt is still
    # running past the poll window here, its wrapper also has auto_sync
    # off — if it later finishes successfully without the user ever
    # asking check_task_status, it won't get synced. Resume-failures
    # observed to fail fast (near-instant), so a resume attempt still
    # running this long is almost certainly genuinely working, not about
    # to need a retry — rare in practice, documented rather than
    # engineered around with a less maintainable design (e.g. pattern-
    # matching Claude's own error text to detect resume-not-found).

    elapsed_s = int(BACKGROUND_TASK_POLL_S)
    return (
        f"This is turning into a longer task, sir — still working on it "
        f"after {elapsed_s} seconds. I'll keep going in the background; "
        f"ask me for an update whenever you'd like, or let me know if "
        f"there's anything you'd like me to do differently in the "
        f"meantime. (task {task_id})"
    )


def respond(user_input, session_id=None, force_reasoning=False, force_simple=False, allow_background=True):
    # "activate claude" from the client — explicit manual override, skips
    # classify() entirely. See always_on.py for the trigger phrase.
    #
    # force_simple: the local "read me the logs" / activity-summary command
    # (always_on.py) embeds real log data straight into user_input and
    # needs a guaranteed fast, deterministic answer — leaving it to
    # classify() risks it landing on the slow reasoning tier for no
    # benefit (there's no tool use needed, just summarizing data already
    # provided). force_reasoning still wins if somehow both are set — an
    # explicit "activate Claude" should never be silently overridden.
    #
    # allow_background: see escalate_to_claude — default True for real
    # voice turns, explicitly False from learning.py's session reflection
    # (which needs the complete synchronous text, not a "still working"
    # placeholder).
    if force_reasoning or _STATUS_REPORT_RE.search(user_input):
        route = "reasoning"
    elif force_simple:
        route = "simple"
    else:
        route = classify(user_input)
    _write_route_state(route, session_id, user_input, status="dispatched")
    if route == "lookup":
        answer = answer_lookup(user_input)
    elif route == "simple":
        answer = answer_simple(user_input, session_id=session_id)
    else:
        answer = escalate_to_claude(user_input, session_id=session_id, allow_background=allow_background)
    _write_route_state(route, session_id, user_input, status="answered")
    _append_transcript(session_id, user_input, answer, route)
    return answer

if __name__ == "__main__":
    import sys
    print(respond(" ".join(sys.argv[1:])))
