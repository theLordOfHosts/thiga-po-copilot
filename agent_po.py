import re
import sys
import json
import time
import argparse
from loguru import logger
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import HumanMessage

from config import (
    APP_VERSION, LLM_MODEL, get_api_keys,
    IMPACT_SCALE, CONFIDENCE_SCALE, FIBONACCI_SCALE,
    MOSCOW_VALUES, FEEDBACK_CATEGORIES, SEGMENT_WEIGHTS
)
from db import init_db, seed_db, calculate_rice, snap_to_scale

logger.remove()
LOG_FORMAT = "{time:YYYY-MM-DD HH:mm:ss} | {level} | [{extra[agent]}] {message}"
logger.configure(extra={"agent": "PO-ENG"})
logger.add(sys.stderr, format=LOG_FORMAT)
logger = logger.bind(agent="PO-ENG")

# Fallback models with separate quota buckets in Google AI Studio
FALLBACK_MODELS = (
    LLM_MODEL,
    "gemini-2.5-flash-lite",
    "gemini-2.0-flash",
    "gemini-2.0-flash-lite",
)


def llm_text(response):
    content = response.content
    if isinstance(content, list):
        return "".join([item.get("text", "") if isinstance(item, dict) else str(item) for item in content])
    return str(content)


def _clean_json_str(s):
    s = s.replace("```json", "").replace("```", "").strip()
    s = re.sub(r",\s*([\]}])", r"\1", s)
    return s


def extract_json_object(raw_text):
    cleaned = _clean_json_str(raw_text)
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    return json.loads(_clean_json_str(match.group(0)) if match else cleaned)


def extract_json_array(raw_text):
    cleaned = _clean_json_str(raw_text)
    match = re.search(r"\[.*\]", cleaned, re.DOTALL)
    return json.loads(_clean_json_str(match.group(0)) if match else cleaned)


import requests

_DEAD_KEYS = set()
_SLOW_MODELS = set()

def safe_llm_invoke(prompt_text, max_retries=2):
    """Invoke Gemini via REST API with native JSON mode, skipping 402 keys and 503 overloaded models."""
    keys = get_api_keys()
    if not keys:
        raise RuntimeError("No Gemini API key found. Add key.txt or GEMINI_API_KEY in .env")

    base_models = [
        "gemini-3.5-flash-lite",
        "gemini-3.8-flash",
    ]
    models = [m for m in base_models if m not in _SLOW_MODELS] + [m for m in base_models if m in _SLOW_MODELS]

    for attempt in range(max_retries):
        for model_name in models:
            for idx, key in enumerate(keys):
                if key in _DEAD_KEYS:
                    continue
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={key}"
                payload = {
                    "contents": [{"parts": [{"text": prompt_text}]}],
                    "generationConfig": {
                        "temperature": 0.2,
                        "responseMimeType": "application/json"
                    }
                }
                try:
                    logger.info(f"🔄 Calling {model_name} with Key #{idx + 1}...")
                    resp = requests.post(url, json=payload, timeout=25)
                    if resp.status_code == 200:
                        data = resp.json()
                        return data["candidates"][0]["content"]["parts"][0]["text"]
                    else:
                        err_msg = resp.json().get("error", {}).get("message", resp.text[:120])
                        logger.warning(f"⚠️ Key #{idx + 1} on {model_name} failed (HTTP {resp.status_code}): {err_msg}")
                        if resp.status_code == 402:
                            _DEAD_KEYS.add(key)
                        elif resp.status_code == 503:
                            _SLOW_MODELS.add(model_name)
                            break
                        continue
                except Exception as e:
                    logger.warning(f"⚠️ Key #{idx + 1} on {model_name} network/timeout error: {e}")
                    _SLOW_MODELS.add(model_name)
                    break
        if attempt < max_retries - 1:
            time.sleep(2)
    raise RuntimeError("All Gemini API keys and fallback models exhausted.")


