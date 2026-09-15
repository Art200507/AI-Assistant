"""Resolves which avfoundation audio device to record from.

Device indices are NOT stable — they shift depending on what's currently
running (e.g. opening Teams adds a "Microsoft Teams Audio" virtual device
that can bump your real mic from index 1 to index 0 or vice versa). Relying
on a hardcoded index is exactly how a working setup silently starts
recording from a virtual/silent device with zero warning — ffmpeg reports
success either way, it just returns silence.

This picks the real physical microphone by name every time, unless
JARVIS_AUDIO_DEVICE is set explicitly (which always wins).
"""
import os
import re
import subprocess

_BAD_WORDS = ("teams", "virtual", "aggregate", "blackhole", "loopback", "soundflower")


def list_avfoundation_audio_devices():
    """Returns [(index, name), ...] for audio input devices, parsed from
    ffmpeg's own device listing (it prints this to stderr)."""
    result = subprocess.run(
        ["ffmpeg", "-f", "avfoundation", "-list_devices", "true", "-i", ""],
        capture_output=True, text=True, timeout=10,
    )
    text = result.stderr
    devices = []
    in_audio_section = False
    for line in text.splitlines():
        if "AVFoundation audio devices" in line:
            in_audio_section = True
            continue
        if in_audio_section:
            m = re.search(r"\[(\d+)\]\s+(.*)$", line)
            if not m:
                break  # first non-matching line ends the audio device block
            devices.append((int(m.group(1)), m.group(2).strip()))
    return devices


def resolve_audio_device() -> str:
    override = os.environ.get("JARVIS_AUDIO_DEVICE")
    if override is not None:
        return override

    devices = list_avfoundation_audio_devices()
    if not devices:
        return "1"  # last-resort guess if listing itself failed

    def is_bad(name: str) -> bool:
        low = name.lower()
        return any(w in low for w in _BAD_WORDS)

    # Prefer something that says "Microphone" and isn't a virtual device.
    for idx, name in devices:
        if "microphone" in name.lower() and not is_bad(name):
            return str(idx)
    # Otherwise, first non-virtual device.
    for idx, name in devices:
        if not is_bad(name):
            return str(idx)
    # Everything looked virtual — just take the first one and let the
    # mic sanity check in always_on.py / jarvis_voice.py flag it as silent.
    return str(devices[0][0])


def describe_selected_device(selected_index: str) -> str:
    devices = dict(list_avfoundation_audio_devices())
    try:
        idx = int(selected_index)
    except ValueError:
        return f"index {selected_index} (unparsed)"
    name = devices.get(idx, "unknown device")
    return f"[{idx}] {name}"


if __name__ == "__main__":
    devices = list_avfoundation_audio_devices()
    print("Available audio input devices:")
    for idx, name in devices:
        print(f"  [{idx}] {name}")
    chosen = resolve_audio_device()
    print(f"\nWould select: {describe_selected_device(chosen)}")
    if os.environ.get("JARVIS_AUDIO_DEVICE"):
        print("(forced via JARVIS_AUDIO_DEVICE env var)")
