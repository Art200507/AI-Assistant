"""JARVIS always-on mode: clap OR a long spacebar hold wakes it up.

Runs one persistent ffmpeg process streaming raw mic audio to this process.
While idle, every ~20ms chunk is checked for a clap (a sharp amplitude
transient well above the recent ambient noise floor). In parallel, a global
keyboard listener watches for the spacebar being held down for at least
LONG_PRESS_SECONDS. Either one wakes Jarvis the same way: it speaks a short
acknowledgment ("What's up?"), then listens and buffers real audio until you
stop talking (silence-based endpointing), then runs it through the same
transcribe -> ask_jarvis -> speak pipeline as the push-to-talk script
(jarvis_voice.py).

Clap detection is amplitude/transient-based, not a trained classifier — it
can be fooled by other sharp sounds (a dropped object, a door) and may need
JARVIS_CLAP_RATIO / JARVIS_CLAP_ABS_MIN tuned to your room (run with
--calibrate). The spacebar listener needs Accessibility permission the
first time (System Settings -> Privacy & Security -> Accessibility -> allow
your terminal app) since it's a global hotkey, not tied to any window.
"""
import os
import signal
import struct
import subprocess
import sys
import threading
import time
import wave
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
# for your real question — gives you an audible cue that it's your turn to talk.
WAKE_ACK_PHRASE = os.environ.get("JARVIS_WAKE_ACK", "Yes? What's up?")

# Endpointing for the actual query after a wake trigger.
SILENCE_RMS = float(os.environ.get("JARVIS_SILENCE_RMS", "400"))
SILENCE_HANG_MS = 1000
MAX_UTTERANCE_S = 12

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


def _handle_wake(proc, trigger: str = "clap"):
    label = "\U0001f44f Clap" if trigger == "clap" else "⌨️  Spacebar held"
    print(f"\n{label} — waking up…")

    # Acknowledge immediately so you know it's your turn to talk, before we
    # start actually recording the real question.
    write_state("speaking", 0.6)
    speak(WAKE_ACK_PHRASE)
    _drain_backlog(proc)  # discard mic input captured while the ack played

    print("Listening…")
    write_state("listening", 0.5)
    buffer = bytearray()
    silence_ms = 0
    heard_voice = False
    start = time.time()

    while True:
        chunk = _read_exact(proc.stdout, CHUNK_BYTES)
        if not chunk:
            break
        buffer.extend(chunk)
        level = _rms(chunk)
        if level > SILENCE_RMS:
            silence_ms = 0
            heard_voice = True
        else:
            silence_ms += CHUNK_MS
        if heard_voice and silence_ms > SILENCE_HANG_MS:
            break
        if (time.time() - start) > MAX_UTTERANCE_S:
            break

    _write_wav(UTTERANCE_WAV, bytes(buffer))
    write_state("thinking", 0.2)
    text = transcribe(UTTERANCE_WAV)
    Path(UTTERANCE_WAV).unlink(missing_ok=True)

    if not text:
        print("(heard nothing usable)")
        speak("I didn't catch that.")
    else:
        print(f"You said: {text}")
        answer = ask_jarvis(text)
        print(f"Jarvis: {answer}")
        speak(answer)

    write_state("idle", 0.0)
    _drain_backlog(proc)
    print("--- listening for the next clap ---")


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