# ---------------------------------------------------------------------------
# BLOCK 1: COMPRENDRE (Feedback Triage & Semantic Clustering)
# ---------------------------------------------------------------------------
def parse_triage_payload(raw_text, valid_feature_ids):
    data = extract_json_object(raw_text) if isinstance(raw_text, str) else raw_text
    cat = str(data.get("category", "FEATURE_REQUEST")).upper()
    if cat not in FEEDBACK_CATEGORIES:
        cat = "FEATURE_REQUEST"

    sentiment = str(data.get("sentiment", "NEUTRAL")).upper()
    if sentiment not in ("NEGATIVE", "NEUTRAL", "POSITIVE"):
        sentiment = "NEUTRAL"

    matched_id = data.get("matched_feature_id")
    if matched_id is not None:
        try:
            matched_id = int(matched_id)
            if matched_id not in valid_feature_ids:
                matched_id = None
        except (ValueError, TypeError):
            matched_id = None

    extracted = str(data.get("extracted_need") or "").strip()[:300]
    raw_title = data.get("new_feature_title")
    raw_prob = data.get("new_feature_problem")
    clean_title = str(raw_title).strip()[:120] if (raw_title and str(raw_title).lower() != "none") else f"[Bug/Besoin] {extracted[:70]}"
    clean_prob = str(raw_prob).strip()[:350] if (raw_prob and str(raw_prob).lower() != "none") else extracted

    return {
        "category": cat,
        "sentiment": sentiment,
        "extracted_need": extracted,
        "matched_feature_id": matched_id,
        "new_feature_title": clean_title if not matched_id else None,
        "new_feature_problem": clean_prob if not matched_id else None,
    }


def _apply_triage_to_db(cur, fb_row, parsed):
    feature_id = parsed["matched_feature_id"]
    seg_weight = SEGMENT_WEIGHTS.get(fb_row["segment"], 1)

    if parsed["category"] != "SUPPORT_NOISE":
        if feature_id is not None:
            feat = cur.execute("SELECT reach, impact, confidence, effort, evidence_count FROM features WHERE id = ?", (feature_id,)).fetchone()
            if feat:
                new_reach = (feat["reach"] or 1.0) + seg_weight
                new_ev = (feat["evidence_count"] or 1) + 1
                new_rice = calculate_rice(new_reach, feat["impact"], feat["confidence"], feat["effort"])
                cur.execute("""
                    UPDATE features
                    SET evidence_count = ?, reach = ?, rice_score = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                """, (new_ev, new_reach, new_rice, feature_id))
        elif parsed["new_feature_title"]:
            existing = cur.execute("SELECT id, reach, impact, confidence, effort, evidence_count FROM features WHERE LOWER(title) = LOWER(?)", (parsed["new_feature_title"],)).fetchone()
            if existing:
                feature_id = existing["id"]
                new_reach = (existing["reach"] or 1.0) + seg_weight
                new_ev = (existing["evidence_count"] or 1) + 1
                new_rice = calculate_rice(new_reach, existing["impact"], existing["confidence"], existing["effort"])
                cur.execute("UPDATE features SET evidence_count = ?, reach = ?, rice_score = ? WHERE id = ?", (new_ev, new_reach, new_rice, feature_id))
            else:
                init_reach = float(seg_weight)
                init_rice = calculate_rice(init_reach, 1.0, 0.8, 2.0)
                cur.execute("""
                    INSERT INTO features (title, problem_statement, evidence_count, reach, impact, confidence, effort, rice_score, status)
                    VALUES (?, ?, 1, ?, 1.0, 0.8, 2.0, ?, 'candidate')
                """, (parsed["new_feature_title"], parsed["new_feature_problem"] or parsed["extracted_need"], init_reach, init_rice))
                feature_id = cur.lastrowid

    cur.execute("""
        UPDATE feedbacks
        SET category = ?, sentiment = ?, extracted_need = ?, feature_id = ?, processed = 1
        WHERE id = ?
    """, (parsed["category"], parsed["sentiment"], parsed["extracted_need"], feature_id, fb_row["id"]))


