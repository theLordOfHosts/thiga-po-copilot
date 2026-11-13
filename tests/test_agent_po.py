import os
import json
import tempfile
import pytest

from db import init_db, seed_db, calculate_rice, snap_to_scale, cas_update_feature
from agent_po import (
    parse_triage_payload,
    parse_scoring_payload,
    validate_user_story_payload,
    ingest_single_feedback
)
import agent_po


@pytest.fixture
def temp_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = seed_db(db_path=path, reset=True)
    yield conn
    conn.close()
    if os.path.exists(path):
        os.remove(path)


def test_deterministic_rice_calculation():
    # Reach=10, Impact=2.0, Confidence=0.8, Effort=4.0 => (10 * 2 * 0.8) / 4 = 4.0
    assert calculate_rice(10, 2.0, 0.8, 4.0) == 4.0
    # Zero effort is clamped to 0.5 minimum to prevent ZeroDivisionError
    assert calculate_rice(5, 1.0, 1.0, 0) == 10.0


def test_scale_snapping_enforces_rubrics():
    assert snap_to_scale(2.7, (0.25, 0.5, 1.0, 2.0, 3.0)) == 3.0
    assert snap_to_scale(0.75, (0.5, 0.8, 1.0)) == 0.8
    assert snap_to_scale(7, (1, 2, 3, 5, 8, 13)) == 8


def test_triage_parser_filters_invalid_feature_ids():
    raw = '{"category": "FEATURE_REQUEST", "sentiment": "NEGATIVE", "extracted_need": "Besoin export", "matched_feature_id": 999}'
    parsed = parse_triage_payload(raw, valid_feature_ids={1, 2, 3})
    assert parsed["matched_feature_id"] is None
    assert parsed["category"] == "FEATURE_REQUEST"


def test_scoring_parser_sanitizes_scales_and_dashes():
    raw = '{"impact": 2.9, "confidence": 0.85, "effort": 2.0, "moscow": "Must Have", "rationale": "Bloquant client — critique"}'
    scored = parse_scoring_payload(raw)
    assert scored["impact"] == 3.0
    assert scored["confidence"] == 0.8
    assert "—" not in scored["rationale"]


def test_user_story_validator_catches_bad_outputs():
    valid_payload = {
        "story_text": "En tant que Chef de projet, je veux exporter le Gantt en PDF afin de gagner 45 minutes par COPIL.",
        "acceptance_criteria": [
            "Étant donné que je suis sur la vue Gantt, Quand je clique sur Exporter PDF, Alors un fichier A3 est téléchargé.",
            "Étant donné que le projet contient 0 tâche, Quand j'ouvre le menu, Alors le bouton d'export est grisé."
        ],
        "story_points": 8,
        "tshirt_size": "L",
        "mvp_slice_suggestion": "Lot 1 : Export PDF statique A4. Lot 2 : Personnalisation A3 et filtre par dates."
    }
    assert validate_user_story_payload(valid_payload) == []

    # Invalid: has placeholder [Persona], story_points=8 without MVP split, and forbidden dash
    broken_payload = {
        "story_text": "En tant que [Persona], je veux exporter — afin de gagner du temps.",
        "acceptance_criteria": ["Simple texte sans gherkin"],
        "story_points": 8,
        "mvp_slice_suggestion": ""
    }
    problems = validate_user_story_payload(broken_payload)
    assert "unresolved_placeholder" in problems
    assert "forbidden_dash" in problems
    assert "non_gherkin_ac" in problems
    assert "missing_mvp_split_for_large_story" in problems


def test_row_version_trigger_and_cas_concurrency(temp_db):
    feat = temp_db.execute("SELECT id, row_version, effort FROM features LIMIT 1").fetchone()
    fid, v0 = feat["id"], feat["row_version"]

    # First update succeeds and trigger bumps row_version to v0 + 1
    assert cas_update_feature(temp_db, fid, v0, "effort = ?", (5.0,)) is True
    updated = temp_db.execute("SELECT row_version, effort FROM features WHERE id = ?", (fid,)).fetchone()
    assert updated["row_version"] == v0 + 1
    assert updated["effort"] == 5.0

    # Stale update using old v0 must be rejected
    assert cas_update_feature(temp_db, fid, v0, "effort = ?", (1.0,)) is False


def test_support_noise_does_not_pollute_backlog(temp_db, monkeypatch):
    monkeypatch.setattr(
        agent_po, "safe_llm_invoke",
        lambda prompt: '{"category": "SUPPORT_NOISE", "sentiment": "NEUTRAL", "extracted_need": "Demande de facture", "matched_feature_id": null}'
    )
    before_count = temp_db.execute("SELECT COUNT(*) FROM features").fetchone()[0]
    fb = temp_db.execute("SELECT * FROM feedbacks WHERE id = 5").fetchone()
    res = ingest_single_feedback(temp_db, fb)
    after_count = temp_db.execute("SELECT COUNT(*) FROM features").fetchone()[0]

    assert res["category"] == "SUPPORT_NOISE"
    assert before_count == after_count


def test_emerging_feature_creation_on_unmatched_need(temp_db, monkeypatch):
    monkeypatch.setattr(
        agent_po, "safe_llm_invoke",
        lambda prompt: json.dumps({
            "category": "FEATURE_REQUEST",
            "sentiment": "NEGATIVE",
            "extracted_need": "Mode hors-ligne sur mobile",
            "matched_feature_id": None,
            "new_feature_title": "Mode Hors-Ligne Mobile & Synchronisation",
            "new_feature_problem": "Perte de saisie dans les zones sans réseau."
        })
    )
    fb = temp_db.execute("SELECT * FROM feedbacks WHERE segment = 'Enterprise' LIMIT 1").fetchone()
    ingest_single_feedback(temp_db, fb)
    new_feat = temp_db.execute("SELECT * FROM features WHERE title LIKE '%Hors-Ligne%'").fetchone()
    assert new_feat is not None
    assert new_feat["reach"] == 3.0  # Enterprise weight = 3