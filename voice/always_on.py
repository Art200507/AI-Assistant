"""JARVIS always-on mode: clap OR a long spacebar hold wakes it up.

Runs one persistent ffmpeg process streaming raw mic audio to this process.
While idle, every ~20ms chunk is checked for a clap (a sharp amplitude
transient well above the recent ambient noise floor). In parallel, a global
keyboard listener watches for the spacebar being held down for at least
LONG_PRESS_SECONDS. Either one wakes Jarvis the same way: it speaks a short
acknowledgment (a rotating pool of dry, polite, JARVIS-flavored lines —
see _WAKE_ACK_POOL), then listens for your actual question. Ending a turn
is explicit, not guessed from pause length: say "Jarvis" to signal you're
done, like a radio sign-off. A pause just triggers a checkpoint — whatever
you've said so far gets transcribed and checked for a trailing "Jarvis";
if it's not there, recording simply continues no matter how long the pause
was (a silence-length fallback exists only for when you forget to say it).
Then runs it through the same ask_jarvis -> speak pipeline as the
push-to-talk script (jarvis_voice.py).

Clap detection is amplitude/transient-based, not a trained classifier — it
can be fooled by other sharp sounds (a dropped object, a door) and may need
JARVIS_CLAP_RATIO / JARVIS_CLAP_ABS_MIN tuned to your room (run with
--calibrate). A loud single-frame spike is also exactly what a light touch
on the mic/table/laptop chassis looks like amplitude-wise — reported live
as "slightest touch sound activates it" — so a spike alone is no longer
enough: it also has to pass a zero-crossing-rate check (see
JARVIS_CLAP_MIN_ZCR below), a cheap proxy for "broadband, high-frequency
crack" that a real clap has and a dull knock/touch/thump mostly doesn't
(touches are low-frequency-dominated — energy concentrated near DC, few
sign changes per sample; a clap's energy is spread much higher). The
spacebar listener needs Accessibility permission the first time (System
Settings -> Privacy & Security -> Accessibility -> allow your terminal
app) since it's a global hotkey, not tied to any window.
"""
import json
import os
import random
import re
import signal
import struct
import subprocess
import sys
import threading
import time
import uuid
import wave
from collections import deque
from pathlib import Path

from pynput import keyboard, mouse

import command_router
import routing_state
import local_claude
from file_bridge import fix_file, latest_inbox_file, log_activity, ACTIVITY_LOG
from learning import sync_lessons_now
# vision (face_recognition/numpy) is imported lazily inside
# _verify_owner_at_startup, its only call site — that's gated behind the
# opt-in JARVIS_REQUIRE_BIOMETRIC_GATE env var (off by default), so an
# unconditional top-level import here used to force a clean install to
# have face_recognition/numpy/cv2/mediapipe importable just to START this
# process at all, even with camera features entirely disabled. gesture
# (cv2/mediapipe) has zero remaining call sites since the trackpad-tap
# gesture-wake dispatch was removed earlier — gesture.py itself is
# untouched and still fully usable if re-wired, just not imported here.
from jarvis_client import ask_jarvis, record_local_turn, ask_knowledge_base, sync_and_reindex_knowledge, sync_persona_docs, check_task_status, fetch_cross_tier_context, SSH_HOST, SSH_USER, SSH_KEY, _SSH_MULTIPLEX_OPTS
from jarvis_voice import transcribe, speak, AUDIO_DEVICE
from mic_device import describe_selected_device
from voice_state import write_state

SAMPLE_RATE = 16000
CHUNK_MS = 20
CHUNK_BYTES = int(SAMPLE_RATE * CHUNK_MS / 1000) * 2  # s16le mono, 2 bytes/sample

# How much louder than the ambient noise floor a chunk must be to count as
# a clap, and an absolute floor so a silent room doesn't make this hair-trigger.
CLAP_RATIO = float(os.environ.get("JARVIS_CLAP_RATIO", "6.0"))
CLAP_ABS_MIN = float(os.environ.get("JARVIS_CLAP_ABS_MIN", "1800"))
CLAP_COOLDOWN_S = 1.0

# Minimum fraction of consecutive samples that must cross zero for a loud
# spike to count as clap-like rather than a dull touch/knock/bump. A real
# clap is a broadband "crack" (lots of high-frequency content -> lots of
# zero crossings); a knuckle on the table or a finger brushing the mic is
# dominated by low-frequency resonance of the surface/mic housing (few
# zero crossings) despite potentially having just as high a peak/RMS
# level — amplitude alone can't tell these apart, which is the actual bug
# being fixed here. 0.25 is a reasoned starting point (speech/claps are
# typically well above it, dull knocks well below), not measured data —
# run --calibrate, make both sounds, and read the real zcr numbers if
# this still misfires either direction.
CLAP_MIN_ZCR = float(os.environ.get("JARVIS_CLAP_MIN_ZCR", "0.25"))


def _zero_crossing_rate(chunk_bytes: bytes) -> float:
    n = len(chunk_bytes) // 2
    if n < 2:
        return 0.0
    samples = struct.unpack(f"<{n}h", chunk_bytes)
    crossings = sum(1 for i in range(1, n) if (samples[i] >= 0) != (samples[i - 1] >= 0))
    return crossings / (n - 1)


def _is_clap_sound(chunk_bytes: bytes) -> bool:
    return _zero_crossing_rate(chunk_bytes) >= CLAP_MIN_ZCR

# How long the spacebar must be held down (while still held, not on release)
# before it counts as a wake trigger — short taps do nothing.
LONG_PRESS_SECONDS = float(os.environ.get("JARVIS_LONG_PRESS_SECONDS", "0.6"))

# What Jarvis says the instant it wakes, before it starts actually listening
# for your real question — gives you an audible cue that it's your turn to
# talk. This now only plays once per conversation (not every follow-up
# turn), so exposure to it is already much lower than before. On top of
# that, the joke/compliment lines are picked with low explicit probability
# rather than uniformly, so they land roughly once every ~20 conversations,
# not once every ~10 turns. Set JARVIS_WAKE_ACK to pin a single fixed phrase.
_WAKE_ACK_PLAIN = [
    "At your service, sir.",
    "Yes, sir? I'm listening.",
    "Go ahead, sir.",
    "Standing by, sir.",
    "How may I be of assistance?",
    "Do go on, sir.",
    "I'm listening, sir.",
    "Yes, sir?",
]
_WAKE_ACK_RARE = [
    "Ah, back so soon. Go on, sir.",  # tiny dry joke
    "Always a pleasure, sir. What can I do for you?",  # compliment
]
_WAKE_ACK_RARE_PROBABILITY = 0.05


def _pick_wake_ack() -> str:
    fixed = os.environ.get("JARVIS_WAKE_ACK")
    if fixed:
        return fixed
    if random.random() < _WAKE_ACK_RARE_PROBABILITY:
        return random.choice(_WAKE_ACK_RARE)
    return random.choice(_WAKE_ACK_PLAIN)


# "I want jarvis to respond immediately, even if it's still working on it" —
# reported live as real dead air: a cloud-tier turn used to go straight to
# ask_jarvis() and say nothing at all until the server came back (up to the
# ~25s background-task poll window). This pool is spoken instantly, before
# the request even goes out — see the mode == "cloud" branch below, which
# dispatches the real request in a background thread right after.
_CLOUD_ACK_POOL = [
    "Let's go, sir — I'll begin now.",
    "Right away, sir.",
    "On it now, sir.",
    "Understood, sir — starting now.",
    "Very good, sir. Beginning now.",
]


def _pick_cloud_ack() -> str:
    fixed = os.environ.get("JARVIS_CLOUD_ACK")
    if fixed:
        return fixed
    return random.choice(_CLOUD_ACK_POOL)

# Endpointing for the actual query after a wake trigger.
#
# Tried webrtcvad here (real VoIP-grade voice-activity detection) instead of
# a raw volume threshold, on the theory it would tell a quiet trailing "s"
# apart from true silence better than a threshold can. Measured it directly
# against this Mac's built-in mic before trusting it: it classified 90-97%
# of quiet ambient room audio as "speech" at every sensitivity mode (0-3).
# That's not a tuning problem, it's unusable — it would mean almost every
# turn hits the MAX_UTTERANCE_S hard cap instead of ending naturally. Likely
# cause: webrtcvad's model was trained on telephone-quality 8kHz audio and
# doesn't generalize well to this mic's noise floor. Reverted to the RMS
# threshold, which is measured to actually work on this hardware.
SILENCE_RMS = float(os.environ.get("JARVIS_SILENCE_RMS", "400"))

