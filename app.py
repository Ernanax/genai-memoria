"""Application web MemorIA (Flask).

  /         écran de Jeanne
  /family   tableau de bord de la famille

L'agent garde une session MCP ouverte, sur une boucle asyncio dans un thread dédié.
Lancement : python app.py (http://127.0.0.1:5002)
"""

import asyncio
import json
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, request, jsonify, send_from_directory, Response

from agent import (CompanionAgent, FamilyAgent, generate_family_recap,
                   CONTACTS, greeting_for, get_contact)
from tts import synthesize, available as tts_available

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"

app = Flask(__name__, static_folder="static")

# --- Boucle asyncio dédiée à l'agent et à sa session MCP ---
loop = asyncio.new_event_loop()
threading.Thread(target=loop.run_forever, daemon=True).start()
agent = CompanionAgent()
family_bot = FamilyAgent()   # assistant d'enrichissement pour la famille


def run(coro, timeout=180):
    """Exécute une coroutine sur la boucle de l'agent depuis Flask."""
    return asyncio.run_coroutine_threadsafe(coro, loop).result(timeout=timeout)


tool_names = run(agent.start())
print(f"MemorIA ready — MCP tools: {tool_names}")


def read_json(name):
    return json.loads((DATA / name).read_text(encoding="utf-8"))


def write_json(name, obj):
    (DATA / name).write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def daily_activity(days=7):
    """Nombre d'appels et d'alertes warning ou urgent par jour, pour le graphique de la famille."""
    counts = {}

    def slot(iso_day):
        return counts.setdefault(iso_day, {"calls": 0, "warning": 0, "urgent": 0})

    for c in read_json("call_log.json"):
        slot(c["timestamp"][:10])["calls"] += 1
    for a in read_json("alerts.json"):
        if a.get("urgency") in ("warning", "urgent"):
            slot(a["timestamp"][:10])[a["urgency"]] += 1

    today = date.today()
    empty = {"calls": 0, "warning": 0, "urgent": 0}
    return [
        {"date": (today - timedelta(days=i)).isoformat(),
         **counts.get((today - timedelta(days=i)).isoformat(), empty)}
        for i in range(days - 1, -1, -1)
    ]


# --- Côté Jeanne ---

@app.route("/")
def index():
    return send_from_directory("static", "index.html")


@app.route("/contacts")
def contacts():
    return jsonify([
        {"id": c["id"], "prenom": c["prenom"], "lien": c["lien"], "emoji": c.get("emoji", "📞")}
        for c in CONTACTS
    ])


@app.route("/call", methods=["POST"])
def call():
    """Démarre un appel vers le proche choisi, que l'agent incarne."""
    contact_id = (request.json or {}).get("contact")
    c = agent.set_contact(contact_id)
    return jsonify({
        "contact": {"id": c["id"], "prenom": c["prenom"], "lien": c["lien"]},
        "greeting": greeting_for(c),
    })


@app.route("/chat", methods=["POST"])
def chat():
    message = (request.json or {}).get("message", "")
    if not message.strip():
        message = "[silence]"
    result = run(agent.chat(message))
    return jsonify(result)


@app.route("/tts", methods=["POST"])
def tts():
    """Lit le texte avec la voix du proche (mp3), ou renvoie 204 pour utiliser la voix du navigateur."""
    data = request.json or {}
    text = data.get("text", "").strip()
    if not text:
        return ("", 204)
    contact = get_contact(data.get("contact")) if data.get("contact") else agent.contact
    audio = synthesize(text, contact.get("voice_id"), contact.get("voice_settings"))
    if audio is None:
        return ("", 204)
    return Response(audio, mimetype="audio/mpeg")


@app.route("/voice-status")
def voice_status():
    return jsonify({"elevenlabs": tts_available()})


@app.route("/hangup", methods=["POST"])
def hangup():
    """Termine l'appel : l'agent rédige le résumé pour la famille."""
    return jsonify(run(agent.hangup()))


@app.route("/reset", methods=["POST"])
def reset():
    contact_id = (request.json or {}).get("contact")
    agent.reset(contact_id)
    return jsonify({"status": "new_call", "contact": agent.contact["prenom"]})


# --- Côté famille ---

@app.route("/family")
def family():
    return send_from_directory("static", "family.html")


@app.route("/family/data")
def family_data():
    persona = read_json("persona.json")
    companion = read_json("companion_config.json")
    return jsonify({
        "persona": {
            "prenom": persona["prenom"],
            "age": persona["age"],
            "ville": persona["ville"],
            "pathologie": persona["sante"]["pathologie"],
        },
        "companion": {
            "role": agent.contact["prenom"],
            "lien": agent.contact["lien"],
            "roster": ", ".join(f'{c["prenom"]} ({c["lien"]})' for c in companion["contacts"]),
            "consentement": companion["cadre_ethique"]["consentement"],
        },
        "alerts": list(reversed(read_json("alerts.json")))[:20],
        "calls": list(reversed(read_json("call_log.json")))[:20],
        "health": read_json("health_profile.json"),
        "pending": [p for p in read_json("pending_validations.json") if p["status"] == "pending"],
        "daily": daily_activity(),
        # Métadonnées seulement : la transcription complète est chargée à la demande.
        "transcripts": [
            {"id": t["id"], "ended_at": t["ended_at"], "contact": t["contact"],
             "turns": len(t["turns"])}
            for t in list(reversed(read_json("transcripts.json")))[:20]
        ],
    })


