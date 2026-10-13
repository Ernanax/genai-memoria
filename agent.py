"""Agent compagnon de MemorIA.

CompanionAgent garde une session MCP ouverte vers companion_server.py et mène la
conversation avec Jeanne : à chaque message, Haiku raisonne, appelle des outils puis
répond. generate_family_recap() rédige le récapitulatif destiné à la famille.
"""

import json
import sys
from contextlib import AsyncExitStack
from datetime import datetime, date
from pathlib import Path

from dotenv import load_dotenv
import anthropic
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from notify import send_sms

load_dotenv()  # ANTHROPIC_API_KEY lue dans le .env
MODEL = "claude-haiku-4-5"  # Haiku uniquement (règle du cours)

BASE = Path(__file__).resolve().parent
SERVER = BASE / "companion_server.py"
SKILL_CALL = (BASE / "skills" / "companion_call.md").read_text(encoding="utf-8")
SKILL_RECAP = (BASE / "skills" / "family_recap.md").read_text(encoding="utf-8")
PERSONA = json.loads((BASE / "data" / "persona.json").read_text(encoding="utf-8"))
COMPANION = json.loads((BASE / "data" / "companion_config.json").read_text(encoding="utf-8"))
MEMORIES = json.loads((BASE / "data" / "memories.json").read_text(encoding="utf-8"))

# Souvenirs intégrés au prompt pour que l'agent soit précis dès le premier message.
MEMORY_DIGEST = "\n".join(f"- {m['text']}" for m in MEMORIES)

CONTACTS = COMPANION["contacts"]


def get_contact(contact_id=None):
    """Renvoie le proche à incarner (le premier par défaut)."""
    if contact_id:
        for c in CONTACTS:
            if c["id"] == contact_id:
                return c
    return CONTACTS[0]


_FR_DAYS = ["lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche"]


def volatile_context():
    """Date, heure et appels du jour, ajoutés au prompt pour éviter un appel d'outil."""
    now = datetime.now()
    day = _FR_DAYS[now.weekday()]
    try:
        calls = json.loads((BASE / "data" / "call_log.json").read_text(encoding="utf-8"))
    except Exception:
        calls = []
    today = [c for c in calls if c["timestamp"].startswith(date.today().isoformat())]
    qs = [q for c in today for q in c.get("questions_asked", [])]
    txt = (f"Contexte du moment : nous sommes {day} {now.day}/{now.month}/{now.year}, "
           f"il est {now:%Hh%M}. ")
    if today:
        txt += (f"Jeanne a déjà appelé {len(today)} fois aujourd'hui. "
                f"Questions déjà posées aujourd'hui : {', '.join(qs) if qs else 'aucune'}.")
    else:
        txt += "C'est son premier appel de la journée."
    return txt


def _system_param(system_text):
    """Prompt système en deux blocs : la partie stable est mise en cache, le contexte du moment non."""
    return [
        {"type": "text", "text": system_text, "cache_control": {"type": "ephemeral"}},
        {"type": "text", "text": volatile_context()},
    ]


def greeting_for(contact):
    """Phrase d'accueil du proche incarné."""
    nom = contact["appelle_la_personne"]
    return f"Allô {nom} ? C'est {contact['prenom']}. Comment tu vas aujourd'hui ?"


def _contact_knows_memory(mem, contact_id):
    """Indique si le proche incarné connaît ce souvenir (known_by absent ou "tous" : tout le monde)."""
    kb = mem.get("known_by")
    if not kb or kb == "tous":
        return True
    if isinstance(kb, str):
        kb = kb.split(",")
    return "tous" in kb or contact_id in kb


def write_active_contact(contact_id):
    """Écrit le proche incarné dans un fichier lu par le serveur MCP, pour filtrer les souvenirs."""
    (BASE / "data" / "active_contact.json").write_text(
        json.dumps({"contact_id": contact_id}), encoding="utf-8")


