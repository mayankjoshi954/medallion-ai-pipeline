import logging
import sys

from src.db import ROOT

_FORMAT = "%(asctime)s %(levelname)-7s [%(name)s] %(message)s"


def configure_logging() -> None:
    """Log to stdout and logs/pipeline.log. Idempotent: safe to call more than once."""
    root = logging.getLogger()
    if root.handlers:
        return
    (ROOT / "logs").mkdir(exist_ok=True)
    root.setLevel(logging.INFO)
    for handler in (logging.StreamHandler(sys.stdout), logging.FileHandler(ROOT / "logs" / "pipeline.log")):
        handler.setFormatter(logging.Formatter(_FORMAT))
        root.addHandler(handler)
    for noisy in ("httpx", "httpx2"):  # the LLM SDKs log every request at INFO
        logging.getLogger(noisy).setLevel(logging.WARNING)
