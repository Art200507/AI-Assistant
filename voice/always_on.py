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
--calibrate). The spacebar listener needs Accessibility permission the
first time (System Settings -> Privacy & Security -> Accessibility -> allow
your terminal app) since it's a global hotkey, not tied to any window.
"""
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

from pynput import keyboard

from file_bridge import fix_file, latest_inbox_file
from jarvis_client import ask_jarvis
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
# Whisper (base.en) doesn't reliably transcribe "Jarvis" — confirmed from
# real use, not guessed: it came back as "Chavez" and "Jorvis" in the same
# short session. Rather than pick a whole new trigger word, accept the
# actual observed mishearings as equivalent — a phonetic match is still a
# correct match here, the point is detecting intent, not exact spelling.
# Add more here the moment a new mishearing shows up.
STOP_WORD_VARIANTS = {"jarvis", "chavez", "jorvis", "javis", "jarviss", "charvis", "travis"}
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

# Explicit manual override for which tier answers, instead of leaving it to
# classify(). Added because auto-routing between two genuinely different
# models sharing one "Jarvis" identity produces a jarring effect: each tier
# is honest about itself individually, but that means Jarvis can seem to
# contradict its own earlier claims mid-conversation whenever the tier
# switches unpredictably. Explicit control means you always know which one
# you're talking to. Resets to off at the start of every new conversation
# (new wake trigger) — deliberately not sticky across separate claps, so
# you can't accidentally leave it on and forget.
_ACTIVATE_CLAUDE_RE = re.compile(
    r"\b(activate claude|switch to claude|use claude( code)?|talk to claude)\b",
    re.IGNORECASE,
)
_DEACTIVATE_CLAUDE_RE = re.compile(
    r"\b(deactivate claude|switch back|back to (normal|fast)( mode)?|"
    r"stop using claude|exit claude( mode)?)\b",
    re.IGNORECASE,
)

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
BARGE_IN_RMS = float(os.environ.get("JARVIS_BARGE_IN_RMS", "2000"))
BARGE_IN_SUSTAIN_CHUNKS = 4  # ~80ms of continuous loud audio
# After an interrupt, how long to wait for you to keep talking before
# giving up (some slack in case it was a false trigger from echo, not you).
BARGE_IN_RECOVERY_WAIT_S = 2.0


def _is_speech(chunk: bytes) -> bool:
    return _rms(chunk) > SILENCE_RMS

# After Jarvis finishes speaking, how long it keeps listening for a
# follow-up before quietly giving up and going back to standby (clap /
# long spacebar hold again to start a new conversation). This is what lets
# you keep talking back and forth without re-triggering every turn.
FOLLOWUP_WAIT_S = float(os.environ.get("JARVIS_FOLLOWUP_WAIT_S", "7"))
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


def _rms(chunk_bytes: bytes) -> float:
    n = len(chunk_bytes) // 2
    if n == 0:
        return 0.0
    samples = struct.unpack(f"<{n}h", chunk_bytes)
    return (sum(s * s for s in samples) / n) ** 0.5


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


def _install_signal_cleanup(proc, waker=None):
    def _handler(signum, frame):
        write_state("idle", 0.0)
        if waker is not None:
            waker.stop()
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

    while True:
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            break
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
    means you're talking over it."""
    consecutive = 0
    while not stop_flag.is_set():
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            return
        if _rms(chunk) > BARGE_IN_RMS:
            consecutive += 1
            if consecutive >= BARGE_IN_SUSTAIN_CHUNKS:
                interrupt_event.set()
                return
        else:
            consecutive = 0


def _speak_interruptible(proc, text: str) -> bool:
    """speak(), but stops immediately if you start talking over it. Returns
    True if it actually got interrupted (vs finishing normally)."""
    interrupt_event = threading.Event()
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


