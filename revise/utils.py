import hashlib
import json
import logging


def hash_params(params):
    return hashlib.sha256(
        json.dumps(params, sort_keys=True).encode("utf-8")
    ).hexdigest()


def configure_logging(level="info"):
    if level == "info":
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    elif level == "debug":
        logging.basicConfig(
            level=logging.DEBUG,
            format="%(asctime)s - %(levelname)s - %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    logger = logging.getLogger(__name__)
    return logger
