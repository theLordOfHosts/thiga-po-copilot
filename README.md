# FlowPulse PO Copilot : Agent IA d'Aide à la Décision Product Owner

Ce projet répond au cas pratique **Product Builder Thiga**. Il met en scène **FlowPulse**, une startup SaaS B2B qui édite une plateforme de gestion de projet (120 clients B2B, du segment SMB au grand compte Enterprise).

Le Product Owner de FlowPulse fait face à deux problèmes au quotidien :
1. Un flux continu de retours clients dispersés entre les tickets Zendesk, les emails des CSM, la communauté Slack et les commentaires In-App.
2. Une difficulté à justifier objectivement la priorisation du backlog et à rédiger des User Stories complètes sans y passer ses soirées.

---

## 1. Démarche Produit & Architecture Hybride (Du problème à l'agent)

### L'impasse du "Tout-LLM"
La première idée quand on construit un assistant pour Product Owner consiste souvent à tout envoyer dans un grand prompt LLM : on lui donne 50 feedbacks en entrée et on lui demande de sortir un tableau RICE et des User Stories.

En pratique, cette approche pose vite trois problèmes majeurs :
* **Les hallucinations arithmétiques** : un LLM calcule mal les scores RICE et invente des chiffres différents d'une exécution à l'autre.
* **La pollution du backlog par le bruit SAV** : une demande de facture ou de réinitialisation de mot de passe finit transformée en fonctionnalité produit.
* **La perte de contrôle du PO** : si l'IA réécrit les priorités toute seule, le Product Owner perd la maîtrise de ses arbitrages face aux équipes Tech et Sales.

### Notre parti pris : IA Sémantique + Moteur Déterministe + Human-in-the-Loop
Nous avons conçu une architecture hybride où chaque brique fait uniquement ce qu'elle maîtrise :
1. **Le LLM (Gemini Flash en mode JSON strict)** agit comme extracteur sémantique : il qualifie l'intention, écarte le bruit SAV (`SUPPORT_NOISE`), rattache un retour client à une fonctionnalité existante ou émergente, et propose des estimations qualitatives argumentées.
2. **Python et SQLite** gardent le monopole des calculs et de l'état : les échelles sont verrouillées (Impact de `0.25` à `3.0`, suite de Fibonacci de `1` à `13`), la pondération client est déterministe (`Enterprise = 3`, `Scale-up = 2`, `SMB = 1`) et la formule `RICE = (Reach * Impact * Confidence) / Effort` est exécutée en Python pur.
3. **Le Product Owner garde le dernier mot (Bot Telegram & CLI)** : depuis son téléphone ou son ordinateur, le PO peut simuler un changement d'effort en direct pendant un Backlog Grooming avec son Tech Lead (boutons `Effort -0.5w / +0.5w`), voir le score RICE se recalculer instantanément, puis générer et valider la User Story.

---

## 2. Rôle des Fichiers et Scripts du Projet

~~~text
thiga-po-copilot/
├── config.py              # Configuration centrale, barèmes métier et chargement des clés API
├── db.py                  # Moteur SQLite (WAL), calculateur RICE Python et verrouillage concurrentiel
├── agent_po.py            # Cœur IA des 3 blocs (Comprendre, Arbitrer, Formaliser) + mode CLI autonome
├── bot_po.py              # Interface conversationnelle Telegram avec boutons d'action en temps réel
├── data/
│   └── seed_dataset.json  # Jeu de données initial (3 features au backlog + 12 feedbacks multicanaux)
├── tests/
│   └── test_agent_po.py   # Suite de 8 tests unitaires pytest (100% hors-ligne)
├── requirements.txt       # Dépendances Python du projet
└── .env.example           # Modèle de fichier de variables d'environnement
~~~