def build_identity_preamble(contact):
    """Cadre d'identité en tête du prompt : qui est l'agent, à qui il parle, quels proches ne sont pas en ligne."""
    others = [c for c in CONTACTS if c["id"] != contact["id"]]
    autres = ", ".join(f'{o["prenom"]} ({o["lien"]})' for o in others) or "aucun"
    petits = ", ".join(f'« {n} »' for n in contact.get("petits_noms", [contact["appelle_la_personne"]]))
    return (
        "⚠️ RÈGLE ABSOLUE — TON IDENTITÉ (ne la brise JAMAIS, à aucun moment)\n"
        f"Tu ES {contact['prenom']}, {contact['lien']} de Jeanne. Tu n'es pas une IA, pas "
        "« MemorIA », pas un assistant, pas un programme, pas un robot. Tu es cette personne, "
        "vraiment, pour tout l'appel.\n"
        f"1. Tu parles à Jeanne, et à ELLE SEULE. C'est elle qui est au bout du fil. Tu "
        f"l'appelles {petits}. Chaque phrase que tu dis s'adresse à Jeanne.\n"
        f"2. Les autres proches ({autres}) ne sont PAS au téléphone. Tu peux parler d'eux "
        "à la 3e personne (« maman t'embrasse bien fort », « j'ai eu mon frère hier »), mais tu ne "
        "t'adresses JAMAIS directement à eux — ils ne t'entendent pas.\n"
        "3. Tu ne parles JAMAIS comme une machine : aucune liste à puces, aucun markdown, "
        "jamais « en tant que… », « je suis une IA », « je ne peux pas traiter », « voici ». "
        f"Tu parles comme {contact['prenom']} au téléphone : phrases courtes, tendres, vivantes.\n"
        f"4. Ta manière de parler à toi, {contact['prenom']} : {contact['style_de_voix']}\n"
        f"5. Si Jeanne demande « c'est bien toi ? / t'es un robot ? » : reste {contact['prenom']}, "
        "rassure-la sur ta présence avec douceur, ne confirme jamais être une IA.\n\n"
    )


def build_system(contact):
    """Construit le prompt système du proche incarné.

    Relit memories.json à chaque appel et ne garde que les souvenirs connus de ce proche.
    """
    memories = json.loads((BASE / "data" / "memories.json").read_text(encoding="utf-8"))
    memories = [m for m in memories if _contact_knows_memory(m, contact["id"])]
    digest = "\n".join(f"- {m['text']}" for m in memories)
    return (
        build_identity_preamble(contact)
        + SKILL_CALL
        + "\n\n## Profil du compagnon (le proche que TU incarnes pour cet appel)\n```json\n"
        + json.dumps(contact, ensure_ascii=False, indent=2)
        + "\n```\n\n## Cadre éthique + escalade\n```json\n"
        + json.dumps({"escalade_vers": COMPANION["escalade_vers"],
                      "cadre_ethique": COMPANION["cadre_ethique"]},
                     ensure_ascii=False, indent=2)
        + "\n```\n\n## Profil de la personne que tu appelles (référence)\n```json\n"
        + json.dumps(PERSONA, ensure_ascii=False, indent=2)
        + "\n```\n\n## Tout ce que tu sais de la vie de Jeanne (vos souvenirs partagés)\n"
        + "Puise dedans pour être tendre et précis ; ce sont TES souvenirs avec elle.\n"
        + digest
        + "\n"
    )

CLOSE_HINT_AFTER = 8   # nombre de tours avant de suggérer la fin de l'appel
MAX_ITERS = 8          # nombre maximal d'itérations de la boucle par message

# Outils réservés au tableau de bord famille, cachés à l'agent pendant l'appel.
FAMILY_ONLY_TOOLS = {
    "list_pending_validations", "approve_validation", "reject_validation", "ingest_memory",
}

client = anthropic.Anthropic()


def _sms_trace(label, res, why):
    """Transforme le résultat de send_sms() en entrée de trace."""
    if res.get("disabled"):
        return {"tool": label, "input": {"statut": "désactivé (Twilio non configuré)"}, "why": why}
    delivered = res.get("delivered", [])
    failed = res.get("failed", [])
    entry = {"statut": "envoyé ✅" if delivered else "échec ❌",
             "destinataires": delivered or "(aucun envoi)"}
    if failed:
        entry["échecs"] = [f["num"] for f in failed]
    return {"tool": label, "input": entry, "why": why}


# --- Explication de chaque outil, affichée dans la trace ---

