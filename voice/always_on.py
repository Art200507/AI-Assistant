"""JARVIS always-on mode: clap OR a long spacebar hold wakes it up.

Runs one persistent ffmpeg process streaming raw mic audio to this process.
While idle, every ~20ms chunk is checked for a clap (a sharp amplitude
transient well above the recent ambient noise floor). In parallel, a global
keyboard listener watches for the spacebar being held down for at least
LONG_PRESS_SECONDS. Either one wakes Jarvis the same way: it speaks a short
acknowledgment (a rotating pool of dry, polite, JARVIS-flavored lines —
see _WAKE_ACK_POOL), then listens and buffers real audio until ~1.4s of
silence (a volume threshold — webrtcvad was tried and measured to perform
badly on this mic, see the note above SILENCE_RMS), then runs it through
the same transcribe -> ask_jarvis -> speak pipeline as the push-to-talk
script (jarvis_voice.py).

Clap detection is amplitude/transient-based, not a trained classifier — it
can be fooled by other sharp sounds (a dropped object, a door) and may need
JARVIS_CLAP_RATIO / JARVIS_CLAP_ABS_MIN tuned to your room (run with
--calibrate). The spacebar listener needs Accessibility permission the
first time (System Settings -> Privacy & Security -> Accessibility -> allow
your terminal app) since it's a global hotkey, not tied to any window.
"""
import os
import random
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
# How long a stretch of silence after you've started talking before we
# decide you're done. Was 800ms (tuned for snappiness) — too aggressive,
# was cutting off mid-thought pauses. 1400ms gives real room to pause and
# collect your thoughts without ending the turn.
SILENCE_HANG_MS = float(os.environ.get("JARVIS_SILENCE_HANG_MS", "1400"))
MAX_UTTERANCE_S = 20

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


def _record_utterance(proc, max_wait_s: float):
    """Wait up to max_wait_s for you to actually start talking; once you do,
    record until SILENCE_HANG_MS of continuous VAD-silence (or the
    MAX_UTTERANCE_S hard cap — a safety net, not the normal stop condition).
    Returns the raw PCM bytes, or None if nobody spoke within max_wait_s —
    that's the signal that the conversation is over, not a failed turn."""
    buffer = bytearray()
    preroll = deque(maxlen=_PREROLL_CHUNKS)
    silence_ms = 0
    heard_voice = False
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
        else:
            silence_ms += CHUNK_MS
        if silence_ms > SILENCE_HANG_MS:
            break
        if (time.time() - start) > MAX_UTTERANCE_S:
            break

    return bytes(buffer)


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


def _handle_wake(proc, trigger: str = "clap"):
    label = "\U0001f44f Clap" if trigger == "clap" else "⌨️  Spacebar held"
    print(f"\n{label} — waking up…")

    # One session_id for this whole conversation (reset on every new wake
    # trigger) — this is what gives Claude actual memory across follow-up
    # turns via `claude --resume`, instead of answering each turn in total
    # isolation. See jarvis_respond.escalate_to_claude on the server.
    conversation_id = str(uuid.uuid4())

    # Acknowledge immediately so you know it's your turn to talk, before we
    # start actually recording the real question. Only said once per
    # conversation, not on every follow-up turn.
    write_state("speaking", 0.6)
    interrupted = _speak_interruptible(proc, _pick_wake_ack())
    if not interrupted:
        _drain_backlog(proc)  # discard mic input captured while the ack played

    first_turn = True
    while True:
        if interrupted:
            # You talked over Jarvis — it already stopped, now catch the
            # rest of what you're saying rather than making you re-trigger.
            # The first ~80ms was consumed by the barge-in detector itself
            # (not saved), so this picks up an instant after that.
            print("(interrupted — listening)")
            write_state("listening", 0.5)
            pcm = _record_utterance(proc, BARGE_IN_RECOVERY_WAIT_S)
        else:
            print("Listening…" if first_turn else "Listening for a follow-up…")
            write_state("listening", 0.5)
            wait_s = MAX_UTTERANCE_S if first_turn else FOLLOWUP_WAIT_S
            pcm = _record_utterance(proc, wait_s)
        first_turn = False

        if pcm is None:
            print("--- no follow-up, back to standby ---")
            break

        _write_wav(UTTERANCE_WAV, pcm)
        write_state("thinking", 0.2)
        text = transcribe(UTTERANCE_WAV)
        Path(UTTERANCE_WAV).unlink(missing_ok=True)

        if not text:
            print("(heard something, couldn't make out words)")
            interrupted = _speak_interruptible(proc, "Sorry, I didn't quite catch that, sir.")
            if not interrupted:
                _drain_backlog(proc)
            continue  # stay in the conversation, give it another go

        print(f"You said: {text}")
        answer = ask_jarvis(text, session_id=conversation_id)
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
                _handle_wake(proc, trigger="clap" if clap_hit else "spacebar")
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