def _handle_wake(proc, conversation_id: str, trigger: str = "clap"):
    label = "\U0001f44f Clap" if trigger == "clap" else "⌨️  Spacebar held"
    print(f"\n{label} — waking up…")

    # Acknowledge immediately so you know it's your turn to talk, before we
    # start actually recording the real question. Only said once per
    # conversation, not on every follow-up turn.
    write_state("speaking", 0.6)
    interrupted = _speak_interruptible(proc, _pick_wake_ack())
    if not interrupted:
        _drain_backlog(proc)  # discard mic input captured while the ack played

    first_turn = True
    claude_mode = False
    while True:
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

        if _ACTIVATE_CLAUDE_RE.search(text):
            claude_mode = True
            print("(claude mode: ON — every turn now forced to the reasoning tier)")
            interrupted = _speak_interruptible(proc, "Claude's active, sir. Every turn now goes there until you switch back.")
            if not interrupted:
                _drain_backlog(proc)
            continue  # control command, not a question — don't send it anywhere
        if _DEACTIVATE_CLAUDE_RE.search(text):
            claude_mode = False
            print("(claude mode: OFF — back to normal routing)")
            interrupted = _speak_interruptible(proc, "Back to normal, sir.")
            if not interrupted:
                _drain_backlog(proc)
            continue

        if _FILE_FIX_RE.search(text):
            print("(local intent: checking inbox for a file to fix)")
            write_state("thinking", 0.4)
            answer = fix_file(latest_inbox_file(), session_id=conversation_id)
        else:
            answer = ask_jarvis(text, session_id=conversation_id, force_reasoning=claude_mode)
        print(f"Jarvis: {answer}")
        interrupted = _speak_interruptible(proc, answer)
        if not interrupted:
            _drain_backlog(proc)
        # loop straight back into listening — no re-trigger needed for a
        # follow-up, this is what makes it feel like an actual conversation

    write_state("idle", 0.0)


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
    """Live RMS meter — run this, clap a few times, and read off real
    numbers instead of guessing at a threshold."""
    print("=== JARVIS clap calibration ===")
    print(f"mic: {describe_selected_device(AUDIO_DEVICE)} (override with JARVIS_AUDIO_DEVICE)")
    print("Watch the numbers. Clap normally a few times and see what they hit.")
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
            hit = level > threshold
            if hit or (now - last_print) > 0.15:
                last_print = now
                bar = "#" * min(60, int(level / 50))
                marker = "  <-- WOULD TRIGGER" if hit else ""
                print(f"level={level:7.1f}  floor={noise_floor:7.1f}  threshold={threshold:7.1f}  {bar}{marker}")
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
    write_state("idle", 0.0)

    # One Claude session for the entire life of this process, not one per
    # wake trigger — a long pause between claps shouldn't mean Jarvis has
    # forgotten what you talked about an hour ago. Only resets if you
    # actually restart this script; a full context window is handled by
    # Claude Code's own autocompact within the session, not by us minting
    # a new one.
    conversation_id = str(uuid.uuid4())
    print(f"session: {conversation_id} (persists until this process restarts)\n")

    proc = _open_mic_stream()
    avg = _sanity_check(proc, seconds=1.0)
    noise_floor = max(avg, 50.0)
    prev_level = 0.0
    last_clap_time = 0.0

    waker = SpacebarWaker()
    waker.start()
    _install_signal_cleanup(proc, waker)

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
            clap_hit = is_spike and was_quiet and (now - last_clap_time) > CLAP_COOLDOWN_S
            space_hit = waker.event.is_set()

            if clap_hit or space_hit:
                waker.event.clear()
                last_clap_time = now
                _handle_wake(proc, conversation_id, trigger="clap" if clap_hit else "spacebar")
                prev_level = 0.0
    except KeyboardInterrupt:
        pass
    finally:
        write_state("idle", 0.0)
        waker.stop()
        _kill_stream(proc)
        print("\nShutting down.")


if __name__ == "__main__":
    if "--calibrate" in sys.argv:
        _calibrate()
    else:
        main()