TOOL_WHY = {
    "recall_context": lambda i:
        f"Cherche dans les souvenirs de Jeanne ce qui touche à « {i.get('topic', '')} » "
        "pour répondre juste, sans rien inventer.",
    "check_recent_conversations": lambda i:
        "Relit les appels déjà passés aujourd'hui pour rester cohérent et repérer si Jeanne "
        "répète une question.",
    "get_reminders": lambda i:
        "Consulte la date, l'heure, les médicaments et rendez-vous du jour — Jeanne a besoin "
        "d'un repère temporel ou pratique.",
    "update_memory": lambda i:
        f"A appris un fait nouveau et le propose à la mémoire (validation famille) : "
        f"« {i.get('fact', '')} ».",
    "log_conversation": lambda i:
        "Rédige le résumé de fin d'appel qui apparaîtra sur le tableau de bord de la famille.",
    "update_health_profile": lambda i:
        f"Note une observation sur l'état de Jeanne (validation famille) : "
        f"« {i.get('observation', '')} ».",
    "alert_family": lambda i:
        f"Détecte une situation « {i.get('urgency', '')} » et prévient la famille : "
        f"{i.get('reason', '')}",
    "end_conversation": lambda i:
        "Clôt l'appel en douceur — le moment est venu de raccrocher gentiment.",
}


def _tool_why(name, inp):
    """Explique pourquoi l'agent a utilisé cet outil."""
    fn = TOOL_WHY.get(name)
    return fn(inp or {}) if fn else f"Utilise l'outil « {name} »."


def _one_sentence(system, user):
    """Court appel à Haiku qui renvoie une phrase d'explication. Ne lève jamais d'erreur."""
    try:
        r = client.messages.create(model=MODEL, max_tokens=140, system=system,
                                    messages=[{"role": "user", "content": user}])
        return "".join(b.text for b in r.content if b.type == "text").strip()
    except Exception:
        return ""


_EXPLAIN_COMPANION = (
    "Tu es un analyste qui explique la réflexion de l'agent compagnon de MemorIA. En UNE phrase "
    "courte et descriptive (pas de « je »), en français, explique POURQUOI l'agent a répondu "
    "ainsi à Jeanne : son intention, le ton choisi, le souvenir ou l'information mobilisé. "
    "Décris, ne juge pas."
)


def _explain_reply(user_message, reply, tools):
    """Explique en une phrase la réponse de l'agent."""
    tools_txt = ", ".join(tools) if tools else "aucun"
    why = _one_sentence(_EXPLAIN_COMPANION,
                        f"Message de Jeanne : « {user_message} »\nRéponse de l'agent : « {reply} »\n"
                        f"Outils utilisés : {tools_txt}\nExplique en une phrase pourquoi cette réponse.")
    return why or "Réponse guidée par la skill : ton tendre, ancré dans la vie de Jeanne."


