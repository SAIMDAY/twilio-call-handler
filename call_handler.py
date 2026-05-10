import os
import json
import logging
from collections import defaultdict
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial, Start
from twilio.rest import Client
import requests

DAVID_CELL = os.getenv("DAVID_CELL", "+19752083042")
TRANSCRIPTION_CALLBACK = os.getenv("TRANSCRIPTION_CALLBACK", "https://twilio.sammieai.org/transcription")
STATUS_CALLBACK = os.getenv("STATUS_CALLBACK", "https://twilio.sammieai.org/status")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("GMAIL_CHAT_ID")
LETTA_API_KEY = os.getenv("LETTA_API_KEY")
AGENT_ID = os.getenv("AGENT_ID")
LETTA_API_BASE_URL = os.getenv("LETTA_API_BASE_URL", "https://api.letta.com")
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

transcription_state = defaultdict(lambda: {"inbound_track": [], "outbound_track": []})
caller_numbers = {}

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

app = Flask(__name__)


@app.route("/voice", methods=["POST"])
def voice():
    """Handle incoming call: start transcription, then dial David with caller ID passthrough."""
    from_number = request.form.get("From", "Unknown")
    call_sid = request.form.get("CallSid", "")
    caller_numbers[call_sid] = from_number
    logger.info(f"Incoming call from {from_number} (SID: {call_sid})")

    response = VoiceResponse()

    # 1) Start real-time transcription BEFORE the Dial.
    #    This forks the parent call audio to the STT engine.
    #    statusCallbackUrl receives: transcription-started, -content, -stopped, -error
    start = Start()
    start.transcription(
        status_callback_url=TRANSCRIPTION_CALLBACK,
        track="both_tracks",
        inbound_track_label="caller",
        outbound_track_label="david",
        transcription_engine="google",
        speech_model="telephony",
        partial_results=False,
    )
    response.append(start)

    # 2) Dial David with caller ID passthrough.
    #    statusCallback lives on the <Number> noun, NOT on <Dial>.
    #    This gives us call-progress events for the outbound leg as a fallback.
    dial = Dial(caller_id=from_number)
    dial.number(
        DAVID_CELL,
        status_callback_event="initiated ringing answered completed",
        status_callback=STATUS_CALLBACK,
        status_callback_method="POST",
    )
    response.append(dial)

    twiml_str = str(response)
    logger.info(f"TwiML response: {twiml_str}")
    return Response(twiml_str, mimetype="text/xml")


@app.route("/status", methods=["POST"])
def status():
    """Call-progress events from the <Number> statusCallback.
    FALLBACK: if TwiML Transcription doesn't start automatically,
    we start API-based transcription when the child call is answered."""
    call_status = request.form.get("CallStatus", "")
    call_sid = request.form.get("CallSid", "")
    parent_call_sid = request.form.get("ParentCallSid", "")
    logger.info(f"Call status: {call_status} for SID: {call_sid} (parent: {parent_call_sid})")

    if call_status in ("in-progress", "answered"):
        target_sid = parent_call_sid if parent_call_sid else call_sid
        try:
            client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
            t = client.calls(target_sid).transcriptions.create(
                status_callback_url=TRANSCRIPTION_CALLBACK,
                track="both_tracks",
                inbound_track_label="caller",
                outbound_track_label="david",
                language_code="en-US",
                partial_results=False,
            )
            logger.info(f"FALLBACK: Started API transcription {t.sid} on call {target_sid}")
        except Exception as e:
            logger.error(f"FALLBACK: Failed to start transcription: {e}")

    return "", 200


@app.route("/transcription", methods=["POST"])
def transcription():
    """Receive real-time transcription events from Twilio.
    Handles: transcription-started, transcription-content, transcription-stopped, transcription-error."""
    data = request.get_json(silent=True) or {}
    event = data.get("TranscriptionEvent", "")
    call_sid = data.get("CallSid", "")

    if event == "transcription-started":
        logger.info(f"Transcription started for call {call_sid} (SID: {data.get('TranscriptionSid', '')})")

    elif event == "transcription-content":
        track = data.get("Track", "")
        raw = data.get("TranscriptionData", "")
        is_final = data.get("Final", "false") == "true"
        try:
            td = json.loads(raw) if raw else {}
            transcript = td.get("transcript", "").strip()
        except json.JSONDecodeError:
            transcript = raw

        if transcript and is_final:
            transcription_state[call_sid][track].append(transcript)
            logger.info(f"[{call_sid}] {track}: {transcript}")

    elif event == "transcription-stopped":
        segments = transcription_state.pop(call_sid, {"inbound_track": [], "outbound_track": []})
        caller_number = caller_numbers.pop(call_sid, "Unknown")
        caller_text = " ".join(segments.get("inbound_track", [])).strip()
        david_text = " ".join(segments.get("outbound_track", [])).strip()

        if not caller_text and not david_text:
            send_telegram(f"\u1f4de Call from {caller_number}\n\n(No transcription available)")
            return "", 200

        parts = []
        if caller_text:
            parts.append(f"Caller: {caller_text}")
        if david_text:
            parts.append(f"David: {david_text}")
        full_transcription = "\n\n".join(parts)

        message = f"[CALL TRANSCRIPTION]\nFrom: {caller_number}\n\n{full_transcription}"
        reply = send_to_sammie(message)
        if reply:
            send_telegram(f"\u1f4de Call from {caller_number}\n\n{reply}")
        else:
            send_telegram(f"\u1f4de Call from {caller_number}\n\n(No reply from Sammie)")

    elif event == "transcription-error":
        logger.error(f"Transcription error: {data.get('TranscriptionErrorCode', '')} - {data.get('TranscriptionError', '')}")

    return "", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


def send_to_sammie(text):
    url = f"{LETTA_API_BASE_URL}/v1/agents/{AGENT_ID}/messages"
    headers = {"Authorization": f"Bearer {LETTA_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json={"input": text}, headers=headers, timeout=60)
        if resp.status_code == 200:
            reply = ""
            for msg in resp.json().get("messages", []):
                if msg.get("message_type") == "assistant_message" and msg.get("content"):
                    reply += msg["content"] + "\n"
            return reply.strip() or None
        logger.error(f"Letta API error {resp.status_code}: {resp.text}")
    except Exception as e:
        logger.error(f"Letta API failed: {e}")
    return None


def send_telegram(text):
    if not TELEGRAM_TOKEN or not CHAT_ID:
        return
    try:
        requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram failed: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
