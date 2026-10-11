"""Tests for logging security filters and secret redaction."""

import io
import logging
from app.logging_config import SensitiveDataFilter


def test_sensitive_tokens_redacted():
    """Scenario 12: Secrets and access tokens are scrubbed from log output."""
    raw_messages = [
        "Sending request with Authorization: Bearer EAAG1234567890abcdefABCDEF12345 to Meta API",
        "Configuring SMTP with password: MySuperSecretPassword123!",
        "Querying Affiliate API with Fk-Affiliate-Token: fk_token_xyz987654321",
        "Meta API response: access_token=EAAG9999999999secret123456",
    ]

    filter_instance = SensitiveDataFilter()

    for msg in raw_messages:
        record = logging.LogRecord(
            name="test_logger",
            level=logging.INFO,
            pathname=__file__,
            lineno=10,
            msg=msg,
            args=(),
            exc_info=None,
        )
        filter_instance.filter(record)

        # Assert no sensitive secrets leak into formatted message
        assert "EAAG1234567890abcdefABCDEF12345" not in record.msg
        assert "MySuperSecretPassword123!" not in record.msg
        assert "fk_token_xyz987654321" not in record.msg
        assert "EAAG9999999999secret123456" not in record.msg

        # Assert redaction tokens are placed
        assert "[REDACTED" in record.msg


def test_logging_stream_redacts_credentials():
    """Verify that end-to-end stream handler scrub operates on root logger."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    handler.addFilter(SensitiveDataFilter())

    logger = logging.getLogger("test_security_stream")
    logger.setLevel(logging.INFO)
    logger.addHandler(handler)

    logger.info("Attempting login with password: SecretP@ssw0rd999 to server")
    output = stream.getvalue()

    assert "SecretP@ssw0rd999" not in output
    assert "[REDACTED_PASSWORD]" in output