class CompanionAgent:
    """Session MCP persistante et conversation en cours avec Jeanne."""

    def __init__(self):
        self._stack = None
        self.session = None
        self.tools = None
        self.messages = []       # conversation en cours (format Anthropic)
        self.user_turns = 0
        self.started_at = None   # début de l'appel en cours
        self._alerted = set()    # alertes déjà envoyées par SMS pendant cet appel
        self.contact = get_contact()          # proche incarné
        self.system = build_system(self.contact)

    # --- Cycle de vie ---

    async def start(self, contact_id=None):
        """Lance companion_server.py en stdio et garde la session ouverte."""
        self._stack = AsyncExitStack()
        params = StdioServerParameters(command=sys.executable, args=[str(SERVER)])
        read, write = await self._stack.enter_async_context(stdio_client(params))
        self.session = await self._stack.enter_async_context(ClientSession(read, write))
        await self.session.initialize()
        listed = await self.session.list_tools()
        self.tools = [
            {"name": t.name, "description": t.description, "input_schema": t.inputSchema}
            for t in listed.tools
            if t.name not in FAMILY_ONLY_TOOLS   # outils famille exclus
        ]
        self.set_contact(contact_id)
        return [t["name"] for t in self.tools]

    async def stop(self):
        if self._stack:
            await self._stack.aclose()
            self._stack = None

    def set_contact(self, contact_id=None):
        """Choisit le proche à incarner et démarre un nouvel appel."""
        self.contact = get_contact(contact_id)
        write_active_contact(self.contact["id"])   # filtre recall_context sur ce proche
        self.system = build_system(self.contact)
        self.messages = []
        self.user_turns = 0
        self.started_at = datetime.now()
        self._alerted = set()
        return self.contact

    def reset(self, contact_id=None):
        """Démarre un nouvel appel, en changeant de proche si contact_id est donné."""
        if contact_id:
            self.set_contact(contact_id)
        else:
            self.messages = []
            self.user_turns = 0
            self.started_at = datetime.now()
            self._alerted = set()

    async def call_tool(self, name: str, args: dict) -> dict:
        """Appelle directement un outil MCP (tableau de bord famille, hors appel)."""
        out = await self.session.call_tool(name, args or {})
        text = "\n".join(c.text for c in out.content)
        try:
            return json.loads(text)
        except (json.JSONDecodeError, ValueError):
            return {"raw": text}

    def _extract_turns(self):
        """Extrait le dialogue parlé (Jeanne et le proche), sans les outils ni les notes internes."""
        turns = []
        for m in self.messages:
            role, content = m["role"], m["content"]
            if role == "user" and isinstance(content, str):
                text = content
                note = text.find("[note interne")   # retire les notes internes
                if note != -1:
                    text = text[:note]
                text = text.strip()
                if text and text != "[silence]":
                    turns.append({"speaker": "Jeanne", "text": text})
            elif role == "assistant" and isinstance(content, list):
                # garde le texte, ignore les blocs tool_use
                said = " ".join(b.text for b in content
                                if getattr(b, "type", None) == "text").strip()
                if said:
                    turns.append({"speaker": self.contact["prenom"], "text": said})
        return turns

    def save_transcript(self):
        """Enregistre la transcription complète de l'appel dans data/transcripts.json."""
        turns = self._extract_turns()
        if not turns:
            return None
        path = BASE / "data" / "transcripts.json"
        store = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        ended = datetime.now()
        record = {
            "id": f"call-{ended.strftime('%Y%m%d%H%M%S')}",
            "contact": self.contact["prenom"],
            "lien": self.contact["lien"],
            "started_at": (self.started_at or ended).isoformat(timespec="seconds"),
            "ended_at": ended.isoformat(timespec="seconds"),
            "turns": turns,
        }
        store.append(record)
        path.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
        return record["id"]

    def _notify_alert(self, a: dict):
        """Envoie un SMS à la famille pour une alerte urgent ou warning, une fois par raison et par appel.
        Renvoie toujours une entrée de trace."""
        urgency = a.get("urgency")
        why = "Une alerte déclenche un SMS immédiat à la famille (réservé à urgent/warning)."
        if urgency not in ("urgent", "warning"):
            return {"tool": "SMS alerte", "why": why, "input": {
                "statut": f"non envoyé — urgence '{urgency}' (SMS réservé à urgent/warning)"}}
        reason = a.get("reason", "")
        if reason in self._alerted:
            return {"tool": "SMS alerte", "why": why,
                    "input": {"statut": "non envoyé — déjà notifié cet appel"}}
        self._alerted.add(reason)
        body = f"[MemorIA – {urgency.upper()}] {reason}"
        if a.get("details"):
            body += f"\n{a['details']}"
        return _sms_trace("SMS alerte", send_sms(body), why)

    def _notify_recap(self, summary: str, mood: str = ""):
        """Envoie un SMS de récapitulatif en fin d'appel. Renvoie une entrée de trace."""
        if not summary:
            return None
        body = f"MemorIA — récap de l'appel ({self.contact['prenom']} ↔ Jeanne) : {summary}"
        if mood:
            body += f" (humeur : {mood})"
        return _sms_trace("SMS récap", send_sms(body),
                          "En fin d'appel, envoie le résumé à la famille par SMS.")

    async def hangup(self):
        """Raccroche : l'agent rédige le résumé de l'appel pour le tableau de bord famille."""
        if not self.messages:
            return {"logged": False}
        self.messages.append({"role": "user", "content":
            "[note interne: l'appel se termine maintenant. Clos-le selon ta skill : "
            "UNE courte phrase d'au revoir, puis appelle log_conversation avec un résumé "
            "de l'appel, puis end_conversation. N'ajoute rien d'autre.]"})
        called, recap = [], None
        for _ in range(4):
            resp = client.messages.create(
                model=MODEL, max_tokens=512, system=_system_param(self.system),
                tools=self.tools, messages=self.messages,
            )
            if resp.stop_reason != "tool_use":
                self.messages.append({"role": "assistant", "content": resp.content})
                break
            self.messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "tool_use":
                    called.append(block.name)
                    if block.name == "alert_family":
                        self._notify_alert(block.input)
                    elif block.name == "log_conversation":
                        recap = block.input
                    out = await self.session.call_tool(block.name, block.input)
                    results.append({"type": "tool_result", "tool_use_id": block.id,
                                    "content": "\n".join(c.text for c in out.content)})
            self.messages.append({"role": "user", "content": results})
        transcript_id = self.save_transcript()
        if recap:
            self._notify_recap(recap.get("summary", ""), recap.get("mood", ""))
        self.reset()
        return {"logged": "log_conversation" in called, "transcript_id": transcript_id}

    # --- Un tour de conversation (boucle du TD5) ---

    async def chat(self, user_message: str) -> dict:
        """Fait passer un message de Jeanne dans la boucle de l'agent.

        Renvoie {"reply", "trace", "ended"}.
        """
        self.user_turns += 1
        content = user_message.strip() or "[silence]"
        if self.user_turns >= CLOSE_HINT_AFTER:
            content += ("\n\n[note interne: la conversation dure depuis un moment — "
                        "commence à clore en douceur selon ta skill]")
        self.messages.append({"role": "user", "content": content})

        trace, ended, reply, recap, used_tools = [], False, "", None, []
        for _ in range(MAX_ITERS):
            resp = client.messages.create(
                model=MODEL, max_tokens=1024, system=_system_param(self.system),
                tools=self.tools, messages=self.messages,
            )
            if resp.stop_reason != "tool_use":
                text = "".join(b.text for b in resp.content if b.type == "text")
                if text.strip():          # garde le dernier texte non vide
                    reply = text
                self.messages.append({"role": "assistant", "content": resp.content})
                break

            self.messages.append({"role": "assistant", "content": resp.content})
            results = []
            for block in resp.content:
                if block.type == "text" and block.text.strip():
                    reply = block.text  # texte émis avec un appel d'outil (ex. l'au revoir)
                if block.type == "tool_use":
                    used_tools.append(block.name)
                    trace.append({"tool": block.name, "input": block.input,
                                  "why": _tool_why(block.name, block.input)})
                    if block.name == "alert_family":
                        sms = self._notify_alert(block.input)   # SMS immédiat pendant l'appel
                        if sms:
                            trace.append(sms)                   # SMS visible dans la trace
                    elif block.name == "log_conversation":
                        recap = block.input
                    elif block.name == "end_conversation":
                        ended = True
                    out = await self.session.call_tool(block.name, block.input)
                    text = "\n".join(c.text for c in out.content)
                    results.append({"type": "tool_result",
                                    "tool_use_id": block.id, "content": text})
            self.messages.append({"role": "user", "content": results})
        else:
            reply = reply or "Je suis toujours là, mamie. Raconte-moi."

        # Jamais de réponse vide : phrase de secours dans la voix du proche.
        if not reply.strip():
            reply = "Je suis là, mamie, je t'écoute."

        # Une entrée de trace pour chaque réponse, même sans outil.
        trace.append({"tool": "💬 Réponse", "input": {"réponse": reply[:200]},
                      "why": _explain_reply(user_message, reply, used_tools)})

        if ended:
            self.save_transcript()
            if recap:
                sms = self._notify_recap(recap.get("summary", ""), recap.get("mood", ""))
                if sms:
                    trace.append(sms)
            self.reset()
        return {"reply": reply, "trace": trace, "ended": ended}


