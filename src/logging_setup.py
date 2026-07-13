"""Strukturiertes JSON-Logging, kompatibel mit Google Cloud Logging.

Cloud Logging parst JSON-Zeilen auf stdout/stderr automatisch; das Feld
``severity`` steuert dort den Log-Level, ``message`` den Anzeigetext.
Zusätzliche ``extra={...}``-Felder aus Log-Aufrufen werden mit ausgegeben
(z. B. connector, region, run_id) und sind in Cloud Logging filterbar.
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone

#: Standard-Attribute eines LogRecord — alles andere gilt als "extra" und
#: wird mit in die JSON-Zeile geschrieben.
_STANDARD_ATTRS = {
    "name", "msg", "args", "levelname", "levelno", "pathname", "filename",
    "module", "exc_info", "exc_text", "stack_info", "lineno", "funcName",
    "created", "msecs", "relativeCreated", "thread", "threadName",
    "processName", "process", "taskName",
}


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "severity": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
            "timestamp": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
        }
        for key, value in record.__dict__.items():
            if key not in _STANDARD_ATTRS and not key.startswith("_"):
                payload[key] = value
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, default=str)


def setup_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # Rauschige Bibliotheks-Logger dämpfen
    for noisy in ("urllib3", "pdfminer", "google"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
