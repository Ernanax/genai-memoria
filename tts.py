"""Synthèse vocale ElevenLabs (une voix par proche).

synthesize renvoie un mp3, ou None pour utiliser la voix du navigateur.
La clé API est lue dans le .env.
"""

import os

import httpx
from dotenv import load_dotenv

load_dotenv()

ELEVEN_KEY = os.getenv("ELEVENLABS_API_KEY")
ELEVEN_MODEL = "eleven_multilingual_v2"   # le plus expressif, bon en français
ENDPOINT = "https://api.elevenlabs.io/v1/text-to-speech/{voice_id}"

# Réglages par défaut (voix douce), modifiables par proche dans companion_config.json.
DEFAULT_SETTINGS = {"stability": 0.4, "similarity_boost": 0.8, "style": 0.35,
                    "use_speaker_boost": True, "speed": 0.95}


def available():
    return bool(ELEVEN_KEY)


def synthesize(text, voice_id, settings=None, model=None):
    """Renvoie le mp3 de `text` dans la voix donnée, ou None. `settings` ajuste la voix et
    `model` remplace le modèle (ex. modèle rapide pour le téléphone)."""
    if not ELEVEN_KEY or not voice_id or not text.strip():
        return None
    voice_settings = {**DEFAULT_SETTINGS, **(settings or {})}
    try:
        r = httpx.post(
            ENDPOINT.format(voice_id=voice_id),
            headers={"xi-api-key": ELEVEN_KEY, "Content-Type": "application/json"},
            json={
                "text": text,
                "model_id": model or ELEVEN_MODEL,
                "voice_settings": voice_settings,
            },
            timeout=30,
        )
        if r.status_code == 200:
            return r.content
        # Affiche l'erreur (quota, voice_id invalide...) ; l'appelant utilisera la voix du navigateur.
        print(f"[tts] ElevenLabs {r.status_code}: {r.text[:200]}")
    except Exception as exc:
        print(f"[tts] error: {exc}")
    return None
