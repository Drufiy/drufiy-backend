"""
JSON structured logging for Cloud Run.
Cloud Run's log viewer parses structured JSON natively — every field becomes
a searchable column. This replaces the default plaintext format.
"""
import json
import logging
import sys
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        log: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "severity": record.levelname,          # Cloud Run uses 'severity' not 'level'
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach structured exception info if present
        if record.exc_info:
            log["exception"] = self.formatException(record.exc_info)

        # Attach any extra fields passed via logger.info(..., extra={...})
        for key, val in record.__dict__.items():
            if key not in (
                "name", "msg", "args", "levelname", "levelno", "pathname",
                "filename", "module", "exc_info", "exc_text", "stack_info",
                "lineno", "funcName", "created", "msecs", "relativeCreated",
                "thread", "threadName", "processName", "process", "message",
                "taskName",
            ):
                log[key] = val

        return json.dumps(log, default=str)


def configure_logging() -> None:
    """
    Replace the root logger's handlers with a single JSON-to-stdout handler.
    Call once at the top of main.py before anything else.
    """
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)

    # Silence noisy third-party loggers
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