def ingest_single_feedback(conn, fb_row, dry_run=False):
    """Classify one feedback item and link it to an existing or emerging backlog feature."""
    cur = conn.cursor()
    features = cur.execute("SELECT id, title, problem_statement FROM features").fetchall()
    valid_ids = {f["id"] for f in features}
    features_catalog = [{"id": f["id"], "title": f["title"], "problem": f["problem_statement"]} for f in features]

    prompt = f"""
    Tu es un Lead Product Owner sur FlowPulse (SaaS B2B de gestion de projet).
    Analyse ce feedback client brut et rattache-le au backlog existant s'il correspond, ou propose une nouvelle fonctionnalité.

    BACKLOG EXISTANT :
    {json.dumps(features_catalog, ensure_ascii=False)}

    FEEDBACK CLIENT BRUT :
    Canal : {fb_row['channel']} | Client : {fb_row['customer_name']} ({fb_row['segment']})
    Message : "{fb_row['raw_text']}"

    RÈGLES STRICTES :
    - Si le message concerne une facture, un mot de passe oublié ou du support administratif hors produit, utilise category = "SUPPORT_NOISE" et matched_feature_id = null.
    - Si le besoin correspond à une fonctionnalité du BACKLOG EXISTANT, indique son ID exact dans matched_feature_id.
    - Si c'est un nouveau besoin produit non couvert, mets matched_feature_id = null et renseigne new_feature_title et new_feature_problem.
    - N'utilise jamais de tiret cadratin dans tes textes.

    RENVOIE UNIQUEMENT UN JSON STRICT :
    {{
        "category": "FEATURE_REQUEST" | "UX_FRICTION" | "BUG" | "SUPPORT_NOISE",
        "sentiment": "NEGATIVE" | "NEUTRAL" | "POSITIVE",
        "extracted_need": "Synthèse du besoin en 1 phrase courte",
        "matched_feature_id": <int ou null>,
        "new_feature_title": "<Titre court si nouveau besoin, sinon null>",
        "new_feature_problem": "<Problème utilisateur si nouveau besoin, sinon null>"
    }}
    """

    raw = safe_llm_invoke(prompt)
    parsed = parse_triage_payload(raw, valid_ids)

    if not dry_run:
        _apply_triage_to_db(cur, fb_row, parsed)
        conn.commit()
    return parsed


def analyze_all_pending_feedbacks(conn, dry_run=False):
    """Batch-analyze all pending feedbacks in a single LLM request to save quota and latency."""
    cur = conn.cursor()
    pending = cur.execute("SELECT * FROM feedbacks WHERE processed = 0 ORDER BY id ASC").fetchall()
    results = {"processed": 0, "noise": 0, "linked": 0}
    if not pending:
        return results

    features = cur.execute("SELECT id, title, problem_statement FROM features").fetchall()
    valid_ids = {f["id"] for f in features}
    features_catalog = [{"id": f["id"], "title": f["title"], "problem": f["problem_statement"]} for f in features]
    feedbacks_input = [
        {"feedback_id": fb["id"], "channel": fb["channel"], "customer": fb["customer_name"], "segment": fb["segment"], "text": fb["raw_text"]}
        for fb in pending
    ]

    prompt = f"""
    Tu es un Lead Product Owner sur FlowPulse (SaaS B2B de gestion de projet).
    Analyse ce lot de feedbacks clients bruts. Pour chacun, rattache-le au backlog existant s'il correspond, ou propose un titre de nouvelle fonctionnalité cohérent (utilise exactement le même new_feature_title si plusieurs feedbacks expriment le même nouveau besoin, ex: "Mode Hors-Ligne Mobile" ou "Intégration Webhook GitHub / GitLab").

    BACKLOG EXISTANT :
    {json.dumps(features_catalog, ensure_ascii=False)}

    LOT DE FEEDBACKS À TRAITER :
    {json.dumps(feedbacks_input, ensure_ascii=False)}

    RÈGLES STRICTES :
    - Si le message concerne une facture, un mot de passe oublié ou du support administratif hors produit, utilise category = "SUPPORT_NOISE" et matched_feature_id = null.
    - N'utilise jamais de tiret cadratin.

    RENVOIE UNIQUEMENT UN TABLEAU JSON STRICT :
    [
      {{
        "feedback_id": <int>,
        "category": "FEATURE_REQUEST" | "UX_FRICTION" | "BUG" | "SUPPORT_NOISE",
        "sentiment": "NEGATIVE" | "NEUTRAL" | "POSITIVE",
        "extracted_need": "Synthèse en 1 phrase",
        "matched_feature_id": <int ou null>,
        "new_feature_title": "<Titre court si nouveau besoin, sinon null>",
        "new_feature_problem": "<Problème utilisateur si nouveau besoin, sinon null>"
      }}
    ]
    """

    raw = safe_llm_invoke(prompt)
    items = extract_json_array(raw)
    by_id = {int(item["feedback_id"]): item for item in items if "feedback_id" in item}

    for fb in pending:
        item = by_id.get(fb["id"], {})
        parsed = parse_triage_payload(item, valid_ids)
        if not dry_run:
            _apply_triage_to_db(cur, fb, parsed)
        results["processed"] += 1
        if parsed["category"] == "SUPPORT_NOISE":
            results["noise"] += 1
        else:
            results["linked"] += 1

    if not dry_run:
        conn.commit()
    return results


