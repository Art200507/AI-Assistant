"""Twilio inbound/outbound call handling — runs on the ORACLE SERVER, not
the Mac (user's explicit choice: always reachable even if the Mac is
asleep, and talks to jarvis_respond.py directly instead of hopping back
over SSH to itself). Deployed to ~/JARVIS/phone/ on the server; this local
copy is the source of truth, kept in sync by hand the same way
jarvis_respond.py itself is (edit locally, scp + py_compile + move into
place — see STATUS.md for the established deploy pattern).

SCAFFOLD, still inert: does not start listening until run explicitly, and
running it does nothing useful until phone/.env has real Twilio
credentials (config.py refuses to proceed otherwise — see
config.require_configured()). No signup performed on the user's behalf.

Binds to 0.0.0.0 (not just localhost) because this now needs to be
reachable from the public internet on the Oracle server — reaching it
requires the server's firewall/security-group to actually allow inbound
traffic on LISTEN_PORT, which is a separate, deliberate step (not done by
importing this file) since it's the one piece that changes this server's
public attack surface. Do that only once real Twilio credentials are in
place, so a half-configured public webhook is never sitting open.

Call flow once activated:
  1. Twilio hits POST /voice when the owner's number receives a call.
  2. Caller is checked against JARVIS_OWNER_PHONE_NUMBER (reject anyone
     else) and the request signature is checked against TWILIO_AUTH_TOKEN
     (reject anything not actually sent by Twilio).
  3. <Gather input="speech"> records and transcribes the caller's speech
     using Twilio's own STT (not Groq/whisper — Twilio needs the
     transcript before it can call back into this webhook, so this one
     leg uses their speech recognition rather than the local pipeline).
  4. POST /voice/process receives the transcript and calls
     jarvis_respond.respond() DIRECTLY (no SSH — this server IS where
     jarvis_respond.py already lives), using the call's own CallSid as
     the session_id so a single phone call has real continuous memory
     turn-to-turn, then speaks the answer back with Twilio's <Say>
     (Polly.Brian — closest match to the local Piper British voice
     without needing to host audio files publicly).

Scope: fast + cloud tiers only, no local-Claude-on-Mac — the Mac isn't
guaranteed to be awake when a call comes in, which is the whole reason
this runs on the always-on server instead.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))  # jarvis_respond.py's directory

from flask import Flask, request, Response
from twilio.request_validator import RequestValidator
from twilio.twiml.voice_response import VoiceResponse, Gather

import config
import jarvis_respond as jr  # noqa: E402  (path insert above must run first)

app = Flask(__name__)

LISTEN_HOST = "0.0.0.0"  # public — see module docstring on the firewall step
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
    # CallSid is unique per call and stable across all turns of that same
    # call — using it as the session_id gives a phone call real
    # turn-to-turn memory via the same shared-transcript mechanism the
    # voice/ pipeline uses, instead of every turn starting fresh.
    call_sid = request.form.get("CallSid", "")
    resp = VoiceResponse()

    if speech_text:
        answer = jr.respond(speech_text, session_id=call_sid or None)
    else:
        answer = "I didn't catch that."
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
    print(f"Starting Twilio webhook server on {LISTEN_HOST}:{LISTEN_PORT}.")
    print("Make sure the server's firewall actually allows inbound traffic on this port.")
    app.run(host=LISTEN_HOST, port=LISTEN_PORT, debug=False)