# Explicit stop word instead of guessing you're "done" from pause length —
# any fixed silence threshold is wrong for someone half the time: too short
# and it cuts you off mid-thought, too long and it feels laggy. Say
# "Jarvis" to end your turn, like a radio sign-off ("...over."). A pause is
# no longer a hard stop, just a checkpoint: whatever's been said so far
# gets transcribed and checked for a trailing "Jarvis" — if it's there,
# that's the real end of the turn (word stripped before it goes anywhere);
# if not, recording just continues, no matter how long the pause was.
STOP_WORD = "jarvis"
# "Jarvis" doesn't reliably transcribe — confirmed from real use, not
# guessed, and true of BOTH STT engines tried: local whisper-cli produced
# "Chavez" and "Jorvis"; Groq's much bigger whisper-large-v3-turbo (the
# current default) still produced "Javasu" ("Activate Cola Javasu" ==
# "Activate Claude, Jarvis" — "Cola" already covers the Claude half).
# Bigger model, same category of problem — a phonetically unusual word is
# still going to get mangled sometimes regardless of model size. Rather
# than pick a whole new trigger word, accept the actual observed
# mishearings as equivalent. Add more here the moment a new one shows up.
STOP_WORD_VARIANTS = {"jarvis", "chavez", "jorvis", "javis", "jarviss", "charvis", "travis", "javasu"}
CHECKPOINT_SILENCE_MS = float(os.environ.get("JARVIS_CHECKPOINT_SILENCE_MS", "600"))
# Safety net only, for when you forget to say it or just trail off: end the
# turn anyway after this much continuous silence, using whatever was
# transcribed at the last checkpoint.
STOPWORD_FALLBACK_SILENCE_MS = float(os.environ.get("JARVIS_STOPWORD_FALLBACK_MS", "4000"))
MAX_UTTERANCE_S = 45  # hard cap regardless — a true safety net, not a normal limit

_STOPWORD_TRAIL_RE = re.compile(
    r"\b(" + "|".join(STOP_WORD_VARIANTS) + r")[.!?,]*\s*$", re.IGNORECASE
)


def _ends_with_stopword(text: str) -> bool:
    words = re.findall(r"[a-zA-Z']+", text.lower())
    return bool(words) and words[-1] in STOP_WORD_VARIANTS


def _strip_stopword(text: str) -> str:
    return _STOPWORD_TRAIL_RE.sub("", text).strip()


# "Check the inbox" / "fix that file" — a LOCAL intent, detected here on the
# Mac before anything goes to the server, since only this machine can read
# the inbox/ folder at all. See file_bridge.py for the actual upload ->
# Claude-fixes-it -> download -> backup -> log pipeline this triggers.
_FILE_FIX_RE = re.compile(
    r"\b(check the inbox|look at (the|that) file|"
    r"fix (the |that )?(bugs?|file)|fix it)\b",
    re.IGNORECASE,
)

# "Read me the logs" — another LOCAL intent, same reasoning as the file-fix
# one above: only this machine has activity.log (every conversation turn,
# now logged below, plus file-fix events from file_bridge.py). Deliberately
# distinct phrasing from _STATUS_REPORT_RE (server-side, jarvis_respond.py)
# — that one is the user's earlier explicit choice for a slow, thorough
# Claude-verified system-state check (~20s, real git/log inspection); this
# one is for "what have we actually been doing", answered fast from real
# log data via force_simple (see ask_jarvis/respond).
_LOG_SUMMARY_RE = re.compile(
    r"\b(read (me )?the logs?|activity log|"
    r"what have (we|you) been (doing|talking about)|"
    r"give me an activity (summary|report))\b",
    re.IGNORECASE,
)
LOG_SUMMARY_LINES = 15

# "Give me an update" — status on a background cloud-reasoning task (see
# jarvis_respond.start_background_task / check_task_status). Distinct
# from _LOG_SUMMARY_RE above: that one reads the local activity log,
# this one asks the server for real progress on a specific long-running
# task. Built after a genuinely long creative request ("build me an
# agentic study tool, check in tomorrow") hit the voice client's own
# timeout and said "the server didn't respond" even though Claude was
# still actively working — this is the real "check on it later" the
# user actually asked for, not a guess at what they might want.
_TASK_UPDATE_RE = re.compile(
    r"\b(give me an update|how'?s (the|my) (task|project|work)( going)?|"
    r"check on (the|my) (task|project)|status on (the|my) (task|project)|"
    r"how is it going|any progress|did you (do|finish|build) it|"
    r"what (has it|have you) built|is it done|has it finished|"
    r"(tell me |check( with claude)? )?what (it|claude) (has|'s) built|"
    r"did (it|claude) finish)\b",
    re.IGNORECASE,
)

# Retry phrase for the startup owner-verification gate (see
# _verify_owner_at_startup) — deliberately plain regex, not the AI
# command router: this is a single narrow security-critical phrase, not
# a varied intent space, and a gate that decides whether to accept ANY
# commands at all shouldn't have a network call (the AI router) as a
# single point of failure for something this security-relevant.
_LOOK_AGAIN_RE = re.compile(
    r"\b(look again|check again|try again|scan again|verify again)\b",
    re.IGNORECASE,
)
STARTUP_VERIFY_WAIT_S = 20.0

# Vision commands — all LOCAL intents (the camera is this machine's
# hardware; there's no meaningful way to route this through the cloud
# tier). Three deliberately distinct phrasings for three genuinely
# different operations (see vision.py's docstring for why face identity
# matching is NOT routed through Claude the way scene description is):
#   - enroll: one-time reference capture
#   - recognize: "is the person in frame me" — deterministic face_recognition match
#   - describe: "what's in front of the camera" — Claude's vision, open-ended
_FACE_ENROLL_RE = re.compile(
    r"\b(enroll (my )?face|remember (my )?face|learn (my )?face)\b",
    re.IGNORECASE,
)
_FACE_RECOGNIZE_RE = re.compile(
    r"\b(do you recognize me|recognize me|who is this|is this me|"
    r"who am i)\b",
    re.IGNORECASE,
)
_VISION_DESCRIBE_RE = re.compile(
    r"\b(what do you see|look at this|open the camera|what am i (holding|looking at)|"
    r"take a (look|photo)|what is this|look again|check again|"
    r"take another look|look once more)\b",
    re.IGNORECASE,
)

# RAG over knowledge/ (rag.py, server-side). "Update my knowledge base"
# is checked first and is deliberately a distinct phrase from any query
# — syncing+reindexing takes a few seconds and shouldn't accidentally
# fire on a real question that happens to mention "knowledge base."
_KNOWLEDGE_UPDATE_RE = re.compile(
    r"\b(update (my )?knowledge base|sync (my )?knowledge base|"
    r"reindex (my )?knowledge base|add (this|that) to (my )?knowledge base)\b",
    re.IGNORECASE,
)
_KNOWLEDGE_QUERY_RE = re.compile(
    r"\b(search (my )?knowledge base|check (my )?knowledge base|"
    r"what does (my )?knowledge base say|ask (my )?knowledge base|"
    r"check (my )?documents)\b",
    re.IGNORECASE,
)

# persona/ — deliberately NOT part of the RAG (knowledge base) commands
# above. Those only apply on explicit query; this makes every dropped
# document shape every response, always. See jarvis_respond.py's
# _persona_docs_text for the full reasoning.
_PERSONA_UPDATE_RE = re.compile(
    r"\b(update (my )?persona|sync (my )?persona|reload (my )?persona)\b",
    re.IGNORECASE,
)

# Reported live: asking "can you open the app" fell through to Claude,
# which correctly said it has no ability to launch applications — true,
# since nothing exposes that as a tool to it. This is a local, no-network
# intent (same category as the knowledge-base/persona commands above),
# handled directly rather than routed anywhere.
_OPEN_DASHBOARD_RE = re.compile(
    r"\b(open|show|launch|bring up|pull up)( me)? (the |my )?(dashboard|app|status (interface|app|screen))\b",
    re.IGNORECASE,
)


def _recent_activity_text(n: int = LOG_SUMMARY_LINES) -> str:
    if not ACTIVITY_LOG.exists():
        return ""
    lines = [l for l in ACTIVITY_LOG.read_text().splitlines() if l.strip()]
    recent = lines[-n:]
    out = []
    for line in recent:
        try:
            e = json.loads(line)
        except Exception:
            continue
        when = e.get("when", "")
        if e.get("type") == "file_fix":
            out.append(f"[{when}] fixed file {e.get('file', '?')}: {e.get('summary', e.get('error', ''))}")
        else:
            out.append(f"[{when}] You: {e.get('user', '')} | Jarvis: {e.get('assistant', '')}")
    return "\n".join(out)