# ---------------------------------------------------------------------------
# BLOCK 2: ARBITRER (RICE & MoSCoW Prioritization)
# ---------------------------------------------------------------------------
def parse_scoring_payload(raw_text):
    data = extract_json_object(raw_text) if isinstance(raw_text, str) else raw_text
    impact = snap_to_scale(data.get("impact", 1.0), IMPACT_SCALE)
    confidence = snap_to_scale(data.get("confidence", 0.8), CONFIDENCE_SCALE)
    try:
        effort = max(0.5, min(20.0, round(float(data.get("effort", 2.0)), 1)))
    except (TypeError, ValueError):
        effort = 2.0

    moscow = str(data.get("moscow", "Should Have")).strip()
    if moscow not in MOSCOW_VALUES:
        moscow = "Should Have"

    rationale = str(data.get("rationale", "")).replace("—", "-").replace("–", "-").strip()[:450]
    if not rationale:
        raise ValueError("Missing prioritization rationale")

    return {
        "impact": impact,
        "confidence": confidence,
        "effort": effort,
        "moscow": moscow,
        "rationale": rationale
    }


def prioritize_backlog(conn, dry_run=False):
    """Batch-evaluate all features in one LLM request and compute deterministic RICE in Python."""
    cur = conn.cursor()
    features = cur.execute("SELECT * FROM features ORDER BY id ASC").fetchall()
    if not features:
        return 0

    features_payload = []
    for feat in features:
        verbatims = cur.execute(
            "SELECT customer_name, segment, raw_text FROM feedbacks WHERE feature_id = ?",
            (feat["id"],)
        ).fetchall()
        features_payload.append({
            "feature_id": feat["id"],
            "title": feat["title"],
            "problem": feat["problem_statement"],
            "evidence_count": feat["evidence_count"],
            "reach": feat["reach"],
            "verbatims": [f"[{v['segment']}] {v['customer_name']}: {v['raw_text']}" for v in verbatims]
        })

    prompt = f"""
    Tu es CPO d'une scale-up SaaS B2B (FlowPulse). Évalue chaque fonctionnalité du backlog pour arbitrer sa priorité.

    FONCTIONNALITÉS DU BACKLOG :
    {json.dumps(features_payload, ensure_ascii=False)}

    GRILLE DE SCORING STRICTE :
    - impact : 3.0 (Massif / bloque des deals Enterprise), 2.0 (Fort), 1.0 (Moyen), 0.5 (Faible), 0.25 (Minimal)
    - confidence : 1.0 (Haute, plusieurs preuves convergentes), 0.8 (Moyenne), 0.5 (Faible)
    - effort : Estimation en semaines-personnes (ex: 1.0, 1.5, 2.0, 3.0, 5.0)
    - moscow : "Must Have" | "Should Have" | "Could Have" | "Won't Have"
    - rationale : Justification concise en Français (2 phrases max) citant l'impact client. Interdiction d'utiliser des tirets cadratins.

    RENVOIE UNIQUEMENT UN TABLEAU JSON STRICT :
    [
      {{
        "feature_id": <int>,
        "impact": <float>,
        "confidence": <float>,
        "effort": <float>,
        "moscow": "<Must Have|Should Have|Could Have|Won't Have>",
        "rationale": "<Justification claire et directe>"
      }}
    ]
    """

    raw = safe_llm_invoke(prompt)
    items = extract_json_array(raw)
    by_id = {int(item["feature_id"]): item for item in items if "feature_id" in item}

    scored_count = 0
    for feat in features:
        item = by_id.get(feat["id"])
        if not item:
            continue
        scored = parse_scoring_payload(item)
        rice = calculate_rice(feat["reach"], scored["impact"], scored["confidence"], scored["effort"])
        logger.info(f"⚖️ Scored '{feat['title']}' -> RICE: {rice} | {scored['moscow']}")
        if not dry_run:
            cur.execute("""
                UPDATE features
                SET impact = ?, confidence = ?, effort = ?, rice_score = ?, moscow = ?, rationale = ?, status = 'prioritized'
                WHERE id = ?
            """, (
                scored["impact"], scored["confidence"], scored["effort"],
                rice, scored["moscow"], scored["rationale"], feat["id"]
            ))
        scored_count += 1

    if not dry_run:
        conn.commit()
    return scored_count


