"""Trigger an outbound call from Jarvis to the owner's phone — SCAFFOLD ONLY.

Will not run until phone/.env has real Twilio credentials. Requires
PUBLIC_BASE_URL (see .env.example) so Twilio has somewhere to fetch the
call's TwiML from (twilio_server.py's /voice route).

Usage once activated:
    python3 make_call.py "Sir, the task queue just cleared."
"""
import sys

import config
from twilio.rest import Client


def make_call(message: str = ""):
    config.require_configured()
    client = Client(config.TWILIO_ACCOUNT_SID, config.TWILIO_AUTH_TOKEN)

    # If a message is given, speak it directly via inline TwiML instead of
    # routing through /voice (skips the "how can I help" prompt for a
    # one-way notification call).
    if message:
        safe_message = message.replace('"', "'")
        twiml = f'<Response><Say voice="Polly.Brian">{safe_message}</Say></Response>'
        call = client.calls.create(
            twiml=twiml,
            to=config.JARVIS_OWNER_PHONE_NUMBER,
            from_=config.TWILIO_PHONE_NUMBER,
        )
    else:
        call = client.calls.create(
            url=f"{config.PUBLIC_BASE_URL}/voice",
            to=config.JARVIS_OWNER_PHONE_NUMBER,
            from_=config.TWILIO_PHONE_NUMBER,
        )
    print(f"Call started: {call.sid}")
    return call.sid


if __name__ == "__main__":
    make_call(" ".join(sys.argv[1:]))