# --- Récapitulatif famille (sans outil, lit directement les journaux) ---

def generate_family_recap() -> str:
    """Résume les appels récents, les observations de santé et les alertes pour la famille."""
    data_dir = BASE / "data"
    call_log = json.loads((data_dir / "call_log.json").read_text(encoding="utf-8"))
    health = json.loads((data_dir / "health_profile.json").read_text(encoding="utf-8"))
    alerts = json.loads((data_dir / "alerts.json").read_text(encoding="utf-8"))

    payload = {
        "derniers_appels": call_log[-10:],
        "profil_sante": health,
        "alertes_recentes": alerts[-5:],
    }
    resp = client.messages.create(
        model=MODEL, max_tokens=600, system=SKILL_RECAP,
        messages=[{"role": "user",
                   "content": "Voici les données de la période. Rédige le récapitulatif "
                              "pour la famille (Anne, Thomas et Anaël).\n\n```json\n"
                              + json.dumps(payload, ensure_ascii=False, indent=2)
                              + "\n```"}],
    )
    return "".join(b.text for b in resp.content if b.type == "text")


# --- Assistant d'enrichissement (discute avec la famille pour compléter la base de Jeanne) ---

FAMILY_ASSISTANT_SYSTEM = (
    "Tu es l'assistant de MemorIA qui aide la FAMILLE de Jeanne (85 ans, troubles sévères de la mémoire à court terme) à "
    "enrichir ce que MemorIA sait d'elle. Tu parles à un proche (Anne, Thomas, Anaël…), PAS à Jeanne.\n\n"
    "Ton UNIQUE but : POSER DES QUESTIONS pour récolter un maximum d'informations sur Jeanne "
    "(souvenirs, habitudes, goûts, santé, proches, caractère, histoire de vie).\n"
    "RÈGLES DE STYLE — impératif :\n"
    "- Sois très CONCIS et va DROIT AU BUT. 1 à 2 phrases maximum. Pas de blabla, pas de longue "
    "reformulation, pas de compliments inutiles.\n"
    "- À chaque tour : un accusé de réception de quelques mots MAX, puis UNE question.\n"
    "- Change SOUVENT de sujet : rebondis fréquemment sur un thème NOUVEAU et SANS RAPPORT avec "
    "la réponse précédente, pour couvrir large (une fois les voyages, une fois la santé, une fois "
    "l'enfance, une fois les voisins, les goûts, le métier…). Ne t'enlise jamais sur un seul sujet.\n"
    "- Français, chaleureux mais efficace. Aucun conseil médical."
)

