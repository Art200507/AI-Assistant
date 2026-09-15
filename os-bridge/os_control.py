"""OS control bridge for Jarvis — open/close apps, volume, brightness, cache purge.

SAFE_MODE is on. Every action logs exactly what it WOULD run, then returns
without running it. Flip SAFE_MODE to False yourself once you've read
actions.log and are comfortable with what each function actually does.

Deliberately NOT implemented here, even behind the flag: file deletion,
app uninstalls. Those aren't in scope for this script at all.
"""
import subprocess
import time
from pathlib import Path

SAFE_MODE = True  # <-- leave this True. Flip it yourself when ready.

HERE = Path(__file__).parent
LOG_FILE = HERE / "actions.log"


def _log(action: str, detail: str, executed: bool):
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    tag = "EXECUTED" if executed else "SAFE_MODE (not run)"
    line = f"[{ts}] {tag}: {action} — {detail}"
    print(line)
    with open(LOG_FILE, "a") as f:
        f.write(line + "\n")


def _guarded_run(action: str, detail: str, cmd: list[str]):
    """Log the intended command; only actually run it if SAFE_MODE is False."""
    _log(action, detail, executed=not SAFE_MODE)
    if SAFE_MODE:
        return None
    return subprocess.run(cmd, capture_output=True, text=True, timeout=15)


def open_app(name: str):
    _guarded_run("open_app", f'open -a "{name}"', ["open", "-a", name])


def close_app(name: str):
    # Gentle quit via AppleScript rather than pkill/kill — lets the app
    # close normally (save prompts etc.) instead of force-terminating it.
    script = f'tell application "{name}" to quit'
    _guarded_run("close_app", f"osascript -e '{script}'", ["osascript", "-e", script])


def set_volume(level: int):
    level = max(0, min(100, level))
    script = f"set volume output volume {level}"
    _guarded_run("set_volume", f"osascript -e '{script}'", ["osascript", "-e", script])


def brightness_up(steps: int = 1):
    # macOS has no simple CLI for an absolute brightness %; this simulates
    # the physical brightness-up key (key code 144) `steps` times, which is
    # the same relative adjustment you'd get pressing the key on a keyboard.
    script = "tell application \"System Events\" to key code 144"
    for _ in range(steps):
        _guarded_run("brightness_up", f"osascript -e '{script}'", ["osascript", "-e", script])


def brightness_down(steps: int = 1):
    script = "tell application \"System Events\" to key code 145"
    for _ in range(steps):
        _guarded_run("brightness_down", f"osascript -e '{script}'", ["osascript", "-e", script])


def clear_cache():
    # Deliberately NOT file deletion — `purge` reclaims inactive memory
    # pages, it does not touch anything on disk. Requires sudo when it
    # actually runs (SAFE_MODE blocks that prompt from ever appearing tonight).
    _guarded_run("clear_cache", "sudo purge  (reclaims inactive RAM, touches no files)", ["sudo", "purge"])


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage: os_control.py <open|close|volume|bright-up|bright-down|clear-cache> [arg]")
        print(f"SAFE_MODE is currently: {SAFE_MODE}")
        sys.exit(0)

    cmd, *rest = sys.argv[1:]
    if cmd == "open" and rest:
        open_app(rest[0])
    elif cmd == "close" and rest:
        close_app(rest[0])
    elif cmd == "volume" and rest:
        set_volume(int(rest[0]))
    elif cmd == "bright-up":
        brightness_up(int(rest[0]) if rest else 1)
    elif cmd == "bright-down":
        brightness_down(int(rest[0]) if rest else 1)
    elif cmd == "clear-cache":
        clear_cache()
    else:
        print("Unknown command or missing argument.")
