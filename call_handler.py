import os
import logging
from flask import Flask, request, Response
from twilio.twiml.voice_response import VoiceResponse
import requests

# ====================== CONFIG ======================
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("GMAIL_CHAT_ID")          # Rename this env var if it's not Gmail-related
LETTA_API_KEY = os.getenv("LETTA_API_KEY")
AGENT_ID = os.getenv("AGENT_ID")
LETTA_API_BASE_URL = os.getenv("LETTA_API_BASE_URL", "https://api.letta.com")

# Callback URLs (highly recommended to use env vars in production)
TRANSCRIBE_CALLBACK = os.getenv(
    "TRANSCRIBE_CALLBACK", 
    "https://YOUR-RENDER-APP.onrender.com/transcription"
)
RECORDING_STATUS_CALLBACK = os.getenv(
    "RECORDING_STATUS_CALLBACK", 
    "https://YOUR-RENDER-APP.onrender.com/recording-status"
)

# ====================== LOGGING ======================
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ====================== FLASK APP ======================
app = Flask(__name__)


@app.route("/voice", methods=["POST"])
def voice():
    """Handle incoming voice call."""
    response = VoiceResponse()
    
    response.say("This call may be recorded for quality purposes.", voice="alice")
    
    response.record(
        timeout=10,
        max_length=3600,
        transcribe=True,
        transcribe_callback=TRANSCRIBE_CALLBACK,
        recording_status_callback=RECORDING_STATUS_CALLBACK,
        recording_status_callback_event="completed"
    )
    
    return Response(str(response), mimetype="text/xml")


@app.route("/transcription", methods=["POST"])
def transcription():
    """Handle transcription callback from Twilio."""
    transcription_text = request.form.get("TranscriptionText", "").strip()
    from_number = request.form.get("From", "Unknown")
    call_sid = request.form.get("CallSid", "")
    
    logger.info(f"Transcription received from {from_number} | CallSid: {call_sid}")

    if transcription_text:
        message = (
            f"[AUTOMATED CALL TRANSCRIPTION]\n"
            f"Caller: {from_number}\n"
            f"Call SID: {call_sid}\n\n"
            f"Transcription:\n{transcription_text}"
        )
        
        reply = send_to_sammie(message)
        
        if reply:
            send_telegram(f"📞 Call from {from_number}\n\n{reply}")
        else:
            send_telegram(f"📞 Call from {from_number}\n\n(No reply from Sammie)")
    else:
        send_telegram(f"📞 Call from {from_number} - No transcription available")

    return "", 200


@app.route("/recording-status", methods=["POST"])
def recording_status():
    """Handle recording status callback."""
    status = request.form.get("RecordingStatus", "")
    call_sid = request.form.get("CallSid", "")
    logger.info(f"Recording status - CallSid: {call_sid} | Status: {status}")
    return "", 200


def send_to_sammie(text: str):
    """Send message to Letta agent and return assistant reply."""
    url = f"{LETTA_API_BASE_URL}/v1/agents/{AGENT_ID}/messages"
    headers = {
        "Authorization": f"Bearer {LETTA_API_KEY}",
        "Content-Type": "application/json"
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
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "Markdown"}

    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram message: {e}")


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