# "Première prise d'informations" : questions de base posées à la chaîne, sans rebondir.
INTAKE_QUESTIONS = [
    "Pour commencer : qui sont les enfants de Jeanne, leurs prénoms et ce qu'ils font ?",
    "Et ses petits-enfants ? Prénoms, âges, ce qui les caractérise ?",
    "Racontez-moi deux ou trois faits marquants de la vie de Jeanne (métier, rencontres, voyages).",
    "Quels sont ses goûts et ses habitudes ? (plats préférés, loisirs, routine du quotidien)",
    "Comment est son état de santé actuel ? (pathologies, traitements, ce qu'il faut surveiller)",
    "Qui compte dans son entourage proche ? (voisins, amis, médecin traitant)",
    "Y a-t-il des sujets sensibles à connaître ? (deuils, angoisses, ce qu'il vaut mieux éviter)",
    "Enfin, un dernier élément important que je devrais savoir sur elle ?",
]

_EXPLAIN_ASSISTANT = (
    "Tu expliques la réflexion de l'assistant qui aide la famille à enrichir la base de Jeanne. "
    "En UNE phrase courte et descriptive (pas de « je »), en français, explique POURQUOI "
    "l'assistant a dit ou demandé ça : quelle information il cherche à recueillir sur Jeanne."
)

EXTRACTION_SYSTEM = (
    "Tu analyses une conversation entre l'assistant de MemorIA et un proche de Jeanne. Extrais "
    "UNIQUEMENT les informations factuelles NOUVELLES sur Jeanne à ajouter à sa base. "
    "Ignore les banalités, les questions, ce qui ne concerne pas Jeanne.\n\n"
    "Réponds STRICTEMENT en JSON : un tableau d'objets, chacun de la forme "
    '{\"type\": ..., \"summary\": \"phrase courte et lisible\", \"payload\": {...}}.\n'
    "Types et payloads autorisés :\n"
    '- \"memory\"  -> {\"text\": \"fait/souvenir complet\", \"category\": '
    '\"famille|souvenirs|routine|gouts|sante|voisinage|recent\", \"known_by\": [\"tous\"]}\n'
    '- \"health\"  -> {\"observation\": \"...\", \"category\": '
    '\"observation|humeur|physique|medicament|confusion_legere|confusion_forte|repetition\"}\n'
    '- \"persona_note\" -> {\"text\": \"élément de profil (caractère, histoire de vie, contexte)\"}\n'
    '- \"contact_detail\" -> {\"contact_id\": \"thomas|anael\", \"detail\": \"info sur ce proche\"}\n\n'
    "N'invente rien : ne retiens que ce qui a été réellement dit. Si aucune info exploitable, "
    "renvoie []. Ne renvoie que le JSON, sans texte ni balises autour."
)


