"""Structured logging configuration with automatic sensitive data redaction."""

from __future__ import annotations

import logging
import re
import sys


class SensitiveDataFilter(logging.Filter):
    """Logging filter that scrubs secrets, access tokens, and passwords from log records."""

    # Patterns for sensitive keys and values
    SENSITIVE_PATTERNS = [
        # Bearer tokens and Meta Graph tokens
        (re.compile(r"(Bearer\s+)[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
        (re.compile(r"(access_token[:=]\s*)[A-Za-z0-9_\-\.]{10,}", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
        # Passwords
        (re.compile(r"((?:password|passwd|pwd)[:=]\s*)[^\s,;&]+", re.IGNORECASE), r"\1[REDACTED_PASSWORD]"),
        # Affiliate tokens
        (re.compile(r"((?:affiliate_token|Fk-Affiliate-Token)[:=]\s*)[^\s,;&]+", re.IGNORECASE), r"\1[REDACTED_TOKEN]"),
        # Secret authorization headers
        (re.compile(r"(Authorization:\s*)[^\s,;]+", re.IGNORECASE), r"\1[REDACTED_HEADER]"),
    ]

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self._redact(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self._redact(v) if isinstance(v, str) else v for k, v in record.args.items()}
            elif isinstance(record.args, tuple):
                record.args = tuple(self._redact(v) if isinstance(v, str) else v for v in record.args)
        return True

    @classmethod
    def _redact(cls, text: str) -> str:
        for pattern, replacement in cls.SENSITIVE_PATTERNS:
            text = pattern.sub(replacement, text)
        return text


def setup_logging(level: str = "INFO", log_file: str | None = None) -> None:
    """Configure root logger with sensitive data filtering and uniform UTC formatting."""
    log_level = getattr(logging, level.upper(), logging.INFO)

    formatter = logging.Formatter(
        fmt="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Avoid duplicate handlers on re-configuration
    for handler in list(root_logger.handlers):
        root_logger.removeHandler(handler)

    redaction_filter = SensitiveDataFilter()

    # Console Handler
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.addFilter(redaction_filter)
    root_logger.addHandler(console_handler)

    # Optional File Handler
    if log_file:
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(formatter)
        file_handler.addFilter(redaction_filter)
        root_logger.addHandler(file_handler)

    # Silence noisy third-party loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("sqlalchemy.engine").setLevel(logging.WARNING)
