from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .config import HsajConfig


class JsonLogFormatter(logging.Formatter):
    def __init__(self, *, service_name: str) -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "service": self._service_name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


def configure_logging(config: HsajConfig) -> None:
    root_logger = logging.getLogger()
    level_name = config.observability.log_level.upper()
    level = getattr(logging, level_name, logging.INFO)
    root_logger.setLevel(level)

    handler = logging.StreamHandler()
    if config.observability.structured_logging:
        handler.setFormatter(JsonLogFormatter(service_name=config.observability.service_name))
    else:
        handler.setFormatter(logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s"))

    root_logger.handlers.clear()
    root_logger.addHandler(handler)
