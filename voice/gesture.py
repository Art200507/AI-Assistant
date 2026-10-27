"""Camera-based gesture detection — tap-the-laptop (see the tap-pattern
detector in always_on.py) opens a SHORT, bounded camera window watching
for one gesture: an open palm that then pinches toward the center. The
camera is released the instant the window ends, found or not — this is
explicitly NOT continuous monitoring, per the user's explicit
requirement ("camera only at that instant... close once the action is
done"), not an always-on gesture watcher.

Detected gesture triggers the exact same wake flow as a clap/spacebar
hold (see always_on.py) — matches the user's own framing ("Jarvis will
come and ask for instructions"), reusing the existing wake mechanism
rather than inventing a separate one.
"""
import time
from pathlib import Path

import cv2
import mediapipe as mp
from mediapipe.tasks import python as mp_python
from mediapipe.tasks.python import vision as mp_vision

HERE = Path(__file__).parent
MODEL_PATH = HERE / "models" / "mediapipe" / "hand_landmarker.task"

# Fingertip landmark indices (thumb, index, middle, ring, pinky tips) and
# the wrist as a palm-center reference, from MediaPipe's 21-point hand
# model (ai.google.dev/edge/mediapipe/solutions/vision/hand_landmarker).
_FINGERTIPS = [4, 8, 12, 16, 20]
_WRIST = 0

# Relative thresholds, not absolute pixel distances — adaptive to each
# individual gesture attempt (hand size, distance from camera, framing
# all vary), calibrated against whatever spread THIS hand showed when
# open, not a fixed number guessed in advance.
OPEN_THRESHOLD_RATIO = 0.85   # "currently open" = within 85% of the best spread seen
PINCH_RATIO = 0.55            # "pinched" = spread dropped below 55% of the open peak
MIN_OPEN_FRAMES = 3           # consecutive open-ish frames needed before a pinch counts,
                               # so a hand entering frame already closed can't false-trigger


def _spread(landmarks) -> float:
    wrist = landmarks[_WRIST]
    dists = [
        ((landmarks[i].x - wrist.x) ** 2 + (landmarks[i].y - wrist.y) ** 2) ** 0.5
        for i in _FINGERTIPS
    ]
    return sum(dists) / len(dists)


def watch_for_gesture(duration_s: float = 8.0) -> bool:
    """Opens the camera, watches live frames for up to duration_s for an
    open-palm-then-pinch. ALWAYS releases the camera before returning —
    bounded window, not continuous monitoring, whether or not a gesture
    was actually seen."""
    base_options = mp_python.BaseOptions(model_asset_path=str(MODEL_PATH))
    options = mp_vision.HandLandmarkerOptions(base_options=base_options, num_hands=1)
    detector = mp_vision.HandLandmarker.create_from_options(options)
    cap = cv2.VideoCapture(0)

    detected = False
    try:
        if not cap.isOpened():
            return False

        max_spread = 0.0
        open_frames = 0
        armed = False  # seen a real sustained open-palm phase yet?
        deadline = time.time() + duration_s

        while time.time() < deadline:
            ok, frame = cap.read()
            if not ok:
                continue

            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            result = detector.detect(mp_image)

            if not result.hand_landmarks:
                open_frames = 0
                continue

            spread = _spread(result.hand_landmarks[0])
            max_spread = max(max_spread, spread)
            if max_spread <= 0:
                continue

            if spread >= max_spread * OPEN_THRESHOLD_RATIO:
                open_frames += 1
                if open_frames >= MIN_OPEN_FRAMES:
                    armed = True
            else:
                open_frames = 0

            if armed and spread < max_spread * PINCH_RATIO:
                detected = True
                break
    finally:
        cap.release()
        detector.close()

    return detected


if __name__ == "__main__":
    import sys
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 8.0
    print(f"Watching for {duration}s — raise your palm, hold it open, then pinch your fingers toward the center...")
    found = watch_for_gesture(duration)
    print("GESTURE DETECTED" if found else "No gesture detected (timed out).")