# Explicit manual override for which tier answers, instead of leaving it to
# classify(). Added because auto-routing between two genuinely different
# models sharing one "Jarvis" identity produces a jarring effect: each tier
# is honest about itself individually, but that means Jarvis can seem to
# contradict its own earlier claims mid-conversation whenever the tier
# switches unpredictably. Explicit control means you always know which one
# you're talking to. Resets to off at the start of every new conversation
# (new wake trigger) — deliberately not sticky across separate claps, so
# you can't accidentally leave it on and forget.
# "Claude" is a genuinely hard word for Whisper (base.en) to catch
# reliably from real speech — same category of problem as the "Jarvis"
# stop-word mishearings above, and same fix: accept the real observed (or
# phonetically obvious) mishearings as equivalent instead of demanding
# exact spelling. User reported Whisper hearing it as "Cola"; "Claud" and
# "Clawed" are the same one-syllable-off shape and worth covering
# preemptively for the same reason. Deliberately not adding "Cloud" —
# too likely to collide with unrelated sentences ("is it cloudy").
_CLAUDE_WORD = r"(?:claude|cola|claud|clawed)"
_ACTIVATE_CLAUDE_RE = re.compile(
    rf"\b(activate {_CLAUDE_WORD}|switch to {_CLAUDE_WORD}|"
    rf"use {_CLAUDE_WORD}( code)?|talk to {_CLAUDE_WORD})\b",
    re.IGNORECASE,
)
_DEACTIVATE_CLAUDE_RE = re.compile(
    rf"\b(deactivate {_CLAUDE_WORD}|switch back|back to (normal|fast)( mode)?|"
    rf"stop using {_CLAUDE_WORD}|exit {_CLAUDE_WORD}( mode)?)\b",
    re.IGNORECASE,
)
_ACTIVATE_LOCAL_RE = re.compile(
    r"\b(activate local|switch to (the )?local( machine)?|use (the )?local machine|"
    r"talk to (the )?local machine)\b",
    re.IGNORECASE,
)

# All three regexes above are the FALLBACK path only, used when the AI
# command router (command_router.classify_command, below) can't be reached
# at all (network down). Enumerating exact phrases was always going to lose
# this fight — every fix this session was another mishearing to add by
# hand ("activate claude" broken by a comma; "claude" heard as "cola").
# An LLM understands intent instead of exact wording, so it doesn't need
# every variant listed — verified 12/13 on a mixed batch of real
# mishearings and normal sentences that must NOT trigger a switch.

# Whisper inserts a comma at a natural pause ("Activate, Claude.") which
# broke the regex above outright — it requires the literal substring
# "activate claude" with a single space, so a real, fairly common way of
# saying the phrase never matched at all. Worse, the failure was silent:
# the unmatched text just fell through to a normal query, and whichever
# tier answered it (informed by the real "activate claude" exchange
# already sitting in shared conversation history) would confidently
# pattern-complete a fake "I am now operating with the deep-reasoning
# tier..." acknowledgment — convincing, but no actual switch happened
# underneath it. Confirmed live: "Activate, Claude." got exactly that fake
# reply, and the very next turn answered from Groq again. Stripping
# punctuation before matching (only for this command check, not for what
# gets sent to ask_jarvis) fixes it at the root instead of trying to
# enumerate every punctuation variant Whisper might produce.
_COMMAND_PUNCT_RE = re.compile(r"[.,!?;:]+")
_COMMAND_SPACE_RE = re.compile(r"\s+")


def _normalize_for_command(text: str) -> str:
    return _COMMAND_SPACE_RE.sub(" ", _COMMAND_PUNCT_RE.sub(" ", text)).strip()

# Barge-in: if you start talking while Jarvis is mid-sentence, stop
# immediately instead of finishing the thought. Threshold has to sit above
# whatever Jarvis's own voice measures as, leaking from the speakers back
# into the mic during playback — measured that directly rather than
# guessing: on this Mac, at normal system volume, speaker echo hits up to
# ~1365 RMS (p50 372, p90 828, p99 1227 across a real sentence). First
# attempt at 1200 sat right in that range and Jarvis interrupted itself
# constantly. 2000 gives real headroom above the measured echo ceiling.
# If you change system volume a lot, this may need retuning — no
# --calibrate mode for this one yet, but the same idea as the clap one
# would work if it needs it.
#
# Retuned with real live data (the peak-RMS printout added specifically
# for this): a genuine attempt to talk over Jarvis peaked at only 2401 —
# barely above the old 2000 threshold, and almost certainly for just one
# or two 20ms chunks, nowhere near the old 4-consecutive (80ms) sustain
# requirement, which is why it never fired. Meanwhile Jarvis's own echo
# peaked at 1506 that same session (matches the historical ~1365
# ceiling). 1800 keeps ~300 of headroom above the highest echo ever
# measured while sitting well below what a real interrupt attempt
# produces, and the sustain requirement is cut in roughly half so a
# short word like "stop" has a real chance of accumulating it.
#
# Raised again: live report of a real self-trigger — echo peaked at
# 2014 against the 1800 threshold, hit the 3-chunk sustain, and
# self-interrupted mid-sentence ("says a few words then stops"). 2014
# is meaningfully above the previous 1506 ceiling; either the setup
# changed (system volume, or possibly different machine hardware — the
# terminal prompt changed from MacBook-Air-6 to Mac) or one sample just
# doesn't capture the true ceiling the way the original multi-chunk
# p50/p90/p99 measurement did. Erring toward a self-trigger being the
# worse failure right now (breaks every single response, vs. a
# genuine interrupt just needing a retry) — 2500 gives real headroom
# above the one observed spike, but this is provisional on one data
# point and narrows the margin against the old 2401 real-interrupt
# reading. Needs a fresh full recalibration (both accidental and
# deliberate-interrupt peak values) from this actual current setup.
BARGE_IN_RMS = float(os.environ.get("JARVIS_BARGE_IN_RMS", "2500"))
BARGE_IN_SUSTAIN_CHUNKS = 3  # ~60ms of continuous loud audio (was 4/80ms)
# After an interrupt, how long to wait for you to keep talking before
# giving up (some slack in case it was a false trigger from echo, not you).
BARGE_IN_RECOVERY_WAIT_S = 2.0


def _is_speech(chunk: bytes) -> bool:
    return _rms(chunk) > SILENCE_RMS

# After Jarvis finishes speaking, how long it keeps listening for a
# follow-up before quietly giving up and going back to standby (clap /
# long spacebar hold again to start a new conversation). This is what lets
# you keep talking back and forth without re-triggering every turn.
# Raised from 7s to 12s — user reported the conversation "ends too
# easily," having to re-trigger mid-exchange because 7s wasn't enough
# time to think of a follow-up and start speaking, which is exactly the
# opposite of what makes back-and-forth feel natural.
FOLLOWUP_WAIT_S = float(os.environ.get("JARVIS_FOLLOWUP_WAIT_S", "12"))
# How long to wait for you to start talking at all right after a fresh
# wake trigger (clap/spacebar), before the wake acknowledgment). Separate
# from MAX_UTTERANCE_S, which is now a 45s hard cap on an ongoing
# utterance, not a "waiting to start" timeout.
FIRST_TURN_WAIT_S = float(os.environ.get("JARVIS_FIRST_TURN_WAIT_S", "10"))

UTTERANCE_WAV = "/tmp/jarvis_clap_utterance.wav"


class SpacebarWaker:
    """Fires `event` once the spacebar has been held for LONG_PRESS_SECONDS,
    while it's still held down — not on release. A short tap never fires."""

    def __init__(self):
        self.event = threading.Event()
        self._down = False
        self._generation = 0
        self._listener = keyboard.Listener(on_press=self._on_press, on_release=self._on_release)

    def start(self):
        self._listener.start()

    def stop(self):
        self._listener.stop()

    def _on_press(self, key):
        if key == keyboard.Key.space and not self._down:
            self._down = True
            self._generation += 1
            gen = self._generation
            threading.Timer(LONG_PRESS_SECONDS, self._check, args=(gen,)).start()

    def _check(self, gen):
        if self._down and gen == self._generation:
            self.event.set()

    def _on_release(self, key):
        if key == keyboard.Key.space:
            self._down = False


# Window within which consecutive clicks count toward a tap pattern —
# roughly standard double/triple-click timing, not a guessed number.
TAP_WINDOW_S = float(os.environ.get("JARVIS_TAP_WINDOW_S", "0.6"))
# How many quick clicks count as "tap the laptop to wake the camera".
# Deliberately NOT audio/clap-based: acoustically, tapping the chassis
# and clapping twice quickly are hard to tell apart with a simple RMS
# threshold — a real trackpad click event has no such ambiguity.
TAP_CLICKS_REQUIRED = int(os.environ.get("JARVIS_TAP_CLICKS_REQUIRED", "2"))


