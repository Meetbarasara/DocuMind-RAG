import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Project root = one level up from src/logger.py
_PROJECT_ROOT = Path(__file__).parent.parent
_LOGS_DIR = _PROJECT_ROOT / "logs"
_LOGS_DIR.mkdir(exist_ok=True)

LOG_FILE_PATH = str(_LOGS_DIR / "app.log")

# ── Handlers ──────────────────────────────────────────────────────────────

_LOG_FORMAT = "%(asctime)s | %(levelname)s | %(name)s | %(message)s"

_rotating_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=10 * 1024 * 1024,  # 10 MB per file
    backupCount=5,
    encoding="utf-8",
)
_rotating_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(logging.Formatter(_LOG_FORMAT))

# ── Root logger configuration ─────────────────────────────────────────────

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _rotating_handler],
)


def get_logger(name: str) -> logging.Logger:
    """Return a named logger that inherits the root configuration."""
    return logging.getLogger(name)