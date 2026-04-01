import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Use project root (two levels up from src/logger.py) to avoid CWD-dependent paths
_PROJECT_ROOT = Path(__file__).parent.parent
logs_path = str(_PROJECT_ROOT / "logs")
os.makedirs(logs_path, exist_ok=True)

LOG_FILE_PATH = os.path.join(logs_path, "app.log")

# P5 fix: RotatingFileHandler — max 10 MB per file, keep 5 backups, no disk flood
_rotating_handler = RotatingFileHandler(
    LOG_FILE_PATH,
    maxBytes=10 * 1024 * 1024,  # 10 MB
    backupCount=5,
    encoding="utf-8",
)
_rotating_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)

_stream_handler = logging.StreamHandler()
_stream_handler.setFormatter(
    logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
)

logging.basicConfig(
    level=logging.INFO,
    handlers=[_stream_handler, _rotating_handler],
)


def get_logger(name: str):
    return logging.getLogger(name)