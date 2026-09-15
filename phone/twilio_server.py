"""Twilio inbound/outbound call handling — SCAFFOLD ONLY.

This does not start listening until you run it explicitly, and running it
does nothing useful until phone/.env has real Twilio credentials (config.py
refuses to proceed otherwise). No signup was performed on your behalf, and
this file does not bind to anything but 127.0.0.1 — reaching it from Twilio
requires you to run a tunnel (ngrok) yourself, which is also not done here.

Call flow once activated:
  1. Twilio hits POST /voice when your number receives a call.
  2. We check the caller is JARVIS_OWNER_PHONE_NUMBER (reject anyone else).
  3. <Gather input="speech"> records and transcribes the caller's speech
     using Twilio's own STT (not whisper.cpp — Twilio needs the transcript
     before it can call back into our webhook, so this one leg uses their
     speech recognition rather than the local pipeline).
  4. POST /voice/process receives the transcript, forwards it to the same
     jarvis_client.ask_jarvis() the voice/ pipeline uses (SSH -> server's
     jarvis_respond.py), and speaks the answer back with Twilio's <Say>
     (Polly.Brian — a British neural voice, closest match to the local
     Piper British voice without needing to host audio files publicly).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "voice"))

from flask import Flask, request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather

import config
from jarvis_client import ask_jarvis  # noqa: E402  (path insert above must run first)

app = Flask(__name__)

LISTEN_HOST = "127.0.0.1"  # localhost only — a tunnel (ngrok) is required to expose this, and that's on you
LISTEN_PORT = 5005


def _validate_twilio_request() -> bool:
    """Reject anything not actually signed by Twilio (prevents spoofed webhook calls)."""
    validator = RequestValidator(config.TWILIO_AUTH_TOKEN)
    signature = request.headers.get("X-Twilio-Signature", "")
    url = f"{config.PUBLIC_BASE_URL}{request.path}"
    return validator.validate(url, request.form, signature)


def _caller_is_owner() -> bool:
    caller = request.form.get("From", "")
    return caller == config.JARVIS_OWNER_PHONE_NUMBER


@app.route("/voice", methods=["POST"])
def voice():
    if not _validate_twilio_request():
        return Response(status=403)

    resp = VoiceResponse()
    if not _caller_is_owner():
        resp.say("This line is private. Goodbye.", voice="Polly.Brian")
        resp.hangup()
        return Response(str(resp), mimetype="text/xml")

    gather = Gather(
        input="speech",
        action="/voice/process",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    gather.say("Good evening. How can I help?", voice="Polly.Brian")
    resp.append(gather)
    resp.say("I didn't catch that. Goodbye.", voice="Polly.Brian")
    return Response(str(resp), mimetype="text/xml")


@app.route("/voice/process", methods=["POST"])
def voice_process():
    if not _validate_twilio_request() or not _caller_is_owner():
        return Response(status=403)

    speech_text = request.form.get("SpeechResult", "")
    resp = VoiceResponse()

    answer = ask_jarvis(speech_text) if speech_text else "I didn't catch that."
    resp.say(answer, voice="Polly.Brian")

    # Offer another turn instead of hanging up immediately.
    gather = Gather(
        input="speech",
        action="/voice/process",
        method="POST",
        speech_timeout="auto",
        timeout=6,
    )
    resp.append(gather)
    resp.say("Goodbye.", voice="Polly.Brian")
    return Response(str(resp), mimetype="text/xml")


if __name__ == "__main__":
    config.require_configured()
    print(f"Starting Twilio webhook server on {LISTEN_HOST}:{LISTEN_PORT} (localhost only).")
    print("Twilio cannot reach this until you point a public tunnel (e.g. ngrok) at it — see phone/.env.example.")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
