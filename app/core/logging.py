import logging
import sys

from app.core.config import get_settings

_configured = False


def setup_logging() -> None:
    global _configured
    if _configured:
        return

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-7s [%(name)s] %(message)s")
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(get_settings().log_level.upper())

    logging.getLogger("uvicorn.access").propagate = False
    _configured = True