# ---------------------------------------------------------------------------
# BLOCK 3: FORMALISER (User Story, Gherkin ACs & Complexity Slicing)
# ---------------------------------------------------------------------------
def validate_user_story_payload(data):
    """Strict quality gate on generated User Stories (no placeholders, valid Gherkin, Fibonacci points)."""
    problems = []
    story = str(data.get("story_text", "")).strip()
    if not story or len(story) < 25:
        problems.append("story_too_short")
    if not all(k in story.lower() for k in ("en tant que", "je veux", "afin de")):
        problems.append("missing_standard_template")
    if re.search(r"\[.*?\]", story):
        problems.append("unresolved_placeholder")
    if "—" in story or "–" in story:
        problems.append("forbidden_dash")

    acs = data.get("acceptance_criteria", [])
    if not isinstance(acs, list) or len(acs) < 2:
        problems.append("insufficient_acceptance_criteria")
    if isinstance(acs, list):
        for ac in acs:
            ac_low = str(ac).lower()
            if not ("étant donné" in ac_low or "quand" in ac_low or "alors" in ac_low):
                problems.append("non_gherkin_ac")
                break

    pts = data.get("story_points")
    if pts not in FIBONACCI_SCALE:
        problems.append(f"invalid_fibonacci({pts})")

    slice_hint = str(data.get("mvp_slice_suggestion", "") or "").strip()
    if isinstance(pts, int) and pts >= 8 and len(slice_hint) < 15:
        problems.append("missing_mvp_split_for_large_story")

    return problems


def generate_user_story(conn, feature_id, dry_run=False):
    cur = conn.cursor()
    feat = cur.execute("SELECT * FROM features WHERE id = ?", (feature_id,)).fetchone()
    if not feat:
        raise ValueError(f"Feature #{feature_id} not found")

    verbatims = cur.execute(
        "SELECT customer_name, segment, raw_text FROM feedbacks WHERE feature_id = ?",
        (feature_id,)
    ).fetchall()
    verbatim_list = [f"[{v['segment']}] {v['customer_name']}: {v['raw_text']}" for v in verbatims]

    prompt = f"""
    Tu es un Product Owner Senior. Rédige une User Story prête pour le Sprint Planning concernant la fonctionnalité suivante.

    FONCTIONNALITÉ : {feat['title']}
    PROBLÈME MÉTIER : {feat['problem_statement']}
    CONTEXTE & VERBATIMS CLIENTS : {json.dumps(verbatim_list, ensure_ascii=False)}

    RÈGLES DE FORMALISATION STRICTES :
    1. story_text : Doit utiliser exactement le format "En tant que <Persona précis>, je veux <Action claire> afin de <Valeur métier mesurable>." Aucun crochet [ ] autorisé.
    2. acceptance_criteria : Liste de 3 critères d'acceptation au format Gherkin en Français ("Étant donné que..., Quand..., Alors..."), couvrant le cas nominal et au moins un cas limite (erreur, permission ou hors-ligne).
    3. story_points : Estimation de complexité relative dans la suite de Fibonacci : {list(FIBONACCI_SCALE)}.
    4. tshirt_size : "XS" | "S" | "M" | "L" | "XL".
    5. mvp_slice_suggestion : Si story_points >= 8, propose un découpage concret en 2 lots (Lot 1 MVP rapide vs Lot 2 complet). Si < 8, indique "Découpage non requis (livrable en 1 sprint)".
    6. INTERDICTION d'utiliser les tirets cadratins ou demi-cadratins.

    RENVOIE UNIQUEMENT UN JSON STRICT :
    {{
        "story_text": "En tant que ..., je veux ... afin de ...",
        "acceptance_criteria": [
            "Scénario 1 (Nominal) : Étant donné que ..., Quand ..., Alors ...",
            "Scénario 2 (Cas limite) : Étant donné que ..., Quand ..., Alors ...",
            "Scénario 3 (Sécurité / UX) : Étant donné que ..., Quand ..., Alors ..."
        ],
        "story_points": <int dans 1, 2, 3, 5, 8, 13>,
        "tshirt_size": "<S|M|L>",
        "mvp_slice_suggestion": "<Proposition de découpage MVP>"
    }}
    """

    parsed = None
    problems = ["uninitialized"]
    for attempt in range(2):
        raw = safe_llm_invoke(prompt)
        data = extract_json_object(raw)
        data["story_points"] = int(snap_to_scale(data.get("story_points", 5), FIBONACCI_SCALE))
        problems = validate_user_story_payload(data)
        parsed = data
        if not problems:
            break
        logger.warning(f"Retry User Story generation for feature #{feature_id}: {problems}")

    if dry_run:
        return parsed, problems

    status = "drafted" if not problems else "needs_review"
    cur.execute("""
        INSERT INTO user_stories (feature_id, story_text, acceptance_criteria, story_points, tshirt_size, mvp_slice_suggestion, status)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(feature_id) DO UPDATE SET
            story_text = excluded.story_text,
            acceptance_criteria = excluded.acceptance_criteria,
            story_points = excluded.story_points,
            tshirt_size = excluded.tshirt_size,
            mvp_slice_suggestion = excluded.mvp_slice_suggestion,
            status = excluded.status,
            updated_at = CURRENT_TIMESTAMP
    """, (
        feature_id,
        parsed["story_text"],
        json.dumps(parsed["acceptance_criteria"], ensure_ascii=False),
        parsed["story_points"],
        parsed.get("tshirt_size", "M"),
        parsed.get("mvp_slice_suggestion", ""),
        status
    ))
    cur.execute("UPDATE features SET status = 'spec_ready' WHERE id = ?", (feature_id,))
    conn.commit()
    return parsed, problems


