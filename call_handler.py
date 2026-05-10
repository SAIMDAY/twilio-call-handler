import os
import json
import logging
from collections import defaultdict
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial, Start
from twilio.rest import Client
import requests as http_requests

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
    from_number = request.form.get("From", "Unknown")
    call_sid = request.form.get("CallSid", "")
    caller_numbers[call_sid] = from_number
    logger.info(f"Incoming call from {from_number} (SID: {call_sid})")

    response = VoiceResponse()

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


@app.route("/transcription", methods=["GET", "POST", "PUT"])
def transcription():
    # DEBUG: Log ALL incoming requests
    logger.info(f"/transcription HIT - Method: {request.method}")
    logger.info(f"/transcription Content-Type: {request.content_type}")
    logger.info(f"/transcription Raw body: {request.get_data(as_text=True)[:2000]}")

    data = request.get_json(silent=True) or {}
    if not data:
        form_data = request.form.to_dict()
        if form_data:
            data = form_data
            logger.info(f"/transcription parsed as FORM data: {list(data.keys())}")

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
            send_telegram(f"Call from {caller_number} - no transcription available")
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
            send_telegram(f"Call from {caller_number}\n\n{reply}")
        else:
            send_telegram(f"Call from {caller_number}\n\n(No reply from Sammie)")

    elif event == "transcription-error":
        logger.error(f"Transcription error: {data.get('TranscriptionErrorCode', '')} - {data.get('TranscriptionError', '')}")
    else:
        logger.info(f"/transcription unknown event: {event} | data keys: {list(data.keys())}")

    return "", 200


@app.route("/health", methods=["GET"])
def health():
    return {"status": "ok"}, 200


def send_to_sammie(text):
    url = f"{LETTA_API_BASE_URL}/v1/agents/{AGENT_ID}/messages"
    headers = {"Authorization": f"Bearer {LETTA_API_KEY}", "Content-Type": "application/json"}
    try:
        resp = http_requests.post(url, json={"input": text}, headers=headers, timeout=60)
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
        http_requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id": CHAT_ID, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.error(f"Telegram failed: {e}")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 5000)))
