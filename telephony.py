"""Vrais appels téléphoniques via Twilio.

Jeanne appelle un numéro Twilio et MemorIA répond avec la voix clonée du proche choisi.
Les échanges se font tour par tour (TwiML) : message d'accueil, reconnaissance vocale par
Twilio, réponse de l'agent, synthèse ElevenLabs, puis écoute de la phrase suivante.

Configuration dans le .env : PUBLIC_URL (tunnel public vers l'application) et PHONE_CONTACT.
"""

import os
import uuid
from pathlib import Path
from xml.sax.saxutils import escape

from flask import request, Response, send_from_directory, jsonify

from agent import greeting_for
from tts import synthesize

BASE = Path(__file__).resolve().parent
AUDIO_DIR = BASE / "tts_cache"
AUDIO_DIR.mkdir(exist_ok=True)

PUBLIC_URL = os.getenv("PUBLIC_URL", "").rstrip("/")
PHONE_CONTACT = os.getenv("PHONE_CONTACT", "anael")
FAST_MODEL = "eleven_flash_v2_5"   # synthèse rapide pour le téléphone (même voix)

# Appel sortant : MemorIA appelle votre téléphone.
TWILIO_SID = os.getenv("TWILIO_ACCOUNT_SID") or os.getenv("TWILIO_SID")
TWILIO_TOKEN = os.getenv("TWILIO_AUTH_TOKEN") or os.getenv("TWILIO_TOKEN")
TWILIO_FROM = os.getenv("TWILIO_FROM")      # numéro Twilio
MY_NUMBER = os.getenv("MY_NUMBER")          # téléphone qui sonne (numéro vérifié)


def configure_number():
    """Configure le numéro Twilio au démarrage (webhook d'appel et callback de fin d'appel), pour
    que chaque raccrochage déclenche le résumé et le SMS. Ne fait rien sans identifiants."""
    if not (TWILIO_SID and TWILIO_TOKEN and TWILIO_FROM and PUBLIC_URL):
        print("[telephony] auto-config ignorée (TWILIO_* / PUBLIC_URL manquants)")
        return
    try:
        from twilio.rest import Client
        client = Client(TWILIO_SID, TWILIO_TOKEN)
        nums = client.incoming_phone_numbers.list(phone_number=TWILIO_FROM)
        if not nums:
            print(f"[telephony] numéro {TWILIO_FROM} introuvable sur le compte Twilio")
            return
        nums[0].update(
            voice_url=f"{PUBLIC_URL}/voice/incoming", voice_method="POST",
            status_callback=f"{PUBLIC_URL}/voice/status", status_callback_method="POST",
        )
        print(f"[telephony] numéro {TWILIO_FROM} configuré → {PUBLIC_URL}/voice/incoming "
              f"(+ status callback → SMS à chaque raccrochage)")
    except Exception as e:
        print(f"[telephony] auto-config échec (à faire à la main dans la console) : {e}")


def place_call(contact_id=None):
    """Twilio appelle MY_NUMBER, puis la conversation passe par /voice/incoming."""
    if not all([TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM, MY_NUMBER, PUBLIC_URL]):
        return {"error": "config Twilio incomplète (SID/token/from/my_number/PUBLIC_URL dans .env)"}
    from twilio.rest import Client
    client = Client(TWILIO_SID, TWILIO_TOKEN)
    url = f"{PUBLIC_URL}/voice/incoming"
    if contact_id:
        url += f"?contact={contact_id}"
    call = client.calls.create(
        to=MY_NUMBER, from_=TWILIO_FROM, url=url, method="POST",
        status_callback=f"{PUBLIC_URL}/voice/status",
        status_callback_event=["completed"], status_callback_method="POST",
    )
    return {"status": "calling", "to": MY_NUMBER, "sid": call.sid}


def _save_tts(text, contact):
    """Génère le mp3 avec la voix clonée du proche (modèle rapide)."""
    audio = synthesize(text, contact.get("voice_id"), contact.get("voice_settings"),
                       model=FAST_MODEL)
    if not audio:
        return None
    fname = f"{uuid.uuid4().hex}.mp3"
    (AUDIO_DIR / fname).write_bytes(audio)
    return f"{PUBLIC_URL}/audio/{fname}"


def _twiml(inner):
    return Response(f'<?xml version="1.0" encoding="UTF-8"?><Response>{inner}</Response>',
                    mimetype="text/xml")


def _gather():
    """Bloc <Gather> qui écoute la réponse et l'envoie à /voice/reply."""
    return (f'<Gather input="speech" language="fr-FR" speechTimeout="1" '
            f'speechModel="phone_call" '
            f'action="{PUBLIC_URL}/voice/reply" method="POST">'
            f'</Gather>'
            # si rien n'est entendu, on relance
            f'<Redirect method="POST">{PUBLIC_URL}/voice/reply</Redirect>')


def _speak_block(text, contact):
    """Joue la voix clonée si elle est disponible, sinon la voix française de Twilio."""
    url = _save_tts(text, contact)
    if url:
        return f'<Play>{url}</Play>'
    return f'<Say language="fr-FR" voice="Polly.Lea-Neural">{escape(text)}</Say>'


def register(app, agent, run):
    """Ajoute les routes de téléphonie à l'application Flask."""

    configure_number()   # configure le webhook du numéro Twilio

    @app.route("/audio/<fname>")
    def audio(fname):
        return send_from_directory(AUDIO_DIR, fname, mimetype="audio/mpeg")

    @app.route("/voice/incoming", methods=["GET", "POST"])
    def voice_incoming():
        # Nouvel appel : la ligne incarne le proche choisi (ou PHONE_CONTACT).
        contact_id = request.values.get("contact") or PHONE_CONTACT
        contact = agent.set_contact(contact_id)
        greeting = greeting_for(contact)
        return _twiml(_speak_block(greeting, contact) + _gather())

    @app.route("/call-me", methods=["POST"])
    def call_me():
        """Lance un appel sortant vers votre téléphone (bouton de démo)."""
        contact_id = (request.json or {}).get("contact") or PHONE_CONTACT
        return jsonify(place_call(contact_id))

    @app.route("/voice/status", methods=["POST"])
    def voice_call_status():
        """Appelée par Twilio en fin d'appel, même en cas de raccrochage brutal : écrit le résumé
        si l'appel n'a pas été clos."""
        status = request.values.get("CallStatus", "")
        if status in ("completed", "busy", "no-answer", "failed", "canceled"):
            if agent.messages:            # appel pas encore clos
                run(agent.hangup())
        return ("", 204)

    @app.route("/voice/reply", methods=["POST"])
    def voice_reply():
        speech = (request.form.get("SpeechResult") or "").strip()
        contact = agent.contact
        if not speech:
            # rien entendu : on relance doucement
            return _twiml(_speak_block("Tu es là, mamie ? Je t'écoute.", contact) + _gather())
        result = run(agent.chat(speech))
        reply = result.get("reply") or "Je suis là, mamie."
        block = _speak_block(reply, contact)
        if result.get("ended"):
            return _twiml(block + "<Hangup/>")
        return _twiml(block + _gather())
