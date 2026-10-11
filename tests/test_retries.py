"""Tests for transient error resilience, retries, and network fault isolation."""

from unittest.mock import MagicMock, patch
from app.models import AvailabilityState
from app.notifications.email import EmailNotifier
from app.notifications.whatsapp import WhatsAppNotifier
from app.services.stock_service import StockService


def test_network_timeout_does_not_corrupt_last_valid_state(base_settings, db_manager, mock_stock_provider):
    """Scenario 5: Network timeouts do not alter or overwrite previously known valid state."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    # 1. Establish valid OUT_OF_STOCK state
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    product = db_manager.get_product("itm1234567890")
    assert product.last_known_availability == AvailabilityState.OUT_OF_STOCK.value

    # 2. Inject network timeout
    mock_stock_provider.trigger_network_error_next(True)
    obs, event = service.execute_stock_check()

    # Must return None and not raise unhandled exception
    assert obs is None
    assert event is None

    # Database state must still be OUT_OF_STOCK, not corrupted
    product_after = db_manager.get_product("itm1234567890")
    assert product_after.last_known_availability == AvailabilityState.OUT_OF_STOCK.value


def test_malformed_responses_do_not_create_false_alerts(base_settings, db_manager, mock_stock_provider):
    """Scenario 6: Malformed or unexpected JSON responses do not trigger false alerts."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    service.execute_stock_check()

    # Inject malformed response
    mock_stock_provider.trigger_malformed_error_next(True)
    obs, event = service.execute_stock_check()

    assert obs is None
    assert event is None

    # State remains intact
    product = db_manager.get_product("itm1234567890")
    assert product.last_known_availability == AvailabilityState.OUT_OF_STOCK.value


def test_email_retries_transient_failures(base_settings):
    """Scenario 9: Email attempts bounded retries on transient SMTP errors."""
    base_settings.MAX_RETRIES = 2
    notifier = EmailNotifier(base_settings)

    with patch("smtplib.SMTP") as mock_smtp_cls, patch("time.sleep") as mock_sleep:
        # Mock SMTP instance raising OSError on each connect attempt
        mock_instance = MagicMock()
        mock_instance.sendmail.side_effect = OSError("Connection refused")
        mock_smtp_cls.return_value = mock_instance

        res = notifier.send_test_message()

        assert res.status.value == "FAILED"
        assert res.attempt_count == 2
        # Verify sleep was called for backoff
        assert mock_sleep.call_count >= 1


def test_whatsapp_retries_transient_500_errors(base_settings):
    """WhatsApp retries bounded 5xx server errors with backoff."""
    base_settings.MAX_RETRIES = 2
    notifier = WhatsAppNotifier(base_settings)

    with patch("requests.post") as mock_post, patch("time.sleep") as mock_sleep:
        mock_resp = MagicMock()
        mock_resp.status_code = 503
        mock_resp.text = "Service Unavailable"
        mock_post.return_value = mock_resp

        res = notifier.send_test_message()

        assert res.status.value == "FAILED"
        assert res.attempt_count == 2
        assert mock_post.call_count == 2
