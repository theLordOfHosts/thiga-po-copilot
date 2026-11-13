import os
from dotenv import load_dotenv

load_dotenv()

APP_VERSION = "1.0.0"
LLM_MODEL = os.getenv("LLM_MODEL", "gemini-3.8-flash")
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "po_copilot.db")
SEED_PATH = os.path.join(BASE_DIR, "data", "seed_dataset.json")

# Strict scales for deterministic validation
IMPACT_SCALE = (0.25, 0.5, 1.0, 2.0, 3.0)
CONFIDENCE_SCALE = (0.5, 0.8, 1.0)
FIBONACCI_SCALE = (1, 2, 3, 5, 8, 13)
MOSCOW_VALUES = ("Must Have", "Should Have", "Could Have", "Won't Have")
FEEDBACK_CATEGORIES = ("FEATURE_REQUEST", "UX_FRICTION", "BUG", "SUPPORT_NOISE")

SEGMENT_WEIGHTS = {
    "Enterprise": 3,
    "Scale-up": 2,
    "SMB": 1
}


def get_api_keys():
    """Load Gemini keys from key.txt (multi-key rotation) or fallback to .env."""
    key_file = os.path.join(BASE_DIR, "key.txt")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            keys = [line.strip() for line in f.readlines() if line.strip()]
        if keys:
            return keys
    env_key = os.getenv("GEMINI_API_KEY", "").strip()
    return [env_key] if env_key else []


def get_telegram_credentials():
    """Load Telegram credentials from telegram_keys.py or fallback to .env."""
    try:
        from telegram_keys import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID
        return TELEGRAM_BOT_TOKEN, str(TELEGRAM_CHAT_ID)
    except ImportError:
        return os.getenv("TELEGRAM_BOT_TOKEN", ""), str(os.getenv("TELEGRAM_CHAT_ID", ""))