import os
import json
import logging
from collections import defaultdict
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse, Dial, Start
import requests

# ====================== CONFIG ======================
DAVID_CELL = os.getenv("DAVID_CELL", "+19752083042")
TRANSCRIPTION_CALLBACK = os.getenv(
    "TRANSCRIPTION_CALLBACK",
    "https://twilio.sammieai.org/transcription"
)
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("GMAIL_CHAT_ID")
LETTA_API_KEY = os.getenv("LETTA_API_KEY")
AGENT_ID = os.getenv("AGENT_ID")
LETTA_API_BASE_URL = os.getenv("LETTA_API_BASE_URL", "https://api.letta.com")

# ====================== STATE ======================
transcription_state = defaultdict(lambda: {"inbound_track": [], "outbound_track": []})
caller_numbers = {}

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ====================== FLASK APP ======================
app = Flask(__name__)


@app.route("/voice", methods=["POST"])
def voice():
    """Handle incoming call: forward to David with caller ID passthrough + real-time transcription."""
    from_number = request.form.get("From", "Unknown")
    call_sid = request.form.get("CallSid", "")

    caller_numbers[call_sid] = from_number

    response = VoiceResponse()

    # Start real-time transcription using TwiML
    start = Start()
    start.transcription(
        status_callback_url=TRANSCRIPTION_CALLBACK,
        track="both_tracks",
        inbound_track_label="caller",
        outbound_track_label="david",
        language_code="en-US",
        partial_results=False,
    )
    response.append(start)

    # Forward call to David's eSIM with caller ID passthrough
    dial = Dial(caller_id=from_number)
    dial.number(DAVID_CELL)
    response.append(dial)

    return Response(str(response), mimetype="text/xml")


@app.route("/transcription", methods=["POST"])
def transcription():
    """Handle real-time transcription events from Twilio."""
    data = request.get_json(silent=True) or {}
    event = data.get("TranscriptionEvent", "")
    call_sid = data.get("CallSid", "")

    if event == "transcription-started":
        logger.info(f"Transcription started for call {call_sid}")

    elif event == "transcription-content":
        track = data.get("Track", "")
        transcription_data_raw = data.get("TranscriptionData", "")
        is_final = data.get("Final", "false") == "true"

        try:
            trans_data = json.loads(transcription_data_raw) if transcription_data_raw else {}
            transcript = trans_data.get("transcript", "").strip()
        except json.JSONDecodeError:
            transcript = transcription_data_raw

        if transcript and is_final:
            transcription_state[call_sid][track].append(transcript)
            logger.info(f"[{call_sid}] {track}: {transcript}")

    elif event == "transcription-stopped":
        segments = transcription_state.pop(call_sid, {"inbound_track": [], "outbound_track": []})
        caller_number = caller_numbers.pop(call_sid, "Unknown")

        caller_text = " ".join(segments.get("inbound_track", [])).strip()
        david_text = " ".join(segments.get("outbound_track", [])).strip()

        if not caller_text and not david_text:
            send_telegram(f"📞 Call from {caller_number}\n\n(No transcription available)")
            return "", 200

        parts = []
        if caller_text:
            parts.append(f"Caller: {caller_text}")
        if david_text:
            parts.append(f"David: {david_text}")

        full_transcription = "\n\n".join(parts)

        message = (
            f"[CALL TRANSCRIPTION]\n"
            f"From: {caller_number}\n\n"
            f"{full_transcription}"
        )

        reply = send_to_sammie(message)
        if reply:
            send_telegram(f"📞 Call from {caller_number}\n\n{reply}")
        else:
            send_telegram(f"📞 Call from {caller_number}\n\n(No reply from Sammie)")

    elif event == "transcription-error":
        error_code = data.get("TranscriptionErrorCode", "")
        error_msg = data.get("TranscriptionError", "")
        logger.error(f"Transcription error for call {call_sid}: {error_code} - {error_msg}")

    return "", 200


@app.route("/health", methods=["GET"])
def health():
    """Health check endpoint."""
    return {"status": "ok"}, 200


def send_to_sammie(text: str) -> str | None:
    """Send message to Sammie and return assistant's reply."""
    url = f"{LETTA_API_BASE_URL}/v1/agents/{AGENT_ID}/messages"
    headers = {
        "Authorization": f"Bearer {LETTA_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {"input": text}

    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=60)
        if resp.status_code == 200:
            data = resp.json()
            reply = ""
            for msg in data.get("messages", []):
                if msg.get("message_type") == "assistant_message":
                    content = msg.get("content")
                    if content:
                        reply += content + "\n"
            return reply.strip() or None
        else:
            logger.error(f"Letta API error {resp.status_code}: {resp.text}")
            return None
    except Exception as e:
        logger.error(f"Failed to communicate with Letta: {e}")
        return None


def send_telegram(text: str):
    """Send notification to Telegram."""
    if not TELEGRAM_TOKEN or not CHAT_ID:
        logger.warning("Telegram credentials not configured")
        return

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": text}

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
