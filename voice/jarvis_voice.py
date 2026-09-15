"""JARVIS voice round-trip: mic -> whisper.cpp -> jarvis_respond (server) -> piper -> speakers.

Push-to-talk: press Enter to start recording, press Enter again to stop.
Everything here runs locally and free except the actual query, which is
forwarded to the existing cloud brain over SSH (see jarvis_client.py).

Also writes ~/.jarvis-dashboard/voice_state.json on every state change so
the Tauri dashboard's 3D avatar can move in sync (idle/listening/thinking/
speaking + a real amplitude envelope extracted from the generated speech).
"""
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
import wave
from concurrent.futures import ThreadPoolExecutor
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

# TTS engine: "kokoro" (default) — local open-weights model, noticeably more
# natural than both Piper and macOS's Daniel. Voice bm_daniel (British male)
# chosen after comparing all 4 British male voices live. Tradeoff: ~2.8s to
# synthesize a 4s sentence (vs macOS's near-instant) — the sentence-pipelined
# playback in speak() hides some of this for multi-sentence answers, but the
# first sentence's synthesis time is still real, unavoidable latency.
# "macos" reverts to the free instant option, "piper" to the original.
TTS_ENGINE = os.environ.get("JARVIS_TTS_ENGINE", "kokoro")

SAY_VOICE = os.environ.get("JARVIS_SAY_VOICE", "Daniel")
SAY_SAMPLE_RATE = 22050
SAY_RATE = os.environ.get("JARVIS_SAY_RATE", "178")  # words/min; 195 tested too fast, macOS default too sluggish

KOKORO_MODEL = os.environ.get("KOKORO_MODEL", str(HERE / "models" / "kokoro" / "kokoro-v1.0.onnx"))
KOKORO_VOICES = os.environ.get("KOKORO_VOICES", str(HERE / "models" / "kokoro" / "voices-v1.0.bin"))
KOKORO_VOICE = os.environ.get("JARVIS_KOKORO_VOICE", "bm_daniel")
_kokoro = None


def _get_kokoro():
    global _kokoro
    if _kokoro is None:
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            from kokoro_onnx import Kokoro
        _kokoro = Kokoro(KOKORO_MODEL, KOKORO_VOICES)
    return _kokoro

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
    if TTS_ENGINE == "kokoro":
        import soundfile as sf
        kokoro = _get_kokoro()
        samples, sr = kokoro.create(text, voice=KOKORO_VOICE, speed=1.0, lang="en-us")
        sf.write(out_wav, samples, sr)
    elif TTS_ENGINE == "piper":
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
                "say", "-v", SAY_VOICE, "-r", SAY_RATE, "-o", out_wav,
                "--file-format=WAVE", f"--data-format=LEI16@{SAY_SAMPLE_RATE}",
                text,
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )


_SENTENCE_SPLIT_RE = re.compile(r"(?<=[.!?])\s+")


def _split_sentences(text: str):
    parts = [p.strip() for p in _SENTENCE_SPLIT_RE.split(text) if p.strip()]
    return parts or [text]


def _synthesize_to_temp(text: str) -> str:
    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
        path = tmp.name
    _synthesize(text, path)
    return path


def _play_wav_with_avatar_sync(wav_path: str):
    envelope = _amplitude_envelope(wav_path)
    player = subprocess.Popen(["afplay", wav_path])
    for amp in envelope:
        if player.poll() is not None:
            break
        write_state("speaking", amp)
        time.sleep(CHUNK_MS / 1000)
    player.wait(timeout=30)


def speak(text: str):
    """Synthesizes and plays sentence-by-sentence, pipelined: the next
    sentence is synthesized in the background while the current one plays,
    instead of waiting for the entire answer to be voiced before any sound
    plays. For a long answer this is the difference between silence for the
    whole response and starting to talk almost immediately."""
    if not text:
        write_state("idle", 0.0)
        return

    sentences = _split_sentences(text)
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        future = executor.submit(_synthesize_to_temp, sentences[0])
        for i in range(len(sentences)):
            wav_path = future.result()
            next_future = (
                executor.submit(_synthesize_to_temp, sentences[i + 1])
                if i + 1 < len(sentences)
                else None
            )
            try:
                _play_wav_with_avatar_sync(wav_path)
            finally:
                Path(wav_path).unlink(missing_ok=True)
            future = next_future
    finally:
        write_state("idle", 0.0)
        executor.shutdown(wait=False)


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
