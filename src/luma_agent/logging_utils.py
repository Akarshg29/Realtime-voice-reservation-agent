"""Structured JSON logging with PII masking.

Every meaningful event (tool call, API request, turn latency, handoff) is
emitted as a single-line JSON object so logs are grep-able locally and
ingestible by any log pipeline (CloudWatch, Loki, Datadog) in production.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from typing import Any

_PHONE_RE = re.compile(r"(\+?\d[\d\-\s().]{5,}\d)")


def mask_phone(value: str | None) -> str | None:
    """Mask all but the last 4 digits of a phone number for logs."""
    if not value:
        return value
    digits = re.sub(r"\D", "", value)
    if len(digits) < 4:
        return "****"
    return f"***{digits[-4:]}"


def scrub(obj: Any) -> Any:
    """Recursively mask phone-like fields in a dict/list before logging."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k in {"phone", "customer_phone", "phone_number"} and isinstance(v, str):
                out[k] = mask_phone(v)
            else:
                out[k] = scrub(v)
        return out
    if isinstance(obj, list):
        return [scrub(v) for v in obj]
    if isinstance(obj, str):
        # Best-effort scrub of phone numbers embedded in free text.
        return _PHONE_RE.sub(lambda m: mask_phone(m.group(0)) or "****", obj)
    return obj


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname,
            "logger": record.name,
            "event": record.getMessage(),
        }
        extra = getattr(record, "fields", None)
        if extra:
            payload.update(scrub(extra))
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def get_logger(name: str = "luma", level: str = "INFO", log_file: str = "") -> logging.Logger:
    logger = logging.getLogger(name)
    if getattr(logger, "_luma_configured", False):
        return logger
    logger.setLevel(level.upper())
    logger.propagate = False

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    logger.addHandler(handler)

    if log_file:
        fh = logging.FileHandler(log_file)
        fh.setFormatter(JsonFormatter())
        logger.addHandler(fh)

    logger._luma_configured = True  # type: ignore[attr-defined]
    return logger


def log_event(logger: logging.Logger, event: str, level: str = "INFO", **fields: Any) -> None:
    """Emit a structured event. Extra kwargs land in the JSON payload."""
    logger.log(getattr(logging, level.upper(), logging.INFO), event, extra={"fields": fields})