class TrackpadTapWaker:
    """Fires `event` when TAP_CLICKS_REQUIRED clicks land within
    TAP_WINDOW_S of each other — a deliberate fast tap pattern on the
    trackpad, used to open a brief gesture-watching camera window (see
    gesture.py) rather than the normal clap/spacebar voice wake."""

    def __init__(self):
        self.event = threading.Event()
        self._click_times = []
        self._listener = mouse.Listener(on_click=self._on_click)

    def start(self):
        self._listener.start()

    def stop(self):
        self._listener.stop()

    def _on_click(self, x, y, button, pressed):
        if not pressed:
            return
        now = time.time()
        self._click_times = [t for t in self._click_times if now - t < TAP_WINDOW_S]
        self._click_times.append(now)
        if len(self._click_times) >= TAP_CLICKS_REQUIRED:
            self._click_times.clear()
            self.event.set()


# "The app should activate jarvis" — explicit requirement, the Dashboard
# used to be read-only (it only ever read voice_state.json/routing_state
# .json, never wrote anything this process would see). The Dashboard
# (electron/main.cjs's send_dashboard_command) writes a small JSON file
# with a fresh id each click; this process has no other way to hear from
# a separate Electron window (no shared memory, no socket today), so it's
# the same one-way-local-file pattern already used in the other
# direction, just reversed. Polls on its own thread so a command can land
# mid-listening or mid-speaking, not only between turns.
DASHBOARD_COMMAND_PATH = Path.home() / ".jarvis-dashboard" / "command.json"
DASHBOARD_COMMAND_POLL_S = 0.12

# Shared across threads: _speak_interruptible's barge-in monitor and the
# Dashboard's Interrupt button both need to be able to stop the SAME
# speak() call, from different threads, without either one knowing about
# the other. See _speak_interruptible — it clears this at the start of
# every call, so a stale click from before speech started can't
# retroactively cut off a later one.
_dashboard_interrupt_event = threading.Event()
# Checked inside _record_utterance's read loop — tells it to pretend no
# silence has accumulated yet, for "I'm not finished, stop cutting me
# off." Consumed (cleared) the moment it's acted on.
_dashboard_keep_listening_event = threading.Event()


# The phone PWA can't write to this Mac's local command.json (no
# filesystem access to this machine at all), so it posts to the mobile
# API's /api/command instead, and this process has to go fetch that
# remotely — same SSH credentials jarvis_client.py already uses. Polled
# far slower than the local file (a real network round trip every
# cycle, not a local disk read) — one phone tap taking up to ~2s to
# register is a reasonable trade for not hammering SSH continuously.
MOBILE_COMMAND_REMOTE_PATH = "~/.jarvis-mobile-api/command.json"
MOBILE_COMMAND_POLL_S = 2.0

# command.json is a last-value register, not a queue — a restart used to
# forget which id it had already consumed (tracked only in process memory)
# and would re-dispatch whatever command was still sitting in the file,
# even one from hours ago (e.g. re-firing a stale INTERRUPT mid-conversation
# on respawn). DASHBOARD_CONSUMED_IDS_FILE persists the last-seen id for
# each channel across restarts; DASHBOARD_COMMAND_MAX_AGE_S additionally
# refuses to act on anything older than this regardless, using the `ts`
# both writers (electron/main.cjs, mobile_api.py) already stamp on it.
DASHBOARD_COMMAND_MAX_AGE_S = 30
DASHBOARD_CONSUMED_IDS_FILE = Path.home() / ".jarvis-dashboard" / "command.consumed.json"


class DashboardCommandWatcher:
    """Polls DASHBOARD_COMMAND_PATH (local, fast — the Electron app on
    this same Mac) and the server's mirrored command file (slow, over
    SSH — the phone PWA, which has no other way to reach this process)
    for a new command id and dispatches: 'activate' sets self.event
    (same shape as the other wake triggers, checked in main()'s idle
    loop); 'interrupt' and 'keep_listening' set the two module-level
    events above directly, since those are consumed from inside
    already-running calls (_speak_interruptible, _record_utterance)
    rather than from the idle loop."""

    def __init__(self):
        self.event = threading.Event()
        persisted = self._load_consumed_ids()
        self._last_seen_id_local = persisted.get("local")
        self._last_seen_id_remote = persisted.get("remote")
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._poll_local_loop, daemon=True)
        self._remote_thread = threading.Thread(target=self._poll_remote_loop, daemon=True)

    def start(self):
        self._thread.start()
        self._remote_thread.start()

    def stop(self):
        self._stop.set()

    @staticmethod
    def _load_consumed_ids():
        try:
            return json.loads(DASHBOARD_CONSUMED_IDS_FILE.read_text())
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {}

    def _persist_consumed_ids(self):
        try:
            DASHBOARD_CONSUMED_IDS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = DASHBOARD_CONSUMED_IDS_FILE.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({"local": self._last_seen_id_local, "remote": self._last_seen_id_remote}))
            tmp.replace(DASHBOARD_CONSUMED_IDS_FILE)
        except OSError:
            pass  # best-effort — worst case a restart replays one command, same as before this fix

    def _dispatch(self, cmd):
        if cmd == "activate":
            self.event.set()
        elif cmd == "interrupt":
            _dashboard_interrupt_event.set()
        elif cmd == "keep_listening":
            _dashboard_keep_listening_event.set()

    @staticmethod
    def _is_stale(data):
        ts = data.get("ts")
        return isinstance(ts, (int, float)) and (time.time() - ts) > DASHBOARD_COMMAND_MAX_AGE_S

    def _poll_local_loop(self):
        while not self._stop.is_set():
            try:
                data = json.loads(DASHBOARD_COMMAND_PATH.read_text())
                cmd_id = data.get("id")
                if cmd_id and cmd_id != self._last_seen_id_local:
                    self._last_seen_id_local = cmd_id
                    self._persist_consumed_ids()
                    if not self._is_stale(data):
                        self._dispatch(data.get("command"))
            except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
                pass
            time.sleep(DASHBOARD_COMMAND_POLL_S)

    def _poll_remote_loop(self):
        while not self._stop.is_set():
            try:
                out = subprocess.run(
                    [
                        "ssh", "-i", SSH_KEY,
                        "-o", "ConnectTimeout=5", "-o", "BatchMode=yes",
                        "-o", "StrictHostKeyChecking=accept-new",
                        *_SSH_MULTIPLEX_OPTS,
                        f"{SSH_USER}@{SSH_HOST}",
                        f"cat {MOBILE_COMMAND_REMOTE_PATH} 2>/dev/null",
                    ],
                    capture_output=True, text=True, timeout=10,
                )
                if out.returncode == 0 and out.stdout.strip():
                    data = json.loads(out.stdout)
                    cmd_id = data.get("id")
                    if cmd_id and cmd_id != self._last_seen_id_remote:
                        self._last_seen_id_remote = cmd_id
                        self._persist_consumed_ids()
                        if not self._is_stale(data):
                            self._dispatch(data.get("command"))
            except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError):
                pass
            time.sleep(MOBILE_COMMAND_POLL_S)


