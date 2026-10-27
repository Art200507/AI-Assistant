"""Claude Code running directly on THIS Mac — real Bash/Read/Write/Edit
access to the user's actual files and CLI, not the cloud server's. This is
the third tier: fast (Groq), cloud reasoning (Claude on the Oracle server,
jarvis_respond.py), local reasoning (this file). Triggered by "activate
local" / "switch to local machine" (see command_router.py + always_on.py).

Real, unrestricted shell access to the user's actual daily-driver machine,
not a disposable cloud VM — meaningfully higher stakes than the cloud path.
No sudo, same as the cloud tier, but otherwise full Bash. Confined to a
dedicated workspace directory as cwd (not the JARVIS-local repo itself, so
a misheard command can't rewrite this project's own code) — Claude can
still read/write elsewhere on the filesystem if asked, since Bash isn't
sandboxed to that directory, but starts there for anything relative.

Persona/capabilities text is deliberately duplicated from jarvis_respond.py
rather than shared over the network (the whole point of this path is NOT
depending on the server) — jarvis_respond.py remains the source of truth
for the shared tone/rules; keep these in sync by hand if that changes.
"""
import subprocess
from pathlib import Path

LOCAL_WORKSPACE_DIR = Path.home() / "FRIDAY" / "JARVIS-local" / "local-workspace"
LOCAL_WORKSPACE_DIR.mkdir(exist_ok=True)

# No MCP Calendar/Gmail tools here — those are configured for the cloud
# Claude context (claude.ai connectors); not assumed to carry over to a
# fresh local invocation without testing. Extend this the same way
# CLAUDE_ALLOWED_TOOLS was extended on the server, once actually verified
# working locally.
LOCAL_ALLOWED_TOOLS = "Bash,Read,Write,Edit,Glob,Grep,WebSearch,WebFetch"

JARVIS_PERSONA = (
    "You are JARVIS: a dry-witted, flawlessly polite British AI assistant. "
    "Precise, understated, unfailingly courteous — never sycophantic, never "
    "overly enthusiastic. You essentially never make jokes — at most once "
    "in every ten separate conversations, never more than once within the "
    "same conversation, and most conversations should have none at all. "
    "The same restraint applies to compliments: genuinely rare, not a "
    "verbal habit. Default to zero embellishment — precision and dry "
    "understatement are the personality, not jokes. Address the user as "
    "\"sir\". This is a live spoken conversation, read aloud by "
    "text-to-speech — brevity matters more than completeness. Default to "
    "2-4 sentences. Lead with the actual answer, not preamble. Only go "
    "longer when the question genuinely can't be answered properly in that "
    "space (e.g. real multi-step reasoning or code) — and even then, cut "
    "anything that isn't load-bearing. No markdown, no bullet lists, no "
    "headers, unless explicitly asked for code or a structured document. "
    "Never invent or guess at anything you don't actually have real data "
    "for — that includes technical details AND current events, news, "
    "today's date, or anything else that changes over time. If you don't "
    "have that data, say so plainly. A confident-sounding guess presented "
    "as fact is a failure, not a helpful answer."
)

SIBLING_AWARENESS = (
    "Jarvis has three tiers sharing one identity, but NOT one memory across "
    "all of them — a fast-response tier (Groq) and a cloud deep-reasoning "
    "tier (Claude, on a remote server) share memory with each other, but "
    "YOU are the third tier: Claude running locally on the user's own "
    "computer, with your own separate conversation history. If asked "
    "what you are: you are Claude (Sonnet 5), running locally, invoked "
    "because the user said 'activate local' or similar. All three tiers "
    "are legitimately Jarvis — different jobs, not competitors."
)

LOCAL_CAPABILITIES = (
    "As the local tier, you have real Bash/Read/Write/Edit access to the "
    "user's actual Mac — not a cloud server. Your working directory is a "
    "dedicated local workspace, but Bash is not sandboxed to it; you can "
    "read or write elsewhere on this machine if the user asks. No sudo. "
    "You do NOT have the cloud tier's Calendar/Gmail access. Say so "
    "plainly if asked to do something needing those."
)

_LOCAL_SYSTEM_PROMPT = JARVIS_PERSONA + "\n\n" + SIBLING_AWARENESS + "\n\n" + LOCAL_CAPABILITIES


def _run_local_claude(user_input: str, extra_args):
    return subprocess.run(
        [
            "claude", "-p", user_input,
            "--allowedTools", LOCAL_ALLOWED_TOOLS,
            "--dangerously-skip-permissions",
            "--append-system-prompt", _LOCAL_SYSTEM_PROMPT,
            *extra_args,
        ],
        capture_output=True, text=True, timeout=180,
        cwd=str(LOCAL_WORKSPACE_DIR),
    )


def ask_local_claude(user_input: str, session_id: str = None) -> str:
    """Same --resume -> --session-id fallback pattern as the cloud tier
    (escalate_to_claude in jarvis_respond.py) — a session_id that doesn't
    exist yet locally fails --resume cleanly, so fall back to minting it.

    Only retries as a brand-new session when stderr actually says the
    session is missing. A nonzero exit can also mean the agent crashed
    mid-tool-execution (e.g. a bash command failed after already writing
    a file) — blindly retrying on any nonzero code would resubmit the same
    user_input as a new run and risk redoing that side effect."""
    session_missing_markers = ("session not found", "no conversation found")
    if session_id:
        result = _run_local_claude(user_input, ["--resume", session_id])
        if result.returncode != 0 and any(
            marker in (result.stderr or "").lower() for marker in session_missing_markers
        ):
            result = _run_local_claude(user_input, ["--session-id", session_id])
    else:
        result = _run_local_claude(user_input, [])
    if result.returncode != 0:
        err = (result.stderr or "unknown error").strip()
        return f"Something went wrong running locally, sir: {err[:200]}"
    return result.stdout.strip()


if __name__ == "__main__":
    import sys
    print(ask_local_claude(" ".join(sys.argv[1:]) or "say hello and confirm you're running locally"))