def run_cli_demo(dry_run=False):
    """Standalone CLI execution for jury evaluation without Telegram."""
    logger.info(f"🚀 Launching Thiga PO Copilot V{APP_VERSION} (CLI Demo Mode)")
    conn = seed_db(reset=True)
    logger.info("📥 Step 1/3 [COMPRENDRE] : Analyzing & clustering multi-channel feedbacks (batched)...")
    stats = analyze_all_pending_feedbacks(conn, dry_run=dry_run)
    logger.success(f"Feedbacks processed: {stats['processed']} | Product needs linked: {stats['linked']} | Support noise filtered: {stats['noise']}")

    logger.info("⚖️ Step 2/3 [ARBITRER] : Scoring & prioritizing backlog (RICE + MoSCoW)...")
    prioritize_backlog(conn, dry_run=dry_run)

    rows = conn.execute("SELECT id, title, rice_score, moscow, reach, impact, confidence, effort, rationale FROM features ORDER BY rice_score DESC").fetchall()
    print("\n" + "=" * 80)
    print("📊 BACKLOG PRIORISÉ (TRIÉ PAR SCORE RICE)")
    print("=" * 80)
    for rank, r in enumerate(rows, 1):
        print(f"#{rank} [{r['moscow']}] {r['title']} -> RICE: {r['rice_score']} (R:{r['reach']} I:{r['impact']} C:{r['confidence']} E:{r['effort']}w)")
        print(f"   Justification : {r['rationale']}\n")

    if rows:
        top_id = rows[0]["id"]
        logger.info(f"📝 Step 3/3 [FORMALISER] : Generating User Story for top feature #{top_id} ({rows[0]['title']})...")
        us, probs = generate_user_story(conn, top_id, dry_run=dry_run)
        print("=" * 80)
        print(f"📝 USER STORY GÉNÉRÉE : {rows[0]['title']}")
        print("=" * 80)
        print(f"Story : {us['story_text']}")
        print(f"Complexité : {us['story_points']} pts Fibonacci (Taille {us['tshirt_size']})")
        print(f"Découpage MVP : {us['mvp_slice_suggestion']}")
        print("Critères d'acceptation (Gherkin) :")
        for ac in us["acceptance_criteria"]:
            print(f"  - {ac}")
        print("=" * 80)
    conn.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Thiga PO Copilot Engine")
    parser.add_argument("--seed", action="store_true", help="Reset and seed the SQLite database")
    parser.add_argument("--cli-demo", action="store_true", help="Run the 3 blocks end-to-end in terminal")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing AI mutations to DB")
    args = parser.parse_args()

    if args.seed:
        seed_db(reset=True)
        logger.success("Database reset and seeded with 12 feedbacks and 3 initial features.")
    elif args.cli_demo:
        run_cli_demo(dry_run=args.dry_run)
    else:
        parser.print_help()