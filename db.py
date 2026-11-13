import json
import sqlite3
from config import DB_PATH, SEED_PATH, IMPACT_SCALE, CONFIDENCE_SCALE

ROW_VERSION_TRIGGER = """
CREATE TRIGGER IF NOT EXISTS trg_features_row_version AFTER UPDATE ON features
WHEN NEW.row_version = OLD.row_version AND (
    NEW.status IS NOT OLD.status OR
    NEW.reach IS NOT OLD.reach OR
    NEW.impact IS NOT OLD.impact OR
    NEW.confidence IS NOT OLD.confidence OR
    NEW.effort IS NOT OLD.effort OR
    NEW.moscow IS NOT OLD.moscow OR
    NEW.rice_score IS NOT OLD.rice_score
)
BEGIN
    UPDATE features SET row_version = OLD.row_version + 1, updated_at = CURRENT_TIMESTAMP WHERE id = NEW.id;
END;
"""


def snap_to_scale(val, scale):
    """Snap any float/int to the closest allowed value in a strict rubric scale."""
    try:
        num = float(val)
    except (TypeError, ValueError):
        return scale[0]
    return min(scale, key=lambda x: abs(x - num))


def calculate_rice(reach, impact, confidence, effort):
    """Deterministic RICE calculation: (Reach * Impact * Confidence) / Effort."""
    r = max(0.0, float(reach if reach is not None else 0.0))
    i = snap_to_scale(impact, IMPACT_SCALE)
    c = snap_to_scale(confidence, CONFIDENCE_SCALE)
    e = max(0.5, float(effort if effort is not None else 1.0))
    return round((r * i * c) / e, 2)


def get_conn(db_path=None):
    conn = sqlite3.connect(db_path or DB_PATH, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("PRAGMA busy_timeout=30000;")
    return conn


def init_db(db_path=None):
    conn = get_conn(db_path)
    cur = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS features (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            title TEXT UNIQUE NOT NULL,
            problem_statement TEXT,
            evidence_count INTEGER DEFAULT 1,
            reach REAL DEFAULT 1.0,
            impact REAL DEFAULT 1.0,
            confidence REAL DEFAULT 0.8,
            effort REAL DEFAULT 2.0,
            rice_score REAL DEFAULT 0.4,
            moscow TEXT DEFAULT 'Should Have',
            rationale TEXT,
            status TEXT DEFAULT 'candidate',
            row_version INTEGER NOT NULL DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS feedbacks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT NOT NULL,
            customer_name TEXT,
            segment TEXT DEFAULT 'SMB',
            raw_text TEXT NOT NULL,
            category TEXT DEFAULT 'UNCLASSIFIED',
            sentiment TEXT DEFAULT 'NEUTRAL',
            extracted_need TEXT,
            feature_id INTEGER,
            processed INTEGER DEFAULT 0,
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(feature_id) REFERENCES features(id)
        )
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS user_stories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            feature_id INTEGER UNIQUE NOT NULL,
            story_text TEXT NOT NULL,
            acceptance_criteria TEXT NOT NULL,
            story_points INTEGER NOT NULL,
            tshirt_size TEXT NOT NULL,
            mvp_slice_suggestion TEXT,
            status TEXT DEFAULT 'drafted',
            created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY(feature_id) REFERENCES features(id)
        )
    """)

    cur.execute(ROW_VERSION_TRIGGER)
    conn.commit()
    return conn


def cas_update_feature(conn, feature_id, expected_version, assignments, params):
    """Compare-and-set update on features using row_version to prevent stale overwrites."""
    cur = conn.cursor()
    cur.execute(
        f"UPDATE features SET {assignments} WHERE id = ? AND row_version = ?",
        tuple(params) + (feature_id, expected_version)
    )
    conn.commit()
    return cur.rowcount > 0


def seed_db(db_path=None, reset=True):
    conn = init_db(db_path)
    cur = conn.cursor()

    if reset:
        cur.execute("DELETE FROM user_stories")
        cur.execute("DELETE FROM feedbacks")
        cur.execute("DELETE FROM features")

    with open(SEED_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    for feat in data.get("existing_features", []):
        rice = calculate_rice(feat["reach"], feat["impact"], feat["confidence"], feat["effort"])
        cur.execute("""
            INSERT OR IGNORE INTO features (
                title, problem_statement, evidence_count, reach, impact, confidence, effort, rice_score, moscow, rationale, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'prioritized')
        """, (
            feat["title"], feat["problem_statement"], 1,
            feat["reach"], feat["impact"], feat["confidence"], feat["effort"],
            rice, feat["moscow"], feat["rationale"]
        ))

    for fb in data.get("feedbacks", []):
        cur.execute("""
            INSERT INTO feedbacks (channel, customer_name, segment, raw_text, processed)
            VALUES (?, ?, ?, ?, 0)
        """, (fb["channel"], fb["customer_name"], fb["segment"], fb["raw_text"]))

    conn.commit()
    return conn