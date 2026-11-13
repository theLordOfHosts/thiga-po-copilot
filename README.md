# FlowPulse PO Copilot : Agent IA d'Aide à la Décision Product Owner

Ce projet répond au cas pratique **Product Builder Thiga**. Il met en scène **FlowPulse**, une startup SaaS B2B éditrice d'une plateforme de gestion de projet (120 clients B2B, du segment SMB au grand compte Enterprise).

Le Product Owner de FlowPulse fait face à deux problèmes quotidiens :
1. Un flux continu de retours clients éparpillés entre les tickets Zendesk, les emails des CSM, la communauté Slack et les commentaires In-App.
2. Une difficulté à justifier objectivement la priorisation du backlog et à rédiger des User Stories complètes sans y passer ses soirées.

---

## 1. Philosophie Produit & Démarche (Du problème à l'agent)

### Pourquoi la plupart des agents PO échouent en production
La première tentation quand on construit un assistant pour Product Owner consiste à tout déléguer à un grand prompt LLM : on lui jette 50 feedbacks en entrée et on lui demande de sortir un tableau RICE et des User Stories.

En pratique, cette approche se heurte vite à trois murs :
* **Les hallucinations arithmétiques** : un LLM est mauvais pour calculer un score RICE cohérent et a tendance à inventer des chiffres différents à chaque exécution.
* **La pollution du backlog par le bruit SAV** : une demande de réinitialisation de mot de passe ou de facture finit transformée en fonctionnalité produit.
* **La perte de contrôle du PO** : si l'IA réécrit les priorités toute seule, le Product Owner perd la maîtrise de ses arbitrages face aux équipes Tech et Sales.

### Notre parti pris : IA Sémantique + Moteur Déterministe + Human-in-the-Loop
Nous avons conçu une architecture hybride où chaque composant fait uniquement ce qu'il sait faire de mieux :
1. **Le LLM (Gemini 2.5 Flash)** agit comme extracteur sémantique : il classe l'intention, filtre le bruit SAV (`SUPPORT_NOISE`), rattache un retour client à un problème existant ou émergent, et propose des estimations qualitatives argumentées.
2. **Python et SQLite** gardent le monopole des mathématiques et de l'état : les échelles sont contraintes (Impact `0.25` à `3.0`, Fibonacci `1` à `13`), la pondération client est déterministe (`Enterprise = 3`, `Scale-up = 2`, `SMB = 1`) et la formule `RICE = (Reach * Impact * Confidence) / Effort` est calculée en Python.
3. **Le Product Owner garde le dernier mot (Interface Telegram + CLI)** : en un clic sur mobile ou desktop, le PO peut simuler un changement d'effort en direct pendant un Backlog Grooming avec son Tech Lead (boutons `Effort -0.5w / +0.5w`), voir le score RICE se recalculer instantanément, puis générer et valider la User Story.

---

## 2. Couverture des 3 Blocs Fonctionnels

### Bloc 1 : COMPRENDRE (`analyze_all_pending_feedbacks` & `ingest_single_feedback`)
* **Triage multicanal** : traitement des emails CSM, tickets Zendesk, messages Slack et NPS.
* **Filtrage du bruit** : séparation stricte entre `FEATURE_REQUEST`, `UX_FRICTION`, `BUG` et `SUPPORT_NOISE` (les demandes de factures ou d'accès sont journalisées mais exclues du backlog produit).
* **Clustering dynamique** : chaque retour valide est soit rattaché à une fonctionnalité existante du backlog (incrémentant son `evidence_count` et son `reach` pondéré par la taille du client), soit converti en nouvelle opportunité produit émergente (ex: *Mode Hors-Ligne Mobile* ou *Intégration Webhook GitHub/GitLab*).
* **Ingestion temps réel** : coller n'importe quel message client dans le bot Telegram déclenche son analyse à la volée et met à jour le score RICE de la fonctionnalité concernée.

### Bloc 2 : ARBITRER (`prioritize_backlog`)
* **Double lecture RICE & MoSCoW** : combine un score quantitatif d'ordonnancement (RICE) et une catégorisation stratégique (`Must Have`, `Should Have`, `Could Have`, `Won't Have`).
* **Traçabilité des preuves** : chaque fonctionnalité conserve le lien vers les verbatims exacts des clients qui l'ont demandée.
* **Simulation interactive en direct** : depuis Telegram, le PO peut ajuster l'effort à la volée. Un mécanisme de contrôle de concurrence (`row_version` via trigger SQLite) garantit l'intégrité des mises à jour.

### Bloc 3 : FORMALISER (`generate_user_story` & `validate_user_story_payload`)
* **Format standardisé** : génération au format strict *"En tant que <Persona>, je veux <Action> afin de <Valeur>"*.
* **Critères d'acceptation Gherkin** : scénarios structurés (*Étant donné / Quand / Alors*) couvrant systématiquement le cas nominal et les cas limites (permissions, erreurs, mode dégradé).
* **Estimation & Slicing MVP automatique** : chiffrage en points Fibonacci (`1, 2, 3, 5, 8, 13`). Si une User Story dépasse ou atteint `8 points`, un validateur force l'IA à proposer un découpage concret en deux lots (Lot 1 MVP vs Lot 2 complet).

---

## 3. Ce qui a été délégué à l'IA vs Contrôlé humainement

| Étape du projet | Délégué aux assistants IA | Vérifié, corrigé et contrôlé humainement |
| :--- | :--- | :--- |
| **Jeu de données** | Génération initiale de verbatims variés. | Calibrage métier B2B (distinction SMB vs Enterprise) et injection volontaire de faux positifs SAV pour tester le filtrage. |
| **Prompts & Schémas** | Rédaction des structures JSON d'extraction. | Ajout de validateurs Python stricts (`validate_user_story_payload`, `snap_to_scale`) rejetant les placeholders `[...]`, les valeurs hors barème Fibonacci et les absences de clauses Gherkin. |
| **Scoring RICE** | Évaluation qualitative de l'Impact et de la Confiance à partir des verbatims. | Interdiction faite au LLM de calculer le score RICE lui-même : le calcul mathématique et la pondération du Reach sont codés en Python pur et couverts par des tests unitaires. |
| **Architecture & Tests** | Squelette du bot Telegram et CLI. | Mise en place du mode WAL SQLite, du trigger `row_version` anti-écrasement concurrent et d'une suite de 8 tests `pytest` hors-ligne. |

---

## 4. Installation et Lancement Rapide

### Prérequis
* Python 3.10+
* Une clé API Google Gemini (dans `.env` sous `GEMINI_API_KEY` ou dans un fichier `key.txt`).

### 1. Installation
```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt