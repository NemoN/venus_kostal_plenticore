import logging
from logging.handlers import RotatingFileHandler

LOG_FILE = "/data/venus_kostal_plenticore/kostal.log"
LOG_MAX_BYTES = 5 * 1024 * 1024  # 5 MB
LOG_BACKUP_COUNT = 3

logger = logging.getLogger("kostal-plenticore")
logger.setLevel(logging.DEBUG)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")

file_handler = RotatingFileHandler(LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)
logger.addHandler(file_handler)

console_handler = logging.StreamHandler()
console_handler.setFormatter(formatter)
console_handler.setLevel(logging.DEBUG)
logger.addHandler(console_handler)


def set_log_level(level_str):
    """Set file handler log level from config string (DEBUG, INFO, WARNING, ERROR)."""
    level = getattr(logging, level_str.upper(), None)
    if level is not None:
        file_handler.setLevel(level)
        logger.info('Log level set to ' + level_str.upper())
    else:
        logger.warning('Unknown log level: ' + level_str)
