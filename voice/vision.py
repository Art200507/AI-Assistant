"""Camera vision for Jarvis: two genuinely different capabilities, kept
deliberately separate rather than both routed through Claude.

1. Scene/object description ("what do you see", "what am I holding") —
   Claude's own vision (via its Read tool on a captured image file, same
   as local_claude.py's file access) is genuinely good at this: flexible,
   natural-language understanding of arbitrary scenes.

2. Face IDENTITY matching ("do you recognize me", "who is this") — done
   entirely by the face_recognition library (dlib-based, 128-d face
   embeddings + distance comparison), NOT by asking an LLM to look at a
   photo and guess. An LLM has no real biometric capability and would be
   guessing/hallucinating a "yes that's you" — the whole reason the user
   chose the rigorous biometric option over the cheap "just trust
   context" option was to get an actual reliable answer, so the identity
   decision has to come from the deterministic algorithm, with Jarvis
   just reporting that real result, not re-deciding it.

Enrollment: one reference photo, once, stored as a face encoding (128
floats) locally at ~/.jarvis_face_<name>.json — same convention as the
API key files (owner-only permissions, never in git, lives outside the
repo in the home directory).
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import face_recognition
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from local_claude import ask_local_claude

CAMERA_DEVICE = os.environ.get("JARVIS_CAMERA_DEVICE", "0")  # "[0] MacBook Air Camera", confirmed via avfoundation -list_devices
CAPTURE_PATH = "/tmp/jarvis_camera_capture.jpg"

FACE_DIR = Path.home()
DEFAULT_FACE_NAME = "owner"

# dlib's own recommended default (compare_faces' default tolerance) —
# lower = stricter match. Not retuned/calibrated against this specific
# camera; if false-accepts or false-rejects show up in real use, this is
# the first real number to adjust, backed by actual observed distances
# rather than guessed.
MATCH_TOLERANCE = 0.6


def _face_encoding_path(name: str) -> Path:
    return FACE_DIR / f".jarvis_face_{name}.json"


def capture_photo(path: str = CAPTURE_PATH, warmup_frames: int = 1) -> str:
    """Grabs one still frame from the webcam. warmup_frames >1 lets the
    camera's auto-exposure/focus settle before the frame that's kept —
    the very first frame after opening the device is often darker/blurrier
    than a moment later; not yet tuned against real observed quality
    difference, just the standard reason this matters for webcams."""
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-f", "avfoundation", "-video_size", "1280x720", "-framerate", "30",
            "-i", f"{CAMERA_DEVICE}",
            "-frames:v", "1", "-update", "1",
            path,
        ],
        capture_output=True, text=True, timeout=15,
    )
    if not Path(path).exists():
        raise RuntimeError("Camera capture failed — check camera permission (System Settings -> Privacy & Security -> Camera) for your terminal app.")
    return path


def describe_scene(photo_path: str, question: str = None) -> str:
    """General vision via Claude's own Read tool on the image file — NOT
    for identity matching, see module docstring. question lets a specific
    voice query ("what am I holding") steer what Claude focuses on,
    instead of always giving a generic full-scene description."""
    q = question or "What do you see in this image? Describe it naturally, as if telling the user what's in front of the camera."
    instruction = f"Read the image file at {photo_path} and answer this, in 2-3 spoken sentences: {q}"
    return ask_local_claude(instruction)


def enroll_face(name: str = DEFAULT_FACE_NAME) -> str:
    """Captures a photo right now and stores its face encoding as the
    reference for future recognize_face() calls. Overwrites any existing
    enrollment for this name — re-enrolling is the correct way to update
    it, not a separate command."""
    photo_path = capture_photo()
    image = face_recognition.load_image_file(photo_path)
    locations = face_recognition.face_locations(image)
    if not locations:
        return "I didn't detect a face in that shot, sir — make sure you're facing the camera and try again."
    if len(locations) > 1:
        return f"I detected {len(locations)} faces, sir — for enrollment I need just one person in frame."
    encoding = face_recognition.face_encodings(image, known_face_locations=locations)[0]

    path = _face_encoding_path(name)
    path.write_text(json.dumps(encoding.tolist()))
    os.chmod(path, 0o600)
    return f"Face enrolled, sir. I'll be able to recognize you as '{name}' going forward."


def recognize_face(name: str = DEFAULT_FACE_NAME) -> tuple:
    """Captures a photo right now and compares any face(s) in it against
    the enrolled reference. The identity decision itself is entirely
    face_recognition's distance comparison — no LLM involved in deciding
    match/no-match, only in phrasing the spoken result elsewhere if
    needed.

    Returns (status, message): status is one of "match", "no_match",
    "no_face", "not_enrolled" — a real status code, not something a
    caller has to guess by parsing the spoken prose. Added specifically
    because the startup owner-verification gate (always_on.py) needs to
    make a clean allow/deny decision, and matching against exact wording
    of a human-readable sentence is exactly the kind of fragile pattern
    this whole project has been moving away from."""
    enc_path = _face_encoding_path(name)
    if not enc_path.exists():
        return "not_enrolled", "I don't have an enrolled face on record yet, sir — say 'enroll my face' first, facing the camera."

    known_encoding = np.array(json.loads(enc_path.read_text()))

    photo_path = capture_photo()
    image = face_recognition.load_image_file(photo_path)
    locations = face_recognition.face_locations(image)
    if not locations:
        return "no_face", "I don't see a face in frame right now, sir."

    encodings = face_recognition.face_encodings(image, known_face_locations=locations)
    distances = face_recognition.face_distance(encodings, known_encoding)
    best_idx = int(np.argmin(distances))
    best_distance = float(distances[best_idx])
    is_match = best_distance <= MATCH_TOLERANCE

    # Rough, unclaibrated confidence readout for the spoken response —
    # not a real probability, just distance-to-percentage for something
    # sayable. Distance 0.0 -> ~100%, distance at the tolerance boundary
    # -> ~0%, clamped.
    confidence_pct = max(0, min(100, round((1 - best_distance / MATCH_TOLERANCE) * 100)))

    if len(locations) > 1 and is_match:
        return "match", f"I recognize you in frame, sir — {confidence_pct}% confidence — along with {len(locations) - 1} other face(s) I don't recognize."
    if is_match:
        return "match", f"That's you, sir — {confidence_pct}% confidence match."
    if len(locations) == 1:
        return "no_match", "I don't recognize this person, sir — doesn't match your enrolled face."
    return "no_match", f"I see {len(locations)} face(s) in frame, sir, but none match your enrolled face."


if __name__ == "__main__":
    import sys as _sys
    cmd = _sys.argv[1] if len(_sys.argv) > 1 else "recognize"
    if cmd == "enroll":
        print(enroll_face())
    elif cmd == "recognize":
        status, message = recognize_face()
        print(f"[{status}] {message}")
    else:
        print(describe_scene(capture_photo(), " ".join(_sys.argv[2:]) or None))