@app.route("/family/transcript/<tid>")
def family_transcript(tid):
    """Renvoie la transcription complète d'un appel."""
    t = next((t for t in read_json("transcripts.json") if t["id"] == tid), None)
    return (jsonify(t), 200) if t else (jsonify({"error": "not found"}), 404)


@app.route("/family/alerts/seen", methods=["POST"])
def alerts_seen():
    """Marque une alerte comme lue, ou toutes si aucun timestamp n'est donné."""
    ts = (request.json or {}).get("timestamp")
    alerts = read_json("alerts.json")
    for a in alerts:
        if ts is None or a["timestamp"] == ts:
            a["seen"] = True
    write_json("alerts.json", alerts)
    return jsonify({"status": "ok", "unseen": sum(1 for a in alerts if not a.get("seen"))})


@app.route("/family/validation/<action>", methods=["POST"])
def family_validation(action):
    """Décision de la famille sur un élément levé pendant un appel : approve l'écrit en base, reject le supprime."""
    if action not in ("approve", "reject"):
        return jsonify({"error": "unknown action"}), 404
    validation_id = (request.json or {}).get("id")
    if not validation_id:
        return jsonify({"error": "missing id"}), 400
    result = run(agent.call_tool(f"{action}_validation", {"validation_id": validation_id}))
    return jsonify(result)


@app.route("/family/recap", methods=["POST"])
def family_recap():
    return jsonify({"recap": generate_family_recap()})


# --- Vrais appels téléphoniques (Twilio) ---
import telephony
telephony.register(app, agent, run)


# --- Assistant d'enrichissement : discussion, propositions, validation puis écriture ---

@app.route("/family/assistant/chat", methods=["POST"])
def assistant_chat():
    message = (request.json or {}).get("message", "")
    return jsonify(family_bot.chat(message))   # {reply, why}


@app.route("/family/assistant/start", methods=["POST"])
def assistant_start():
    """Démarre une conversation en mode "free" ou "intake"."""
    mode = (request.json or {}).get("mode", "free")
    return jsonify({"mode": mode, "first": family_bot.start(mode)})


@app.route("/family/assistant/reset", methods=["POST"])
def assistant_reset():
    family_bot.reset()
    return jsonify({"status": "reset"})


@app.route("/family/assistant/extract", methods=["POST"])
def assistant_extract():
    """Fin de la conversation : propose les informations à ajouter à la base."""
    return jsonify({"proposals": family_bot.extract()})


@app.route("/family/assistant/apply", methods=["POST"])
def assistant_apply():
    """Écrit les propositions approuvées par la famille dans les fichiers concernés."""
    approved = set((request.json or {}).get("approved_ids", []))
    applied, trace = [], []
    for item in family_bot.proposals:
        if item["id"] not in approved:
            continue
        t, p = item["type"], item.get("payload", {})
        if t == "memory":
            run(agent.call_tool("ingest_memory", {
                "text": p.get("text", ""), "category": p.get("category", "recent"),
                "known_by": p.get("known_by") or ["tous"]}))
            trace.append({"tool": "🧠 Souvenir mémorisé", "input": {"texte": p.get("text", "")},
                          "why": "Fait durable sur la vie de Jeanne → ajouté à la base de souvenirs "
                                 "(RAG) et vectorisé pour être retrouvé pendant les appels."})
        elif t == "health":
            prof = read_json("health_profile.json")
            prof["observations"].append({
                "timestamp": datetime.now().isoformat(timespec="seconds"),
                "observation": p.get("observation", ""),
                "category": p.get("category", "observation"), "source": "famille"})
            write_json("health_profile.json", prof)
            trace.append({"tool": "🩺 Observation santé", "input": {"obs": p.get("observation", "")},
                          "why": "Élément de santé → ajouté au dossier santé suivi par la famille."})
        elif t == "persona_note":
            persona = read_json("persona.json")
            persona.setdefault("complements_famille", []).append({
                "text": p.get("text", ""),
                "added_at": datetime.now().isoformat(timespec="seconds")})
            write_json("persona.json", persona)
            trace.append({"tool": "👤 Note de profil", "input": {"texte": p.get("text", "")},
                          "why": "Élément de profil / contexte de vie → ajouté à la fiche de Jeanne."})
        elif t == "contact_detail":
            cfg = read_json("companion_config.json")
            for c in cfg["contacts"]:
                if c["id"] == p.get("contact_id"):
                    c.setdefault("details_partages", []).append(p.get("detail", ""))
                    break
            write_json("companion_config.json", cfg)
            trace.append({"tool": "👨‍👩‍👧 Détail proche",
                          "input": {"proche": p.get("contact_id", ""), "détail": p.get("detail", "")},
                          "why": "Info sur un proche → ajoutée à sa fiche pour enrichir l'incarnation "
                                 "lors des appels."})
        applied.append(item["id"])
    family_bot.proposals = [x for x in family_bot.proposals if x["id"] not in approved]
    return jsonify({"applied": applied, "count": len(applied), "trace": trace})


if __name__ == "__main__":
    # pas de reloader ni de debug : l'agent tient un sous-processus et une boucle asyncio
    app.run(port=5002, debug=False)