* **`config.py`** : Centralise les constantes métier (échelles strictes d'impact, de confiance, suite de Fibonacci, valeurs MoSCoW, pondération des segments clients) et charge les clés API Gemini et Telegram (depuis `.env` ou depuis les fichiers locaux `key.txt` et `telegram_keys.py`).
* **`db.py`** : Gère la base de données relationnelle SQLite (`po_copilot.db`) configurée en mode `WAL` (Write-Ahead Logging). Il contient les 3 tables (`features`, `feedbacks`, `user_stories`), la fonction déterministe `calculate_rice()`, le recadrage des barèmes `snap_to_scale()`, ainsi qu'un trigger SQLite `trg_features_row_version` qui incrémente automatiquement `row_version` à chaque modification pour empêcher les conflits d'écriture concurrents (`cas_update_feature`).
* **`agent_po.py`** : Contient toute l'intelligence métier des 3 blocs attendus, le client HTTP REST Gemini avec rotation automatique de clés/modèles en cas de quota atteint (`safe_llm_invoke`), et les validateurs de schémas (`validate_user_story_payload`). Il peut être lancé directement en ligne de commande via `--cli-demo`.
* **`bot_po.py`** : Serveur de polling Telegram (`pyTelegramBotAPI`) offrant une interface mobile et desktop sans friction pour le Product Owner. Il permet d'exécuter les 3 blocs, de coller des feedbacks clients à la volée dans le chat, et d'ajuster l'effort technique en direct via des boutons inline.
* **`data/seed_dataset.json`** : Contient l'état initial du backlog de FlowPulse (3 fonctionnalités) et 12 retours clients bruts issus de 4 canaux (Emails CSM, tickets Zendesk, Slack, NPS), incluant volontairement 2 demandes de support administratif (facture, mot de passe) pour démontrer le filtrage de bruit.
* **`tests/test_agent_po.py`** : Suite de tests unitaires `pytest` vérifiant hors-ligne le calcul RICE, le bornage des échelles, le filtrage du bruit SAV, la détection des User Stories mal formées et la protection contre les écritures concurrentes.

---

## 3. Couverture des 3 Blocs Fonctionnels

### Bloc 1 : COMPRENDRE (`analyze_all_pending_feedbacks` & `ingest_single_feedback`)
* **Triage multicanal** : traitement par lot ou au fil de l'eau des emails CSM, tickets Zendesk, messages Slack et commentaires NPS.
* **Filtrage du bruit** : séparation stricte entre `FEATURE_REQUEST`, `UX_FRICTION`, `BUG` et `SUPPORT_NOISE` (les demandes administratives sont journalisées mais exclues du backlog produit).
* **Clustering dynamique** : chaque retour valide est soit rattaché à une fonctionnalité existante du backlog (augmentant son `evidence_count` et son `reach` pondéré), soit converti en nouvelle opportunité produit émergente (ex: *Mode Hors-Ligne Mobile* ou *Intégration Webhook GitHub / GitLab*).

### Bloc 2 : ARBITRER (`prioritize_backlog`)
* **Double lecture RICE & MoSCoW** : combine un score quantitatif de rentabilité (RICE) et une catégorisation stratégique (`Must Have`, `Should Have`, `Could Have`, `Won't Have`).
* **Traçabilité des preuves** : chaque fonctionnalité affiche les verbatims exacts des clients qui l'ont demandée ainsi que la justification de l'arbitrage.
* **Simulation interactive en direct** : depuis Telegram, le PO ajuste l'effort par paliers de `0.5` semaine-personne et observe l'impact immédiat sur le score RICE.

### Bloc 3 : FORMALISER (`generate_user_story` & `validate_user_story_payload`)
* **Format standardisé** : rédaction au format *"En tant que <Persona>, je veux <Action> afin de <Valeur>"*.
* **Critères d'acceptation Gherkin** : 3 scénarios structurés (*Étant donné / Quand / Alors*) couvrant le cas nominal, les erreurs techniques et les restrictions de permissions.
* **Estimation Fibonacci & Slicing MVP automatique** : chiffrage dans la suite `1, 2, 3, 5, 8, 13`. Dès qu'une story atteint ou dépasse `8 points`, le validateur impose une proposition de découpage en 2 lots (Lot 1 MVP rapide vs Lot 2 complet).

---

## 4. Ce qui a été délégué à l'IA vs Contrôlé humainement

| Étape du projet | Délégué aux assistants IA | Vérifié, corrigé et contrôlé humainement |
| :--- | :--- | :--- |
| **Jeu de données** | Rédaction initiale de verbatims variés. | Calibrage métier B2B (pondération SMB vs Enterprise) et ajout de faux positifs SAV pour prouver l'efficacité du filtrage. |
| **Prompts & Schémas** | Structure JSON des prompts d'analyse et de rédaction. | Ajout de validateurs Python stricts (`validate_user_story_payload`, `snap_to_scale`) rejetant les placeholders `[...]`, les valeurs hors barème Fibonacci et les critères non-Gherkin, avec boucle d'auto-correction. |
| **Scoring RICE** | Évaluation qualitative de l'Impact et de la Confiance selon les verbatims. | Retrait du calcul mathématique RICE du prompt LLM : la formule et la pondération du Reach sont codées en Python pur dans `db.py`. |
| **Résilience & Tests** | Squelette du bot Telegram et de l'interface CLI. | Appels REST avec `responseMimeType: application/json`, rotation automatique multi-clés/modèles sur erreurs `402/503`, trigger SQLite `row_version` et tests `pytest`. |

---

## 5. Installation et Configuration

### Prérequis
* Python 3.10 ou supérieur
* Une clé API Google Gemini
* Un Token de Bot Telegram (uniquement si vous souhaitez lancer l'interface interactive Telegram ; le mode CLI fonctionne sans Telegram)

### 1. Cloner le dépôt et installer les dépendances
~~~bash
git clone <URL_DU_REPO>
cd thiga-po-copilot

python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip setuptools wheel
pip install --prefer-binary -r requirements.txt
~~~

### 2. Configurer la clé API Gemini et le Bot Telegram
Créez un fichier `.env` à la racine du projet à partir du modèle fourni :
~~~bash
cp .env.example .env
~~~

Renseignez vos clés dans `.env` :
~~~ini
GEMINI_API_KEY=AIzaSyVotreCleGemini...
TELEGRAM_BOT_TOKEN=1234567890:AAHVotreTokenBotFather...
TELEGRAM_CHAT_ID=VotreChatIdTelegram
~~~
*(Note : pour supporter la rotation automatique sur plusieurs clés Gemini, vous pouvez également placer une clé par ligne dans un fichier local `key.txt`, et vos identifiants Telegram dans `telegram_keys.py`).*

#### Comment initialiser un Bot Telegram en 30 secondes (Optionnel)
1. Ouvrez Telegram, cherchez **`@BotFather`** et envoyez la commande `/newbot`.
2. Donnez un nom d'affichage (ex: `FlowPulse PO Copilot`) puis un identifiant se terminant par `bot` (ex: `flowpulse_po_bot`).
3. Copiez le token HTTP généré par `@BotFather` dans `TELEGRAM_BOT_TOKEN`.
4. Cliquez sur le lien `t.me/...` fourni par `@BotFather` et appuyez sur **Start** pour ouvrir le canal de discussion.

---

## 6. Démarrage et Lancement des Scripts

### 1. Exécuter la suite de tests unitaires (Hors-ligne)
Avant de lancer l'agent, vérifiez que tous les garde-fous métier (calcul RICE, validation Gherkin, filtrage du bruit, concurrence SQLite) passent :
~~~bash
python -m pytest -v tests/test_agent_po.py
~~~

### 2. Mode A : Lancer la démonstration complète dans le Terminal (Sans Telegram)
Ce mode permet au jury de tester l'intégralité des 3 blocs (Comprendre, Arbitrer, Formaliser) en une seule commande :
~~~bash
python agent_po.py --cli-demo
~~~

Autres options disponibles sur `agent_po.py` :
* `python agent_po.py --seed` : Réinitialise uniquement la base de données SQLite `po_copilot.db` avec le jeu de données initial.
* `python agent_po.py --cli-demo --dry-run` : Exécute l'analyse IA en lecture seule sans modifier la base de données.

### 3. Mode B : Lancer le Bot Telegram Interactif (Mode Démo Live)
Démarrez le serveur de polling local dans votre terminal :
~~~bash
python bot_po.py
~~~

Ouvrez ensuite la conversation avec votre bot dans Telegram et utilisez les commandes ou les boutons interactifs :
* `/start` ou `/help` : Affiche le menu principal avec les boutons d'accès rapide aux 3 blocs.
* `/seed` : Réinitialise la base de démo (3 features existantes + 12 feedbacks bruts).
* `/ingest` : **Bloc 1 (Comprendre)**, analyse les 12 feedbacks en lot, écarte les 2 demandes SAV hors-produit, rattache les retours aux features existantes et crée les fonctionnalités émergentes.
* `/prioritize` : **Bloc 2 (Arbitrer)**, calcule les scores RICE et MoSCoW, justifie les arbitrages et affiche le classement complet.
* `/backlog` : Affiche les cartes détaillées des fonctionnalités avec leurs verbatims clients. Permet de simuler une variation d'effort (`Effort -0.5w` / `Effort +0.5w`) et de lancer le **Bloc 3 (Formaliser)** via le bouton `Générer User Story`.
* **Message texte libre** : Collez directement n'importe quel email, ticket ou commentaire client dans le chat Telegram pour que l'agent le qualifie en temps réel, mette à jour le Reach et recalcule instantanément le score RICE.
