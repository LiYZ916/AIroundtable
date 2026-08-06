from __future__ import annotations

import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from app.services.privacy import redact_sensitive_text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_sensitive_text(str(record.msg))
        if record.args:
            record.args = tuple(redact_sensitive_text(str(arg)) for arg in record.args)
        return True


def configure_logging(log_directory: str | Path) -> logging.Logger:
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger("ai_roundtable")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    handler = RotatingFileHandler(
        directory / "application.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
    )
    handler.addFilter(RedactingFilter())
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger


SAFE_EVENT_PAYLOAD_KEYS = {
    "attempt",
    "retry_count",
    "elapsed_seconds",
    "error",
    "actions",
    "discussion_id",
}


def record_engine_event(
    log_directory: str | Path,
    discussion_id: str,
    event: Any,
) -> Path | None:
    """Append a privacy-safe engine event without prompts or answer bodies."""
    if not discussion_id or getattr(event, "transient", False):
        return None
    directory = Path(log_directory)
    directory.mkdir(parents=True, exist_ok=True)
    safe_payload = {
        key: redact_sensitive_text(str(value))
        for key, value in getattr(event, "payload", {}).items()
        if key in SAFE_EVENT_PAYLOAD_KEYS
    }
    record = {
        "created_at": event.created_at.isoformat(),
        "discussion_id": discussion_id,
        "event_type": event.event_type,
        "stage": event.stage.value,
        "provider_name": event.provider_name,
        "status": event.status.value,
        "call_id": event.call_id,
        "message": redact_sensitive_text(event.message),
        "metrics": safe_payload,
    }
    path = directory / f"run_{discussion_id}.jsonl"
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    logging.getLogger("ai_roundtable").info(
        "run=%s event=%s stage=%s provider=%s status=%s call=%s",
        discussion_id,
        event.event_type,
        event.stage.value,
        event.provider_name or "-",
        event.status.value,
        event.call_id or "-",
    )
    return path
