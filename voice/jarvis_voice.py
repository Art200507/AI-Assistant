"""JARVIS voice round-trip: mic -> whisper.cpp -> jarvis_respond (server) -> piper -> speakers.

Push-to-talk: press Enter to start recording, press Enter again to stop.
Everything here runs locally and free except the actual query, which is
forwarded to the existing cloud brain over SSH (see jarvis_client.py).

Also writes ~/.jarvis-dashboard/voice_state.json on every state change so
the Tauri dashboard's 3D avatar can move in sync (idle/listening/thinking/
speaking + a real amplitude envelope extracted from the generated speech).
"""
import os
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

from jarvis_client import ask_jarvis
from mic_device import resolve_audio_device, describe_selected_device
from voice_state import write_state

HERE = Path(__file__).parent
WHISPER_BIN = os.environ.get("WHISPER_BIN", "/opt/homebrew/bin/whisper-cli")
WHISPER_MODEL = os.environ.get(
    "WHISPER_MODEL", str(HERE / "models" / "ggml-base.en.bin")
)
PIPER_BIN = os.environ.get("PIPER_BIN", str(HERE.parent / ".venv" / "bin" / "piper"))
PIPER_MODEL = os.environ.get(
    "PIPER_MODEL", str(HERE / "models" / "piper" / "en_GB-alan-medium.onnx")
)

# TTS engine: "macos" (default) uses the system `say` command with Daniel
# (en_GB, male) — the only Daniel actually registered on this Mac; verified
# via `say -v '?'`. Note: `say -v` on an unrecognized name does NOT error,
# it silently falls back to the system default voice — which is how a
# fabricated "Daniel (Enhanced)" ended up playing a female US voice earlier.
# Always check a voice name is real with `say -v '?' | grep -i <name>`
# before using it. If you download the genuine enhanced/premium Daniel via
# System Settings -> Accessibility -> Spoken Content -> System Voice -> (i)
# -> find Daniel -> Download, it will show up as its own exact line in
# `say -v '?'` — copy that exact string into JARVIS_SAY_VOICE once it does.
# Set JARVIS_TTS_ENGINE=piper to use Piper instead.
TTS_ENGINE = os.environ.get("JARVIS_TTS_ENGINE", "macos")
SAY_VOICE = os.environ.get("JARVIS_SAY_VOICE", "Daniel")
SAY_SAMPLE_RATE = 22050

AUDIO_DEVICE = resolve_audio_device()  # auto-picks the real mic by name; JARVIS_AUDIO_DEVICE overrides

CHUNK_MS = 60


def _validate_say_voice():
    """`say -v <name>` on an unrecognized name does NOT error — it silently
    plays the system default voice instead. That's exactly how a fabricated
    "Daniel (Enhanced)" turned into an unexpected female US voice. Check the
    configured name is real at startup so a typo/wrong guess is loud, not
    silent, next time."""
    if TTS_ENGINE != "macos":
        return
    try:
        result = subprocess.run(["say", "-v", "?"], capture_output=True, text=True, timeout=10)
        names = {line.split("  ")[0].strip() for line in result.stdout.splitlines() if line.strip()}
        if SAY_VOICE not in names:
            print(
                f"⚠️  JARVIS_SAY_VOICE={SAY_VOICE!r} isn't a real installed voice — "
                f"`say` will silently use the system default instead (probably not "
                f"who you think). Run `say -v '?'` to see actual installed voices."
            )
    except Exception:
        pass  # non-fatal — this is a best-effort sanity check, not required to run


_validate_say_voice()  # runs on import so both this script and always_on.py get the check


def record_until_enter(out_wav: str):
    print("Recording… press Enter to stop.")
    write_state("listening", 0.4)
    proc = subprocess.Popen(
        [
            "ffmpeg", "-y",
            "-f", "avfoundation",
            "-i", f":{AUDIO_DEVICE}",
            "-ar", "16000",
            "-ac", "1",
            out_wav,
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    input()  # wait for Enter
    try:
        proc.communicate(input=b"q", timeout=5)
    except subprocess.TimeoutExpired:
        proc.terminate()
        proc.wait(timeout=5)


def transcribe(wav_path: str) -> str:
    write_state("thinking", 0.2)
    result = subprocess.run(
        [WHISPER_BIN, "-m", WHISPER_MODEL, "-f", wav_path, "-nt", "-np"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    lines = [
        l.strip()
        for l in result.stdout.splitlines()
        if l.strip() and not l.startswith(("whisper_", "ggml_", "main:", "system_info", "load_backend", "read_audio_data"))
    ]
    return " ".join(lines).strip()


def _amplitude_envelope(wav_path: str, chunk_ms: int = CHUNK_MS):
    """RMS amplitude per chunk, normalized 0..1. Pure stdlib (no audioop)."""
    with wave.open(wav_path, "rb") as wf:
        channels = wf.getnchannels()
        framerate = wf.getframerate()
        n_frames = wf.getnframes()
        raw = wf.readframes(n_frames)

    samples = struct.unpack(f"<{len(raw) // 2}h", raw)
    chunk_size = max(1, int(framerate * chunk_ms / 1000) * channels)

    envelope = []
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i : i + chunk_size]
        if not chunk:
            continue
        rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5
        envelope.append(min(1.0, rms / 9000))
    return envelope


def _synthesize(text: str, out_wav: str):
    if TTS_ENGINE == "piper":
        subprocess.run(
            [PIPER_BIN, "-m", PIPER_MODEL, "-f", out_wav],
            input=text,
            text=True,
            capture_output=True,
            timeout=60,
        )
    else:
        subprocess.run(
            [
                "say", "-v", SAY_VOICE, "-o", out_wav,
                "--file-format=WAVE", f"--data-format=LEI16@{SAY_SAMPLE_RATE}",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )


def speak(text: str):
    if not text:
        write_state("idle", 0.0)
        return
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        out_wav = tmp.name
    try:
        _synthesize(text, out_wav)
        envelope = _amplitude_envelope(out_wav)
        player = subprocess.Popen(["afplay", out_wav])
        for amp in envelope:
            if player.poll() is not None:
                break
            write_state("speaking", amp)
            time.sleep(CHUNK_MS / 1000)
        player.wait(timeout=30)
    finally:
        write_state("idle", 0.0)
        Path(out_wav).unlink(missing_ok=True)


def run_once():
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        wav_path = tmp.name
    try:
        record_until_enter(wav_path)
        print("Transcribing…")
        text = transcribe(wav_path)
        if not text:
            print("(heard nothing)")
            speak("I'm afraid I didn't quite catch that, sir.")
            return
        print(f"You said: {text}")
        print("Asking Jarvis…")
        answer = ask_jarvis(text)
        print(f"Jarvis: {answer}")
        speak(answer)
    finally:
        write_state("idle", 0.0)
        Path(wav_path).unlink(missing_ok=True)


def main():
    print("=== JARVIS voice interface ===")
    print(f"whisper: {WHISPER_BIN} ({WHISPER_MODEL})")
    print(f"piper:   {PIPER_BIN} ({PIPER_MODEL})")
    print(f"mic: {describe_selected_device(AUDIO_DEVICE)} (override with JARVIS_AUDIO_DEVICE)")
    print("Press Enter to start each turn, Ctrl+C to quit.\n")
    write_state("idle", 0.0)
    while True:
        try:
            input("--- Press Enter to talk ---")
            run_once()
            print()
        except KeyboardInterrupt:
            write_state("idle", 0.0)
            print("\nShutting down.")
            sys.exit(0)


if __name__ == "__main__":
    main()
