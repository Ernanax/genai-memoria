# 🌸 MemorIA, un compagnon mémoire qui répond avec la voix d'un proche

**Projet de hackathon · cours d'IA générative, M2 IASD (12 h, binôme)**

Les personnes atteintes de pertes de mémoire sévères appellent souvent leur famille en boucle, oublient qu'elles viennent d'appeler et se sentent seules. **MemorIA** répond à l'appel quand le proche qu'elles cherchent à joindre ne peut pas décrocher. Il répond **à la place de ce proche**, avec sa voix clonée, sa façon de parler et les souvenirs qu'il partage avec la personne. En parallèle, il tient la famille informée et **transmet tout danger réel à de vrais proches**.

> MemorIA ne remplace pas la famille. Il lui donne une voix quand elle ne peut pas répondre, et la prévient quand ça compte vraiment.

**Ce projet est une preuve de concept et le dépôt n'est pas exécutable.** Il reposait sur des accès API (Anthropic, ElevenLabs, Twilio) qui ne sont plus disponibles. Le code est publié pour être lu. Les données de `data/` (persona, souvenirs, appels) sont fictives.

## Cas d'usage

**Jeanne, 85 ans**, vit seule et reste autonome physiquement, mais sa mémoire à court terme est presque éteinte. Son mari est décédé au début de l'année. Elle l'oublie et appelle ses petits-fils presque chaque jour, inquiète, pour demander où il est. Lui réannoncer le décès à chaque appel la ferait revivre son deuil à chaque fois.

Quand Jeanne appelle, MemorIA répond comme l'un de ses petits-fils. Il parle à la première personne, utilise les petits noms qu'il lui donne et évoque des souvenirs qu'ils partagent. Il l'apaise, oriente doucement la conversation vers autre chose, et s'assure que la famille sait comment elle va.

## Fonctionnement

1. Jeanne appelle, par téléphone ou depuis l'interface web.
2. L'agent compagnon (`agent.py`, Claude Haiku) reçoit sa phrase et lance une boucle : raisonner, appeler des outils, observer le résultat, recommencer si besoin.
3. Les outils sont fournis par un serveur MCP (`companion_server.py`) : recherche dans les souvenirs (RAG avec ChromaDB), fichiers JSON et file de validation.
4. La réponse est lue avec la voix clonée du proche (ElevenLabs, `tts.py`), au téléphone via Twilio (`telephony.py`).
5. La famille suit tout sur son tableau de bord (`/family`) et reçoit les résumés et les alertes par SMS (`notify.py`).

Les outils de l'agent pendant un appel :

| Outil | Rôle |
|---|---|
| `recall_context(topic)` | Recherche RAG dans les souvenirs de Jeanne (ChromaDB et `all-MiniLM-L6-v2`), limitée à ce que connaît le proche incarné |
| `check_recent_conversations()` | Relit les appels du jour pour rester cohérent et repérer les questions répétées |
| `get_reminders()` | Visites des soignants et rendez-vous |
| `update_memory(fact)` | Propose un nouveau souvenir à la famille |
| `update_health_profile(observation)` | Propose une observation de santé à la famille |
| `alert_family(reason, urgency)` | Affiche une alerte sur le tableau de bord en temps réel et l'envoie par SMS |
| `log_conversation(summary, mood, …)` | Résumé de fin d'appel pour le tableau de bord et le SMS |
| `end_conversation()` | Termine l'appel en douceur |

### Principaux choix de conception

- **L'agent joue un membre de la famille, pas un assistant.** Chaque proche (ici deux petits-fils) a un profil dans `data/companion_config.json` : lien de parenté, ton, petits noms et voix. L'agent incarne l'un d'eux pendant tout l'appel.
- **Validation humaine de chaque écriture.** Rien de ce que l'agent soulève pendant un appel (nouveau souvenir, note de santé, alerte) n'est écrit directement dans la base. L'élément est montré en direct à la famille et n'est enregistré qu'après son **approbation** depuis le tableau de bord. Les alertes urgentes parviennent immédiatement à la famille.
- **Des souvenirs propres à chaque proche.** Chaque souvenir a un champ `known_by`. L'agent n'évoque que ce que connaît le proche qu'il incarne : ce que Jeanne a confié à un petit-fils ne ressort pas quand l'agent joue l'autre.
- **Un comportement défini dans des skills.** Les règles de conversation sont dans [`skills/companion_call.md`](skills/companion_call.md) : ne jamais la contredire brutalement, ne jamais réannoncer le décès, quand lever une alerte, comment relancer après un silence, comment terminer un appel. Le récapitulatif périodique pour la famille suit [`skills/family_recap.md`](skills/family_recap.md).
- **Un assistant pour enrichir la base.** Sur le tableau de bord, la famille peut discuter librement ou répondre à un questionnaire guidé. L'assistant extrait ensuite des informations structurées et les propose à la validation.
- **Mise en cache du prompt.** La grande partie stable du prompt système (skill, persona, souvenirs) est mise en cache. Seul le contexte du moment (date, heure, appels du jour) change à chaque tour.

## Fonctionnalités

- **Deux interfaces web** (Flask) : un écran d'appel simple et chaleureux pour Jeanne, et un tableau de bord pour la famille. Ce dernier regroupe les alertes, les résumés d'appels avec le raisonnement de l'agent, les transcriptions complètes, les notes de santé, les éléments à valider, un récapitulatif hebdomadaire et l'assistant d'enrichissement.
- **Vrais appels téléphoniques entrants** via Twilio, tour par tour : reconnaissance vocale, agent, voix clonée, lecture.
- **Voix clonées** avec ElevenLabs, une par petit-fils. Un modèle à faible latence est utilisé au téléphone.
- **SMS à la famille** après chaque appel et pour chaque alerte. Les messages sont raccourcis à un seul SMS pour passer sur un compte Twilio d'essai.

## Structure du dépôt

```
├─ app.py                  Application Flask : écran de Jeanne, tableau de bord famille et API
├─ agent.py                CompanionAgent (boucle d'outils MCP) et FamilyAgent (questionnaire et extraction)
├─ companion_server.py     Serveur MCP : outils, index ChromaDB des souvenirs, file de validation
├─ telephony.py            Appels téléphoniques entrants Twilio (TwiML)
├─ tts.py                  Synthèse vocale ElevenLabs
├─ notify.py               SMS à la famille (Twilio)
├─ skills/                 Règles de conversation et prompt du récapitulatif famille
├─ static/                 index.html (Jeanne) · family.html (tableau de bord)
├─ data/                   Persona, souvenirs, profil de santé, journal d'appels, alertes et transcriptions (fictifs)
├─ docs/presentation.html  Présentation du projet
└─ requirements.txt        Technologies utilisées : Flask, SDK Anthropic, MCP, ChromaDB, sentence-transformers, Twilio
```

## Éthique et limites

- Le dispositif est **configuré et supervisé par la famille**. Le but est d'apporter une présence rassurante, pas de tromper. Si Jeanne demande si elle parle à un robot, l'agent la rassure et l'oriente vers une vraie visite.
- **Tout danger réel est transmis** à de vrais proches. L'agent ne prend **aucune décision médicale** et ne donne aucun conseil médical, ce que la skill impose.
- C'est une preuve de concept réalisée en 12 heures. Elle n'a pas été testée avec de vrais patients et n'est pas un dispositif médical.

## Briques du cours réutilisées

RAG (TD3) · serveur MCP (TD4) · boucle d'agent avec outils (TD5) · skills (fichiers de prompt pour chaque comportement).