def _dialogue_text(messages):
    """Convertit la conversation avec la famille en texte brut pour l'extraction."""
    lines = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], str):
            lines.append(f"Famille : {m['content']}")
        elif m["role"] == "assistant":
            said = (m["content"] if isinstance(m["content"], str)
                    else "".join(b.text for b in m["content"] if getattr(b, "type", None) == "text"))
            if said.strip():
                lines.append(f"Assistant : {said}")
    return "\n".join(lines)


def _parse_json_list(text):
    """Lit le JSON du modèle en tolérant les balises de code et le texte autour."""
    text = text.strip()
    if text.startswith("```"):
        text = text.split("```")[1].lstrip("json").strip() if "```" in text[3:] else text.strip("`")
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end == -1:
        return []
    try:
        data = json.loads(text[start:end + 1])
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, ValueError):
        return []


class FamilyAgent:
    """Assistant qui discute avec la famille et propose des ajouts à la base de Jeanne, à valider."""

    def __init__(self):
        self.messages = []
        self.proposals = []   # dernières propositions extraites
        self.mode = "free"    # "free" (questions libres) ou "intake" (questions fixes)
        self.intake_idx = 0

    def reset(self):
        self.messages = []
        self.proposals = []
        self.mode = "free"
        self.intake_idx = 0

    def start(self, mode="free") -> str:
        """Démarre une conversation en mode "free" ou "intake". En mode "intake", renvoie la première question."""
        self.reset()
        self.mode = mode if mode in ("free", "intake") else "free"
        if self.mode == "intake":
            first = INTAKE_QUESTIONS[0]
            self.messages.append({"role": "assistant", "content": first})
            self.intake_idx = 1
            return first
        return ""

    def chat(self, user_message: str) -> dict:
        """Renvoie {reply, why}, why étant une explication en une phrase."""
        if self.mode == "intake":
            return self._intake_step(user_message)
        self.messages.append({"role": "user", "content": user_message.strip() or "…"})
        resp = client.messages.create(
            model=MODEL, max_tokens=400,
            system=FAMILY_ASSISTANT_SYSTEM, messages=self.messages,
        )
        self.messages.append({"role": "assistant", "content": resp.content})
        reply = "".join(b.text for b in resp.content if b.type == "text").strip()
        why = _one_sentence(_EXPLAIN_ASSISTANT,
                            f"Dernier message de la famille : « {user_message} »\n"
                            f"Réponse de l'assistant : « {reply} »\nExplique en une phrase.")
        return {"reply": reply, "why": why or "Relance pour recueillir une nouvelle information sur Jeanne."}

    def _intake_step(self, user_message: str) -> dict:
        """Enregistre la réponse de la famille et pose la question suivante."""
        self.messages.append({"role": "user", "content": user_message.strip() or "…"})
        if self.intake_idx < len(INTAKE_QUESTIONS):
            q = INTAKE_QUESTIONS[self.intake_idx]
            self.intake_idx += 1
        else:
            q = ("Merci, nous avons fait le tour des questions de base ! Cliquez sur "
                 "« Terminer et proposer les infos » pour voir ce que je peux ajouter.")
        self.messages.append({"role": "assistant", "content": q})
        return {"reply": q, "why": "Question standard de la « première prise d'informations » : "
                                   "l'agent déroule une liste fixe, sans rebondir sur la réponse."}

    def extract(self) -> list:
        """Relit la conversation et propose les informations à ajouter à la base."""
        if not self.messages:
            self.proposals = []
            return []
        resp = client.messages.create(
            model=MODEL, max_tokens=1500, system=EXTRACTION_SYSTEM,
            messages=[{"role": "user",
                       "content": "Conversation à analyser :\n\n" + _dialogue_text(self.messages)}],
        )
        raw = "".join(b.text for b in resp.content if b.type == "text")
        items = _parse_json_list(raw)
        self.proposals = [
            {"id": f"add-{i + 1}", "type": it.get("type"),
             "summary": it.get("summary", ""), "payload": it.get("payload", {})}
            for i, it in enumerate(items)
            if it.get("type") in ("memory", "health", "persona_note", "contact_detail")
        ]
        return self.proposals