def _rms(chunk_bytes: bytes) -> float:
    n = len(chunk_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", chunk_bytes)
    return (sum(s * s for s in samples) / n) ** 0.5


def _peak(chunk_bytes: bytes) -> int:
    n = len(chunk_bytes) // 2
    if n == 0:
        return 0
    samples = struct.unpack(f"<{n}h", chunk_bytes)
    return max(abs(s) for s in samples)


# "When I click my thumb(s)... it should recognize the clicking sound and
# go back to standby" — explicitly audio this time, not the trackpad tap
# (TrackpadTapWaker above), which was deliberately built as a hardware
# event instead of an acoustic one for exactly this reason: a real
# percussive click/snap and a loud syllable are genuinely hard to tell
# apart with a plain RMS threshold. This is a best-effort heuristic for
# the one case that's actually distinguishable without a trained
# classifier — a click/snap is a near-instant transient (almost all its
# energy in a sliver of the frame) landing in an otherwise quiet moment,
# which gives it a far higher peak-to-RMS ratio ("crest factor") than
# voiced speech, whose energy is comparatively spread out across the
# frame. Checking that the previous frame was quiet too rules out
# misfiring on a percussive consonant (p/t/k) in the middle of an
# ongoing word. Deliberately biased toward missing a real click over
# cutting off real speech: a miss just means the follow-up timeout
# handles it a few seconds later, but a false trigger kills a turn
# mid-sentence. Needs real-world tuning against an actual thumb click —
# these thresholds are a reasoned starting point, not calibrated data.
CLICK_PEAK_THRESHOLD = int(os.environ.get("JARVIS_CLICK_PEAK_THRESHOLD", "9000"))
CLICK_CREST_FACTOR = float(os.environ.get("JARVIS_CLICK_CREST_FACTOR", "7.0"))
CLICK_PREV_PEAK_MAX = int(os.environ.get("JARVIS_CLICK_PREV_PEAK_MAX", "1200"))


def _is_click(chunk: bytes, prev_peak: int) -> bool:
    peak = _peak(chunk)
    if peak < CLICK_PEAK_THRESHOLD or prev_peak > CLICK_PREV_PEAK_MAX:
        return False
    rms = _rms(chunk)
    crest = peak / (rms + 1e-6)
    return crest >= CLICK_CREST_FACTOR


def _read_exact(stream, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        piece = stream.read(n - len(buf))
        if not piece:
            break
        buf.extend(piece)
    return bytes(buf)


def _drain_backlog(proc):
    """Discard whatever built up in the pipe while we were busy speaking/
    thinking (ffmpeg keeps writing to a bounded OS pipe buffer even while
    nobody's reading) — otherwise we'd process stale audio, including a
    chance of Jarvis's own voice leaking back through the mic."""
    fd = proc.stdout.fileno()
    os.set_blocking(fd, False)
    try:
        while True:
            try:
                chunk = os.read(fd, 65536)
                if not chunk:
                    break
            except BlockingIOError:
                break
    finally:
        os.set_blocking(fd, True)


def _open_mic_stream():
    return subprocess.Popen(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-i", f":{AUDIO_DEVICE}",
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-f", "s16le", "-",
        ],
        stdout=subprocess.PIPE,
    )


def _kill_stream(proc):
    """SIGTERM, then SIGKILL if it doesn't die — a stale ffmpeg process left
    holding the avfoundation device will starve the next run of audio, which
    looks exactly like 'claps aren't triggering'. Belt-and-suspenders: this
    is called both from normal shutdown and from an explicit signal handler,
    since a blocked pipe read can occasionally swallow the first SIGINT."""
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            pass


def _trigger_session_reflection(session_start_ts: float):
    """Fires learning.py as a detached background process on shutdown —
    NOT awaited here, since a cloud Claude reflection call plus an SSH
    sync can take 10-20s and there's no reason to make the user wait on
    that just to quit. start_new_session=True detaches it from this
    process's session so it keeps running even after always_on.py exits
    (a plain background thread would die with the parent process)."""
    try:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).parent / "learning.py"), str(session_start_ts)],
            start_new_session=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass  # best-effort — never let this block shutdown


def _install_signal_cleanup(proc, waker=None, tap_waker=None, dashboard_watcher=None):
    def _handler(signum, frame):
        write_state("idle", 0.0)
        if waker is not None:
            waker.stop()
        if tap_waker is not None:
            tap_waker.stop()
        if dashboard_watcher is not None:
            dashboard_watcher.stop()
        _kill_stream(proc)
        sys.exit(0)

    signal.signal(signal.SIGINT, _handler)
    signal.signal(signal.SIGTERM, _handler)


def _write_wav(path: str, pcm_bytes: bytes):
    with wave.open(path, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm_bytes)


# How many pre-speech chunks to always keep buffered, so the moment voice
# crosses the threshold we can prepend what came just before it. Real words
# often start quiet and ramp up — "stand down" got captured as just "down"
# once, most likely because the soft onset of "stand" sat under SILENCE_RMS
# until it was already partway through the word. 8 chunks = 160ms, enough
# for a typical soft onset without meaningfully risking picking up noise
# from well before you actually started talking.
_PREROLL_CHUNKS = 8


def _transcribe_buffer(buffer: bytearray) -> str:
    _write_wav(UTTERANCE_WAV, bytes(buffer))
    try:
        return transcribe(UTTERANCE_WAV)
    finally:
        Path(UTTERANCE_WAV).unlink(missing_ok=True)


def _record_utterance(proc, max_wait_s: float):
    """Wait up to max_wait_s for you to actually start talking. Once you
    do, a pause no longer ends the turn by itself — it's just a checkpoint:
    whatever's been said so far gets transcribed and checked for a
    trailing "Jarvis". Found it -> that's the real end, word stripped.
    Not found -> keep recording, no matter how long the pause was, up to
    STOPWORD_FALLBACK_SILENCE_MS of silence (you forgot to say it) or
    MAX_UTTERANCE_S (hard cap, a safety net not a normal limit).

    Returns the final transcript (str, possibly empty), or None if nobody
    spoke at all within max_wait_s — that's "conversation's over", not a
    failed turn."""
    buffer = bytearray()
    preroll = deque(maxlen=_PREROLL_CHUNKS)
    silence_ms = 0
    heard_voice = False
    checked_this_pause = False
    last_text = ""
    start = time.time()
    prev_peak = 0

    while True:
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            break

        if _is_click(chunk, prev_peak):
            print("(thumb click detected — back to standby)")
            return None
        prev_peak = _peak(chunk)

        speech = _is_speech(chunk)

        if not heard_voice:
            if speech:
                heard_voice = True
                for pre_chunk in preroll:
                    buffer.extend(pre_chunk)
                buffer.extend(chunk)
            else:
                preroll.append(chunk)
                if (time.time() - start) > max_wait_s:
                    return None  # gave up waiting — nobody spoke, not an error
            continue

        buffer.extend(chunk)

        # "I'm not finished" — the Dashboard's Keep Listening button.
        # Pretends no silence has accumulated yet, same as if you'd just
        # kept talking, so the checkpoint/fallback timers below don't
        # treat this pause as the end of your turn.
        if _dashboard_keep_listening_event.is_set():
            _dashboard_keep_listening_event.clear()
            silence_ms = 0
            checked_this_pause = False

        if speech:
            silence_ms = 0
            checked_this_pause = False
        else:
            silence_ms += CHUNK_MS

            if not checked_this_pause and silence_ms >= CHECKPOINT_SILENCE_MS:
                checked_this_pause = True
                last_text = _transcribe_buffer(buffer)
                if _ends_with_stopword(last_text):
                    return _strip_stopword(last_text)

            if silence_ms >= STOPWORD_FALLBACK_SILENCE_MS:
                return last_text  # gave up waiting for "Jarvis" — use last checkpoint

        if (time.time() - start) > MAX_UTTERANCE_S:
            if not checked_this_pause:
                last_text = _transcribe_buffer(buffer)
            return _strip_stopword(last_text) if _ends_with_stopword(last_text) else last_text

    return last_text or None


def _barge_in_monitor(proc, stop_flag: threading.Event, interrupt_event: threading.Event):
    """Runs in a background thread for as long as speak() is playing audio.
    Reads the SAME mic stream speak() itself isn't touching right now (it's
    blocked synthesizing/playing), watching for sustained loud audio that
    means you're talking over it.

    Reported live as broken: user talked over Jarvis and it "did not
    listen, kept going." The old version reset the consecutive-loud-chunk
    counter to 0 on ANY single quiet chunk — real speech isn't a flat
    tone, it has brief dips between syllables/plosives well within a
    single word, so a strict "4 in a row, zero tolerance" requirement
    could plausibly never accumulate during a short interruption attempt
    like "wait" or "stop." Switched to a leaky counter (decay by 1 on a
    quiet chunk instead of hard reset to 0) — tolerates natural dips
    without needing sustained volume any louder than before, so it
    shouldn't increase false triggers from a single loud echo blip.
    Also now prints the peak RMS seen each turn — no way to verify a
    threshold-related audio bug without real numbers from a live attempt,
    same discipline as how BARGE_IN_RMS itself was originally set."""
    consecutive = 0
    peak = 0.0
    total_chunks = 0
    loud_chunks = 0
    max_consecutive = 0
    while not stop_flag.is_set():
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            break
        level = _rms(chunk)
        peak = max(peak, level)
        total_chunks += 1
        if level > BARGE_IN_RMS:
            loud_chunks += 1
            consecutive += 1
            max_consecutive = max(max_consecutive, consecutive)
            if consecutive >= BARGE_IN_SUSTAIN_CHUNKS:
                interrupt_event.set()
                break
        else:
            consecutive = max(0, consecutive - 1)
    print(
        f"(barge-in monitor: peak={peak:.0f}, threshold={BARGE_IN_RMS:.0f}, "
        f"loud_chunks={loud_chunks}/{total_chunks}, max_consecutive={max_consecutive}"
        f"/{BARGE_IN_SUSTAIN_CHUNKS})"
    )


