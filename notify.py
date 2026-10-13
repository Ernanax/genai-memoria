"""Envoi de SMS à la famille (Twilio).

Sans identifiants Twilio, send_sms se contente d'afficher le message. L'envoi ne lève
jamais d'erreur, pour ne pas interrompre un appel.

Variables d'environnement : TWILIO_SID, TWILIO_TOKEN, TWILIO_FROM et SMS_DESTINATAIRES
(numéros séparés par des virgules).
"""

import os
import unicodedata


def _recipients():
    return [n.strip() for n in os.getenv("SMS_DESTINATAIRES", "").split(",") if n.strip()]


# Les comptes Twilio d'essai refusent les SMS en plusieurs parties (erreur 30044) : le texte
# est converti en ASCII et limité à un seul SMS.
_SMS_MAX = 155
_REPLACE = {"—": "-", "–": "-", "↔": "-", "…": "...", "«": '"', "»": '"',
            "’": "'", "‘": "'", "“": '"', "”": '"', "€": "EUR", "→": "->"}


def _gsm7(body: str) -> str:
    for k, v in _REPLACE.items():
        body = body.replace(k, v)
    # retire les accents puis tout caractère non ASCII
    body = unicodedata.normalize("NFKD", body).encode("ascii", "ignore").decode("ascii")
    body = " ".join(body.split())  # regroupe espaces et retours à la ligne
    if len(body) > _SMS_MAX:
        body = body[: _SMS_MAX - 3].rstrip() + "..."
    return body


def send_sms(body: str) -> dict:
    """Envoie le SMS à chaque destinataire séparément et renvoie {delivered, failed, disabled}.
    Ne lève jamais d'erreur."""
    body = _gsm7(body)   # ASCII, un seul SMS
    sid = os.getenv("TWILIO_SID") or os.getenv("TWILIO_ACCOUNT_SID")
    token = os.getenv("TWILIO_TOKEN") or os.getenv("TWILIO_AUTH_TOKEN")
    frm, to = os.getenv("TWILIO_FROM"), _recipients()
    if not (sid and token and frm and to):
        print(f"[SMS désactivé — renseignez TWILIO_* et SMS_DESTINATAIRES] {body[:120]}")
        return {"delivered": [], "failed": [], "disabled": True}

    delivered, failed = [], []
    try:
        from twilio.rest import Client  # import tardif : l'application fonctionne sans twilio
        client = Client(sid, token)
    except Exception as e:               # twilio absent
        print(f"[SMS échec import] {e}")
        return {"delivered": [], "failed": [{"num": n, "error": str(e)} for n in to], "disabled": False}

    for num in to:
        try:
            client.messages.create(body=body, from_=frm, to=num)
            delivered.append(num)
        except Exception as e:           # ex. numéro non vérifié : on passe au suivant
            failed.append({"num": num, "error": str(e)})
            print(f"[SMS échec -> {num}] {e}")
    if delivered:
        print(f"[SMS envoyé -> {', '.join(delivered)}] {body[:80]}")
    return {"delivered": delivered, "failed": failed, "disabled": False}
