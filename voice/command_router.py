"""Decides whether an utterance is a mode-switch command (and which mode),
using an actual LLM instead of enumerating exact phrases.

Why: every regex-based trigger phrase added this session ("activate claude"
-> comma breaks it -> normalize punctuation; "claude" mis-heard as "cola" ->
enumerate word variants) was the same underlying problem — Whisper doesn't
reliably produce exact wording, so pattern-matching exact wording is
fundamentally the wrong tool. An LLM understands intent (and tolerates
phonetic mishearings like "cola"/"kola" for "Claude") without needing every
variant enumerated by hand. Runs directly against Groq from the Mac (same
local key set up for STT) — no SSH round trip, sub-second in practice.

Regex (always_on.py's _ACTIVATE_CLAUDE_RE etc.) is kept as a fallback for
when this call fails outright (network down) — never let a broken network
call silently disable switching entirely.
"""
import os
import requests
from pathlib import Path

GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
GROQ_MODEL = "openai/gpt-oss-20b"


def _load_key_file(env_var, filename):
    env_key = os.environ.get(env_var)
    if env_key:
        return env_key
    key_file = Path.home() / filename
    if key_file.exists():
        return key_file.read_text().strip()
    return None


GROQ_API_KEY = _load_key_file("GROQ_API_KEY", ".jarvis_groq_key")

_SYSTEM_PROMPT = (
    "You are a command router for a voice assistant called Jarvis, which "
    "has three ways of answering: a fast tier (default), a cloud "
    "deep-reasoning tier ('Claude'), and a local deep-reasoning tier that "
    "runs directly on the user's own computer with real file/CLI access "
    "('local Claude', 'local machine'). Speech-to-text is imperfect, so "
    "the exact wording may be garbled or phonetically similar to the real "
    "word (e.g. 'Cola' or 'Kola' for 'Claude').\n\n"
    "Decide if the user's utterance is a command to SWITCH modes, and "
    "which one, or if it's just a normal question/statement with no "
    "switching intent. Respond with EXACTLY ONE of these words, nothing "
    "else:\n"
    "CLOUD - switch to the cloud deep-reasoning tier (e.g. 'activate "
    "claude', 'activate cola', 'switch to claude', 'talk to claude')\n"
    "LOCAL - switch to the local tier on the user's own machine (e.g. "
    "'activate local', 'switch to local machine', 'use my computer')\n"
    "NORMAL - switch back to normal fast-tier routing (e.g. 'switch "
    "back', 'back to normal', 'deactivate claude', 'stop using claude')\n"
    "NONE - not a switching command at all, just a regular question or "
    "statement — this is the right answer for almost everything you see."
)


def classify_command(text: str) -> str:
    """Returns 'cloud', 'local', 'normal', or 'none' (never raises —
    caller should treat an exception/None as 'ask failed, use fallback')."""
    if not GROQ_API_KEY or not text or not text.strip():
        return None
    try:
        resp = requests.post(
            GROQ_URL,
            headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
            json={
                "model": GROQ_MODEL,
                "messages": [
                    {"role": "system", "content": _SYSTEM_PROMPT},
                    {"role": "user", "content": text},
                ],
                # gpt-oss-20b burns tokens on internal chain-of-thought before
                # the actual answer — same fix as jarvis_respond.py's ask_groq:
                # a small max_tokens returns empty content (finish_reason
                # "length") with all budget spent on invisible reasoning.
                "max_tokens": 300,
                "reasoning_effort": "low",
            },
            timeout=8,
        )
        if resp.status_code != 200:
            return None
        answer = (resp.json()["choices"][0]["message"].get("content") or "").strip().upper()
        # Exact match only — substring matching let an off-spec answer like
        # "NONE: do not select LOCAL or CLOUD" match CLOUD (checked first)
        # purely because the word appears inside the model's own explanation
        # of why it *isn't* that answer. The prompt asks for exactly one
        # word; treat anything else as a failed classification, not a guess.
        answer = answer.strip().rstrip(".:")
        if answer in ("CLOUD", "LOCAL", "NORMAL", "NONE"):
            return answer.lower()
        return None
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    text = " ".join(sys.argv[1:]) or "activate claude"
    print(classify_command(text))