# Emergency off switch: user hit a self-interruption loop that kept
# escalating (peaks climbing 2014 -> 3344 -> 3715 -> 4546 -> 5515 -> 5905
# across successive tests) even as BARGE_IN_RMS was raised repeatedly to
# match — a moving target, not a stable value to tune against, and
# every raise just got outrun by the next reading. Root cause (likely
# the Omi app's background audio capture, still unconfirmed since it
# wasn't actually tested with Omi closed) needs real diagnosis, but the
# user needed a working assistant immediately, not another guessed
# number. Default OFF (barge-in runs normally) — set to skip the
# monitor thread entirely, so self-interruption is categorically
# impossible regardless of what the mic reads, at the cost of losing
# the ability to interrupt Jarvis by talking over it. Re-enable once
# the actual cause is found and fixed.
BARGE_IN_DISABLED = os.environ.get("JARVIS_DISABLE_BARGE_IN", "").lower() in ("1", "true", "yes")


def _speak_interruptible(proc, text: str) -> bool:
    """speak(), but stops immediately if you start talking over it OR hit
    the Dashboard's Interrupt button. Returns True if it actually got
    interrupted (vs finishing normally).

    Reuses the shared _dashboard_interrupt_event as the actual event
    passed to speak() (cleared first, in case a stale click from before
    this call never got consumed) so the Dashboard's Interrupt button
    works independently of BARGE_IN_DISABLED — audio-detected and
    UI-clicked interruption are two different sources feeding the same
    switch, and disabling the audio one was never meant to disable the
    other."""
    _dashboard_interrupt_event.clear()
    interrupt_event = _dashboard_interrupt_event

    if BARGE_IN_DISABLED:
        speak(text, interrupt_event=interrupt_event)
        return interrupt_event.is_set()

    stop_flag = threading.Event()
    monitor = threading.Thread(
        target=_barge_in_monitor, args=(proc, stop_flag, interrupt_event), daemon=True
    )
    monitor.start()
    try:
        speak(text, interrupt_event=interrupt_event)
    finally:
        stop_flag.set()
        monitor.join(timeout=1)
    return interrupt_event.is_set()


def _verify_owner_at_startup(proc):
    """Security gate, run once at process launch, before any commands are
    accepted — user's explicit requirement. Loops until a real match is
    confirmed; "look again" retries a failed attempt (bad lighting, not
    in frame yet, stepped away) without needing to restart the whole
    script. Fails CLOSED (no match / no face -> locked out) with one
    deliberate exception: nothing enrolled yet. Locking the owner out of
    their own never-configured system forever would be a worse failure
    than letting an unconfigured gate pass through — there's nothing to
    verify against yet, not a security hole being opened."""
    import vision  # lazy: only needed when this opt-in gate actually runs
    while True:
        print("Verifying identity via camera before accepting any commands…")
        write_state("thinking", 0.4)
        status, message = vision.recognize_face()
        print(f"[{status}] {message}")

        if status == "not_enrolled":
            _speak_interruptible(proc, "No face is enrolled yet, sir, so I can't verify — say 'enroll my face' once you're ready.")
            return
        if status == "match":
            _speak_interruptible(proc, message)
            return

        # no_match or no_face — locked out until "look again"
        interrupted = _speak_interruptible(proc, "I'm sorry, I cannot accept any commands right now.")
        if not interrupted:
            _drain_backlog(proc)

        while True:
            write_state("listening", 0.5)
            retry_text = _record_utterance(proc, STARTUP_VERIFY_WAIT_S)
            if retry_text and _LOOK_AGAIN_RE.search(_normalize_for_command(retry_text)):
                break  # re-check the camera
            # silence, or anything that isn't "look again" — no commands
            # are accepted while locked out, so just keep waiting rather
            # than re-announcing the lockout on every stray utterance


# Runs `npm run electron` (dev mode) rather than opening a packaged .app
# — there's no signed/packaged build in this workflow (that's what
# `npm run electron:build` + electron-builder would produce, never run
# here), and dev mode always picks up the latest source anyway, which
# matters given how often this UI has changed this session. Dev mode has
# none of a packaged .app's "open an already-running app just brings it
# forward" behavior, so that's handled explicitly below via a PID file —
# without it, every always_on.py restart during a dev/test session would
# spawn a new, separate Dashboard window on top of any still open.
DASHBOARD_DIR = Path(__file__).parent.parent / "dashboard"
DASHBOARD_PID_FILE = Path.home() / ".jarvis-dashboard" / "electron.pid"


