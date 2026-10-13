"""Serveur MCP de MemorIA (stdio).

Outils de l'agent compagnon : lecture (souvenirs par RAG, appels du jour, rappels),
propositions soumises à la famille (souvenir, santé, alerte), journal des appels et
validation par la famille.

Chaque souvenir a un champ known_by : recall_context ne renvoie que ce que connaît le
proche incarné (data/active_contact.json). L'index ChromaDB est stocké dans ./chroma_index.
"""

import json
import os
from datetime import datetime, date

import chromadb
from sentence_transformers import SentenceTransformer
from mcp.server.fastmcp import FastMCP

BASE = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(BASE, "data")
INDEX_PATH = os.path.join(BASE, "chroma_index")
COLLECTION_NAME = "memories"
EMBED_MODEL = "all-MiniLM-L6-v2"

FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]
FR_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet",
             "août", "septembre", "octobre", "novembre", "décembre"]


# --- Lecture et écriture des fichiers JSON ---

def _read(name):
    with open(os.path.join(DATA, name), encoding="utf-8") as f:
        return json.load(f)


def _write(name, obj):
    with open(os.path.join(DATA, name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# --- Proche en ligne et filtrage des souvenirs par known_by ---

SHARED = "tous"  # souvenir connu de tous


def _active_contact():
    """Identifiant du proche incarné, ou None."""
    try:
        return _read("active_contact.json").get("contact_id")
    except (FileNotFoundError, ValueError):
        return None


def _kb_to_str(known_by):
    """Convertit known_by (liste, texte ou None) en texte séparé par des virgules."""
    if not known_by:
        return SHARED
    if isinstance(known_by, str):
        return known_by
    return ",".join(known_by)


def _contact_knows(known_by, contact_id):
    """Indique si le proche connaît le souvenir. Sans proche actif, aucun filtrage."""
    tokens = _kb_to_str(known_by).split(",")
    if SHARED in tokens or contact_id is None:
        return True
    return contact_id in tokens


# --- File de validation : la famille confirme chaque écriture en base ---

def _enqueue(kind, payload, summary):
    """Ajoute un élément (memory, health ou alert) à la file de validation."""
    queue = _read("pending_validations.json")
    vid = f"val-{datetime.now().strftime('%Y%m%d%H%M%S')}-{len(queue) + 1:03d}"
    queue.append({
        "id": vid,
        "type": kind,
        "status": "pending",
        "proposed_at": datetime.now().isoformat(timespec="seconds"),
        "source_contact": _active_contact(),   # proche en ligne au moment de la proposition
        "summary": summary,
        "payload": payload,
    })
    _write("pending_validations.json", queue)
    return vid


# --- Index des souvenirs (construit depuis memories.json au premier lancement) ---

embed_model = SentenceTransformer(EMBED_MODEL)
chroma = chromadb.PersistentClient(path=INDEX_PATH)


def _get_or_build_collection():
    col = chroma.get_or_create_collection(COLLECTION_NAME)
    if col.count() == 0:
        memories = _read("memories.json")
        col.add(
            ids=[m["id"] for m in memories],
            embeddings=embed_model.encode([m["text"] for m in memories]).tolist(),
            documents=[m["text"] for m in memories],
            metadatas=[{"category": m["category"], "known_by": _kb_to_str(m.get("known_by"))}
                       for m in memories],
        )
    return col


collection = _get_or_build_collection()

mcp = FastMCP("memoria-companion")


# --- Outils de lecture ---

@mcp.tool()
def recall_context(topic: str, k: int = 3) -> list:
    """Retrouve les k souvenirs de la vie de Jeanne les plus proches d'un sujet (famille, routine,
    goûts, événements passés...). À utiliser avant de répondre sur sa vie. Ne renvoie que les
    souvenirs que partage le proche que tu incarnes."""
    contact = _active_contact()
    q_vec = embed_model.encode(topic).tolist()
    # On récupère plus de résultats, puis on garde ceux que ce proche connaît.
    n = min(max(k * 4, k), collection.count())
    res = collection.query(query_embeddings=[q_vec], n_results=n)
    out = []
    for doc, meta in zip(res["documents"][0], res["metadatas"][0]):
        if _contact_knows(meta.get("known_by"), contact):
            out.append({"memory": doc, "category": meta.get("category", "")})
        if len(out) >= k:
            break
    return out


@mcp.tool()
def check_recent_conversations() -> dict:
    """Renvoie les appels précédents de la journée (heure, résumé, questions posées, signalements)
    pour rester cohérent d'un appel à l'autre et repérer les questions répétées."""
    log = _read("call_log.json")
    today = date.today().isoformat()
    todays = [c for c in log if c["timestamp"].startswith(today)]
    return {
        "calls_today": len(todays),
        "calls": [
            {
                "time": c["timestamp"][11:16],
                "summary": c["summary"],
                "questions_asked": c.get("questions_asked", []),
                "flags": c.get("flags", []),
            }
            for c in todays
        ],
        "all_questions_asked_today": [q for c in todays for q in c.get("questions_asked", [])],
    }


@mcp.tool()
def get_reminders() -> dict:
    """Renvoie la date et l'heure en français, les médicaments du jour et les prochains rendez-vous.
    À utiliser quand elle demande le jour, l'heure, ses médicaments ou son programme."""
    persona = _read("persona.json")
    now = datetime.now()
    day_name = FR_DAYS[now.weekday()]
    return {
        "maintenant": f"{day_name} {now.day} {FR_MONTHS[now.month - 1]} {now.year}, {now.strftime('%Hh%M')}",
        "medicaments": persona["sante"]["traitements"],
        "rendez_vous": persona["sante"]["rendez_vous"],
        "reperes": {
            "visite_de_thomas": "tous les samedis matin",
            "appel_d_anael": "tous les dimanches à 18h30",
            "kine": "tous les mardis à 10h",
        },
    }


# --- Outils d'écriture ---

@mcp.tool()
def update_memory(fact: str, category: str = "recent") -> dict:
    """Propose un fait NOUVEAU appris pendant la conversation (une visite prévue, un événement, une
    nouvelle habitude...) pour la base de souvenirs de Jeanne. Le fait n'entre PAS encore dans la
    base : il part dans la file de validation de la famille (approuvé ou rejeté après l'appel) et
    reste propre au proche que tu incarnes. Ne compte pas dessus pendant ce même appel.
    Catégories : famille, souvenirs, routine, gouts, sante, voisinage, recent."""
    vid = _enqueue("memory", {"text": fact, "category": category}, f"Souvenir : {fact}")
    return {"status": "pending_validation", "id": vid,
            "note": "En attente de validation par la famille avant d'entrer dans la mémoire."}


@mcp.tool()
def log_conversation(summary: str, mood: str, topics: list = [],
                     questions_asked: list = [], flags: list = []) -> dict:
    """Ajoute le résumé de fin d'appel au journal (résumé de 2 à 3 phrases en français, son humeur,
    les sujets abordés, ses questions et les signalements comme les répétitions ou la confusion).
    Le tableau de bord de la famille le lit. OBLIGATOIRE avant end_conversation()."""
    log = _read("call_log.json")
    log.append({
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "summary": summary,
        "mood": mood,
        "topics": topics or [],
        "questions_asked": questions_asked or [],
        "flags": flags or [],
    })
    _write("call_log.json", log)
    return {"status": "logged", "total_calls": len(log)}


@mcp.tool()
def update_health_profile(observation: str, category: str = "observation") -> dict:
    """Note une observation importante sur son état cognitif ou physique (nouvelle confusion,
    médicament oublié, agitation inhabituelle...). Elle est montrée en direct à la famille mais
    n'est écrite dans le profil de santé qu'après sa validation, après l'appel. Catégories :
    repetition, confusion_legere, confusion_forte, medicament, humeur, physique, observation."""
    vid = _enqueue("health", {"observation": observation, "category": category},
                   f"Observation santé : {observation}")
    return {"status": "pending_validation", "id": vid,
            "note": "Observation communiquée à la famille ; à valider avant d'entrer au dossier."}


@mcp.tool()
def alert_family(reason: str, urgency: str = "info", details: str = "") -> dict:
    """Alerte la famille de Jeanne (sa fille Anne et ses petits-fils Thomas et Anaël). urgency :
    'urgent' (chute, détresse, danger), 'warning' (confusion inhabituelle, médicament oublié,
    comportement anormal), 'info' (bon à savoir). L'alerte est transmise à la famille EN TEMPS
    RÉEL (elle s'affiche aussitôt sur son tableau de bord) ; elle n'est écrite dans le journal
    des alertes qu'après validation par la famille, après l'appel. Lève-la dès que nécessaire,
    sans attendre."""
    if urgency not in ("urgent", "warning", "info"):
        urgency = "info"
    vid = _enqueue("alert", {"reason": reason, "urgency": urgency, "details": details},
                   f"Alerte {urgency} : {reason}")
    return {"status": "alert_communicated", "urgency": urgency, "id": vid,
            "family_notified": ["Anne", "Thomas", "Anaël"] if urgency == "urgent" else ["Anne"],
            "note": "Alerte affichée en temps réel ; à confirmer par la famille après l'appel."}


# --- Ajout direct de souvenirs (assistant famille, hors appel) ---

@mcp.tool()
def ingest_memory(text: str, category: str = "recent", known_by: list = ["tous"]) -> dict:
    """Ajoute directement un souvenir à la base de Jeanne et l'indexe (utilisé par l'assistant
    d'enrichissement une fois l'information validée par la famille). `known_by` indique qui le
    partage (tout le monde par défaut). Non accessible à l'agent en appel, qui passe par la validation."""
    memories = _read("memories.json")
    new_id = f"mem-{len(memories) + 1:04d}"
    kb = known_by or [SHARED]
    memories.append({"id": new_id, "category": category, "text": text, "known_by": kb})
    _write("memories.json", memories)
    collection.add(
        ids=[new_id],
        embeddings=[embed_model.encode(text).tolist()],
        documents=[text],
        metadatas=[{"category": category, "known_by": _kb_to_str(kb)}],
    )
    return {"status": "ingested", "id": new_id, "total_memories": collection.count()}


# --- Validation par la famille ---

@mcp.tool()
def list_pending_validations() -> list:
    """Liste tout ce qui a été proposé pendant les appels (souvenirs, notes de santé, alertes) et
    attend encore la décision de la famille."""
    return [v for v in _read("pending_validations.json") if v["status"] == "pending"]


@mcp.tool()
def approve_validation(validation_id: str) -> dict:
    """Approuve un élément en attente (décision de la famille) et l'écrit dans la base selon son
    type : un souvenir est indexé (attribué au proche qui l'a proposé), une note de santé est
    ajoutée au profil, une alerte est écrite dans le journal des alertes."""
    queue = _read("pending_validations.json")
    item = next((v for v in queue if v["id"] == validation_id and v["status"] == "pending"), None)
    if item is None:
        return {"status": "not_found", "id": validation_id}

    p = item["payload"]
    result = {}
    if item["type"] == "memory":
        # Le souvenir est attribué au proche en ligne lors de la proposition.
        known_by = [item["source_contact"]] if item.get("source_contact") else [SHARED]
        memories = _read("memories.json")
        new_id = f"mem-{len(memories) + 1:04d}"
        memories.append({"id": new_id, "category": p["category"],
                         "text": p["text"], "known_by": known_by})
        _write("memories.json", memories)
        collection.add(
            ids=[new_id],
            embeddings=[embed_model.encode(p["text"]).tolist()],
            documents=[p["text"]],
            metadatas=[{"category": p["category"], "known_by": _kb_to_str(known_by)}],
        )
        result = {"memory_id": new_id, "known_by": known_by}
    elif item["type"] == "health":
        profile = _read("health_profile.json")
        profile["observations"].append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "observation": p["observation"], "category": p["category"],
        })
        _write("health_profile.json", profile)
        result = {"total_observations": len(profile["observations"])}
    elif item["type"] == "alert":
        alerts = _read("alerts.json")
        alerts.append({
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "urgency": p["urgency"], "reason": p["reason"],
            "details": p.get("details", ""), "seen": False,
        })
        _write("alerts.json", alerts)
        result = {"total_alerts": len(alerts)}

    item["status"] = "approved"
    item["decided_at"] = datetime.now().isoformat(timespec="seconds")
    item["result"] = result
    _write("pending_validations.json", queue)
    return {"status": "approved", "id": validation_id, "type": item["type"], **result}


@mcp.tool()
def reject_validation(validation_id: str) -> dict:
    """Rejette un élément en attente (décision de la famille) : il n'entre jamais dans la base."""
    queue = _read("pending_validations.json")
    item = next((v for v in queue if v["id"] == validation_id and v["status"] == "pending"), None)
    if item is None:
        return {"status": "not_found", "id": validation_id}
    item["status"] = "rejected"
    item["decided_at"] = datetime.now().isoformat(timespec="seconds")
    _write("pending_validations.json", queue)
    return {"status": "rejected", "id": validation_id, "type": item["type"]}


@mcp.tool()
def end_conversation() -> dict:
    """Marque l'appel comme terminé, juste après log_conversation(). L'application clôt la session."""
    return {"status": "call_ended"}


if __name__ == "__main__":
    mcp.run()  # transport stdio