def _dashboard_already_running() -> bool:
    try:
        pid = int(DASHBOARD_PID_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        os.kill(pid, 0)  # signal 0: existence check only, nothing is actually sent
        return True
    except OSError:
        return False


def _open_dashboard():
    if _dashboard_already_running():
        print("(dashboard already running — not launching a second one)")
        return
    if not DASHBOARD_DIR.exists():
        print(f"(dashboard directory not found at {DASHBOARD_DIR} — skipping)")
        return
    try:
        # `npm run electron` is `npm run build && electron .` as one shell
        # chain — npm's own process stays alive for exactly as long as the
        # Electron window does (it's the last command in the chain, not
        # backgrounded), so npm's pid is a reliable proxy for "is the
        # dashboard still open", not just "did launching it succeed."
        proc = subprocess.Popen(
            ["npm", "run", "electron"],
            cwd=str(DASHBOARD_DIR),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        DASHBOARD_PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        DASHBOARD_PID_FILE.write_text(str(proc.pid))
    except Exception as e:
        print(f"(couldn't open dashboard: {e})")


def _handle_wake(proc, conversation_id: str, trigger: str = "clap"):
    label = {
        "clap": "\U0001f44f Clap",
        "spacebar": "⌨️  Spacebar held",
        "gesture": "\U0001f90c Gesture",
        "dashboard": "\U0001f5a5️ Dashboard ACTIVATE",
    }.get(trigger, trigger)
    print(f"\n{label} — waking up…")
    conversation_start_ts = time.time()

    # Acknowledge immediately so you know it's your turn to talk, before we
    # start actually recording the real question. Only said once per
    # conversation, not on every follow-up turn.
    write_state("speaking", 0.6)
    interrupted = _speak_interruptible(proc, _pick_wake_ack())
    if not interrupted:
        _drain_backlog(proc)  # discard mic input captured while the ack played

    first_turn = True
    mode = "normal"  # "normal" (fast/cloud auto-routed) | "cloud" | "local"
    # Background cloud-tier dispatches (mode == "cloud" branch below)
    # awaiting announcement — each is {"done": threading.Event(), "answer": str}.
    pending_cloud_results = []
    # Set the moment mode switches to "local", consumed on the very next
    # local-tier turn (and only that one) — local Claude otherwise has
    # zero visibility into what fast/cloud said earlier in this same
    # conversation (they live in a shared server-side transcript; local
    # runs its own separate `claude --resume` session on this Mac). Once
    # consumed, local's own --resume carries its own history forward on
    # its own, so re-fetching the same prefix every turn would just burn
    # tokens for no new information.
    local_context_pending = False
    while True:
        # Announce any finished background result right here — the one
        # natural conversational gap, never mid-sentence. Only when we're
        # not already mid-interruption-recovery, so it can't stack
        # confusingly on top of that; if announcing itself gets talked
        # over, stop announcing and fall into the same interrupted-catch
        # path a normal turn would, leaving whatever's left still pending
        # for the next gap.
        if not interrupted and pending_cloud_results:
            still_pending = list(pending_cloud_results)
            for p in pending_cloud_results:
                if not p["done"].is_set():
                    continue
                print(f"Jarvis (background result): {p['answer']}")
                write_state("speaking", 0.6)
                was_interrupted = _speak_interruptible(proc, p["answer"])
                still_pending.remove(p)
                if was_interrupted:
                    interrupted = True
                    break
                _drain_backlog(proc)
            pending_cloud_results = still_pending

        if interrupted:
            # You talked over Jarvis — it already stopped, now catch the
            # rest of what you're saying rather than making you re-trigger.
            # The first ~80ms was consumed by the barge-in detector itself
            # (not saved), so this picks up an instant after that.
            print("(interrupted — listening)")
            write_state("listening", 0.5)
            text = _record_utterance(proc, BARGE_IN_RECOVERY_WAIT_S)
        else:
            print("Listening…" if first_turn else "Listening for a follow-up…")
            write_state("listening", 0.5)
            wait_s = FIRST_TURN_WAIT_S if first_turn else FOLLOWUP_WAIT_S
            text = _record_utterance(proc, wait_s)
        first_turn = False

        if text is None:
            print("--- no follow-up, back to standby ---")
            break

        write_state("thinking", 0.2)

        if not text:
            print("(heard something, couldn't make out words)")
            interrupted = _speak_interruptible(proc, "Sorry, I didn't quite catch that, sir.")
            if not interrupted:
                _drain_backlog(proc)
            continue  # stay in the conversation, give it another go

        print(f"You said: {text}")
        command_text = _normalize_for_command(text)

        switch = command_router.classify_command(command_text)
        if switch is None:
            # AI router unreachable (network down) — fall back to regex
            # rather than losing mode-switching entirely.
            print("(command router unreachable — falling back to regex)")
            if _ACTIVATE_LOCAL_RE.search(command_text):
                switch = "local"
            elif _ACTIVATE_CLAUDE_RE.search(command_text):
                switch = "cloud"
            elif _DEACTIVATE_CLAUDE_RE.search(command_text):
                switch = "normal"
            else:
                switch = "none"

        if switch == "cloud":
            mode = "cloud"
            print("(mode: CLOUD — every turn now forced to the cloud reasoning tier)")
            interrupted = _speak_interruptible(proc, "Claude's active, sir. Every turn now goes there until you switch back.")
            if not interrupted:
                _drain_backlog(proc)
            continue  # control command, not a question — don't send it anywhere
        if switch == "local":
            mode = "local"
            local_context_pending = True
            print("(mode: LOCAL — every turn now runs on this machine)")
            interrupted = _speak_interruptible(proc, "Switched to the local machine, sir. Every turn now runs here until you switch back.")
            if not interrupted:
                _drain_backlog(proc)
            continue
        if switch == "normal" and mode != "normal":
            mode = "normal"
            print("(mode: NORMAL — back to normal routing)")
            interrupted = _speak_interruptible(proc, "Back to normal, sir.")
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _LOG_SUMMARY_RE.search(command_text):
            print("(local intent: summarizing recent activity log)")
            write_state("thinking", 0.4)
            recent = _recent_activity_text()
            if not recent:
                answer = "There's nothing in the activity log yet, sir."
            else:
                instruction = (
                    "Here is the real recent activity log — this is the only "
                    "data that exists, never add anything beyond it:\n" + recent +
                    "\n\nIn 2-3 short spoken sentences, summarize what's been "
                    "happening. Lead with the most recent thing."
                )
                answer = ask_jarvis(instruction, session_id=conversation_id, force_simple=True)
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _TASK_UPDATE_RE.search(command_text):
            print("(local intent: checking background task status)")
            write_state("thinking", 0.4)
            answer = check_task_status(session_id=conversation_id)
            log_activity({"type": "task_status", "result": answer})
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _KNOWLEDGE_UPDATE_RE.search(command_text):
            print("(local intent: syncing + reindexing knowledge base)")
            write_state("thinking", 0.4)
            answer = sync_and_reindex_knowledge()
            log_activity({"type": "knowledge", "action": "update", "result": answer})
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _PERSONA_UPDATE_RE.search(command_text):
            print("(local intent: syncing persona documents)")
            write_state("thinking", 0.4)
            answer = sync_persona_docs()
            log_activity({"type": "persona", "action": "update", "result": answer})
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _OPEN_DASHBOARD_RE.search(command_text):
            print("(local intent: opening dashboard)")
            write_state("thinking", 0.3)
            _open_dashboard()
            answer = "Done, sir — the dashboard should be opening now."
            log_activity({"type": "dashboard", "action": "open", "result": answer})
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _KNOWLEDGE_QUERY_RE.search(command_text):
            print("(local intent: querying knowledge base)")
            write_state("thinking", 0.4)
            answer = ask_knowledge_base(text)
            log_activity({"type": "knowledge", "action": "query", "user": text, "result": answer})
            threading.Thread(
                target=record_local_turn,
                args=(conversation_id, text, answer),
                daemon=True,
            ).start()
            print(f"Jarvis: {answer}")
            interrupted = _speak_interruptible(proc, answer)
            if not interrupted:
                _drain_backlog(proc)
            continue

        # Face enroll/recognize and "describe what you see" local intents
        # (_FACE_ENROLL_RE / _FACE_RECOGNIZE_RE / _VISION_DESCRIBE_RE) are
        # disabled here per explicit request: "eliminate every command
        # that required camera access... no feature involving camera for
        # now." The regexes and vision.py itself are untouched (not
        # deleted) — this is just the three dispatch branches that used
        # to route to them removed, so a match now just falls through to
        # the normal Claude dispatch below (which will correctly say it
        # has no camera tool, same as any other unsupported request).

        if _FILE_FIX_RE.search(text):
            print("(local intent: checking inbox for a file to fix)")
            write_state("thinking", 0.4)
            answer = fix_file(latest_inbox_file(), session_id=conversation_id)
        elif mode == "local":
            print("(routing to local Claude — real Bash/file access on this Mac)")
            routing_state.write_state("local", "Local Claude (this Mac)", text)
            local_text = text
            if local_context_pending:
                # One-time fetch, right after switching — local's own
                # --resume carries its own history forward from here, so
                # this never re-fetches on later turns in the same local
                # stretch (see the flag's own comment, set near "normal").
                local_context_pending = False
                cross_tier_context = fetch_cross_tier_context(conversation_id)
                if cross_tier_context:
                    local_text = cross_tier_context + text
            # ask_jarvis (the cloud/SSH path) catches its own TimeoutExpired
            # and returns a clean string (jarvis_client.py) — this call
            # never did, so a hung local `claude` CLI call used to raise
            # all the way up through this loop and main()'s own
            # except KeyboardInterrupt (which doesn't catch it), killing
            # the entire always-on process over one slow local turn.
            try:
                answer = local_claude.ask_local_claude(local_text, session_id=conversation_id)
            except subprocess.TimeoutExpired:
                answer = "That local request timed out, sir — it may still be running, but I've stopped waiting on it."
            except Exception as e:
                answer = f"Something went wrong running that locally, sir: {e}"
            log_activity({"type": "conversation", "user": text, "assistant": answer, "tier": "local"})
            # Sync into the server's shared transcript so the fast/cloud
            # tiers actually know this happened — fire-and-forget in the
            # background, no reason to delay speaking the answer for an
            # SSH round trip that only affects future turns.
            threading.Thread(
                target=record_local_turn,
                args=(conversation_id, text, answer),
                daemon=True,
            ).start()
        elif mode == "cloud":
            # "I want jarvis to respond immediately, even if it's still
            # working on it... keep giving me updates" — speak a quick ack
            # now (no network round trip, so it's actually immediate),
            # dispatch the real request in a background thread, and let
            # the top of this loop announce the result the next time
            # there's a natural gap. Not blocking on ask_jarvis at all
            # here is the real fix — the old behavior blocked on it
            # (up to the ~25s background-task poll inside
            # escalate_to_claude) before saying a single word.
            print("(routing to cloud Claude — immediate ack, running in the background)")
            write_state("speaking", 0.6)
            interrupted = _speak_interruptible(proc, _pick_cloud_ack())
            if not interrupted:
                _drain_backlog(proc)
            log_activity({
                "type": "conversation", "user": text,
                "assistant": "(dispatched to background — announced when done)", "tier": "cloud",
            })
            pending = {"done": threading.Event(), "answer": None}

            def _run_cloud_turn(text=text, pending=pending):
                pending["answer"] = ask_jarvis(text, session_id=conversation_id, force_reasoning=True)
                pending["done"].set()

            threading.Thread(target=_run_cloud_turn, daemon=True).start()
            pending_cloud_results.append(pending)
            continue  # already acknowledged above — skip the shared print/speak tail
        else:
            answer = ask_jarvis(text, session_id=conversation_id)
            log_activity({"type": "conversation", "user": text, "assistant": answer, "tier": mode})
        print(f"Jarvis: {answer}")
        interrupted = _speak_interruptible(proc, answer)
        if not interrupted:
            _drain_backlog(proc)
        # loop straight back into listening — no re-trigger needed for a
        # follow-up, this is what makes it feel like an actual conversation

    write_state("idle", 0.0)
    # "Every time I stop talking" — reflect at the end of THIS conversation,
    # not just when the whole process eventually shuts down. A single
    # always_on.py run can span many separate conversations (clap, talk,
    # go quiet, clap again, hours later) — waiting for Ctrl+C to reflect
    # would mean lessons from an early conversation don't exist yet for a
    # much later one in the same run.
    _trigger_session_reflection(conversation_start_ts)


def _sanity_check(proc, seconds: float = 1.0):
    """Confirm the mic stream is actually carrying signal before we start
    waiting for claps — silent/near-zero levels almost always mean either
    the wrong JARVIS_AUDIO_DEVICE index, or Terminal/your Python interpreter
    doesn't have mic permission (System Settings -> Privacy & Security ->
    Microphone) and ffmpeg is silently producing empty audio."""
    n_chunks = int(seconds * 1000 / CHUNK_MS)
    levels = []
    for _ in range(n_chunks):
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            break
        levels.append(_rms(chunk))
    peak = max(levels) if levels else 0.0
    avg = sum(levels) / len(levels) if levels else 0.0
    print(f"mic sanity check: avg={avg:.1f} peak={peak:.1f} ({len(levels)} chunks)")
    if peak < 5.0:
        print(
            "\n⚠️  The mic stream looks silent. Before assuming the clap\n"
            "   detector is broken, check:\n"
            "   1. System Settings -> Privacy & Security -> Microphone -> is your\n"
            "      terminal app (Terminal/iTerm/VS Code) allowed?\n"
            "   2. Is JARVIS_AUDIO_DEVICE pointing at a real mic? Run:\n"
            "        ffmpeg -f avfoundation -list_devices true -i \"\"\n"
            "      and check the 'AVFoundation audio devices' list — set\n"
            "      JARVIS_AUDIO_DEVICE to that index if it's not 1.\n"
        )
    return avg


def _calibrate():
    """Live RMS + zero-crossing-rate meter — run this, make both a real
    clap and a dull touch/knock/bump on the mic or table, and read off
    real numbers instead of guessing at thresholds. The amplitude alone
    can't tell those two apart (that was the actual bug — "slightest
    touch sound activates it"); zcr is what's supposed to, so watch
    specifically whether claps and touches land on different sides of
    JARVIS_CLAP_MIN_ZCR here."""
    print("=== JARVIS clap calibration ===")
    print(f"mic: {describe_selected_device(AUDIO_DEVICE)} (override with JARVIS_AUDIO_DEVICE)")
    print("Watch the numbers. Try a real clap AND a light touch/knock on the mic or table —")
    print("compare their zcr, not just their level.")
    print("Ctrl+C to stop.\n")
    proc = _open_mic_stream()
    _install_signal_cleanup(proc)
    _sanity_check(proc, seconds=0.5)
    noise_floor = 300.0
    last_print = 0.0
    try:
        while True:
            chunk = _read_exact(proc.stdout, CHUNK_BYTES)
            if not chunk:
                break
            level = _rms(chunk)
            threshold = max(CLAP_ABS_MIN, noise_floor * CLAP_RATIO)
            if level < threshold:
                noise_floor = noise_floor * 0.98 + level * 0.02

            now = time.time()
            is_spike = level > threshold
            zcr = _zero_crossing_rate(chunk) if is_spike else 0.0
            hit = is_spike and zcr >= CLAP_MIN_ZCR
            if is_spike or (now - last_print) > 0.15:
                last_print = now
                bar = "#" * min(60, int(level / 50))
                if is_spike:
                    marker = "  <-- WOULD TRIGGER (clap-like)" if hit else "  <-- spike, but NOT clap-like (zcr too low)"
                else:
                    marker = ""
                zcr_str = f"zcr={zcr:.2f}  " if is_spike else ""
                print(f"level={level:7.1f}  floor={noise_floor:7.1f}  threshold={threshold:7.1f}  {zcr_str}{bar}{marker}")
    except KeyboardInterrupt:
        pass
    finally:
        _kill_stream(proc)
        print("\nStopped.")


def main():
    print("=== JARVIS always-on (clap OR long spacebar hold to talk) ===")
    print(f"mic: {describe_selected_device(AUDIO_DEVICE)} (override with JARVIS_AUDIO_DEVICE)")
    print(f"clap threshold: {CLAP_RATIO}x ambient noise floor, min {CLAP_ABS_MIN}")
    print(f"spacebar: hold for {LONG_PRESS_SECONDS}s (needs Accessibility permission")
    print("  for your terminal app — System Settings -> Privacy & Security ->")
    print("  Accessibility — if it doesn't seem to respond the first time)")
    print("Ctrl+C to quit.\n")
    if BARGE_IN_DISABLED:
        print("⚠️  Barge-in DISABLED (JARVIS_DISABLE_BARGE_IN set) — you can't interrupt Jarvis by talking over it right now. Unset this once the self-interruption cause is fixed.\n")
    write_state("idle", 0.0)

    # Mic has to be open before the biometric gate below can speak or
    # listen for a "look again" retry — moved ahead of everything else
    # that used to run first, since the gate is now genuinely the first
    # thing that happens, per the user's explicit requirement.
    proc = _open_mic_stream()
    avg = _sanity_check(proc, seconds=1.0)
    noise_floor = max(avg, 50.0)
    prev_level = 0.0
    last_clap_time = 0.0

    # Explicit request: "eliminate every command that required camera
    # access or biometric... no feature involving camera for now." The
    # gate used to run by default (opt-out via JARVIS_SKIP_BIOMETRIC_GATE)
    # — now it's the reverse: off by default, opt back IN via
    # JARVIS_REQUIRE_BIOMETRIC_GATE if camera features come back later.
    # _verify_owner_at_startup (below) is untouched, not deleted — this is
    # a toggle, "for now," not a removal of the capability.
    if os.environ.get("JARVIS_REQUIRE_BIOMETRIC_GATE", "").lower() in ("1", "true", "yes"):
        _verify_owner_at_startup(proc)
    else:
        print("(biometric verification disabled — no camera features active right now)\n")

    # "Activating" Jarvis should mean it actually re-reads its own past
    # lessons right now, not that it's trusting a background sync from
    # whenever the last session happened to end (that sync is
    # fire-and-forget on shutdown — see _trigger_session_reflection — and
    # could fail silently or just not have finished yet if you relaunch
    # quickly). Blocking here is fine: this runs once before the mic loop
    # even starts, not mid-conversation.
    lesson_count = sync_lessons_now()
    if lesson_count:
        print(f"Learned lessons: synced {lesson_count} recent entries from learning_log.md to the server.\n")
    else:
        print("Learned lessons: none recorded yet (or sync failed) — starting fresh.\n")

    # "When I run the above command, the dashboard should also start" —
    # fire-and-forget (Popen returns immediately; _open_dashboard already
    # no-ops if one's already running), so this doesn't delay the mic loop
    # starting up while `npm run build && electron .` does its own thing
    # in the background.
    _open_dashboard()

    # One Claude session for the entire life of this process, not one per
    # wake trigger — a long pause between claps shouldn't mean Jarvis has
    # forgotten what you talked about an hour ago. Only resets if you
    # actually restart this script; a full context window is handled by
    # Claude Code's own autocompact within the session, not by us minting
    # a new one.
    conversation_id = str(uuid.uuid4())
    # Tracks when the CURRENTLY in-progress (or about-to-start) wake began
    # — _handle_wake() reflects on its own conversation as soon as it ends
    # (see its own _trigger_session_reflection call), so this is only a
    # safety net for the edge case of Ctrl+C landing mid-conversation,
    # before _handle_wake() reaches that point itself. Updated right
    # before each wake, not left as the original process-start time — if
    # it stayed fixed at process start, this shutdown-time call would
    # re-reflect on every already-reflected conversation from the whole
    # run, producing duplicate entries.
    current_wake_start_ts = time.time()
    print(f"session: {conversation_id} (persists until this process restarts)\n")

    waker = SpacebarWaker()
    waker.start()
    # TrackpadTapWaker/gesture.py (trackpad tap -> camera gesture window)
    # disabled per explicit request: "eliminate every command that
    # required camera access... no feature involving camera for now."
    # Not instantiated at all, so the trackpad listener never even starts
    # — the class itself is untouched for later re-enabling.
    dashboard_watcher = DashboardCommandWatcher()
    dashboard_watcher.start()
    _install_signal_cleanup(proc, waker, None, dashboard_watcher)

    try:
        while True:
            chunk = _read_exact(proc.stdout, CHUNK_BYTES)
            if not chunk:
                break
            level = _rms(chunk)
            threshold = max(CLAP_ABS_MIN, noise_floor * CLAP_RATIO)

            # Only adapt the noise floor while things are clearly quiet, so
            # a loud room doesn't teach the detector to ignore loud sounds.
            if level < threshold:
                noise_floor = noise_floor * 0.98 + level * 0.02

            is_spike = level > threshold
            was_quiet = prev_level <= threshold
            prev_level = level

            now = time.time()
            candidate_clap = is_spike and was_quiet and (now - last_clap_time) > CLAP_COOLDOWN_S
            # Only bother with the zero-crossing check on an actual candidate
            # spike — no reason to pay for it on every single quiet chunk.
            clap_hit = candidate_clap and _is_clap_sound(chunk)
            space_hit = waker.event.is_set()
            dashboard_hit = dashboard_watcher.event.is_set()

            if clap_hit or space_hit or dashboard_hit:
                waker.event.clear()
                dashboard_watcher.event.clear()
                last_clap_time = now
                current_wake_start_ts = time.time()
                trigger = "clap" if clap_hit else "spacebar" if space_hit else "dashboard"
                _handle_wake(proc, conversation_id, trigger=trigger)
                prev_level = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        write_state("idle", 0.0)
        waker.stop()
        _kill_stream(proc)
        _trigger_session_reflection(current_wake_start_ts)
        print("\nShutting down.")


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        _calibrate()
    else:
        main()
