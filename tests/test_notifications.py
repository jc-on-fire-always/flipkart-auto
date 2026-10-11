"""Tests for notification formatting, fallback logic, and duplicate suppression."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models import (
    AvailabilityState,
    NotificationChannel,
    NotificationStatus,
    RestockEvent,
)
from app.notifications.email import EmailNotifier
from app.notifications.whatsapp import WhatsAppNotifier
from app.services.notification_service import NotificationService


def test_whatsapp_message_format_and_link(base_settings):
    """Verify WhatsApp message text contains required headers, timestamp, and direct link."""
    notifier = WhatsAppNotifier(base_settings)
    event = RestockEvent(
        product_id="itm12345",
        product_name="Flipkart Smart TV",
        product_url="https://www.flipkart.com/tv/p/itm12345",
        detected_at_utc=datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc),
        price=29999.0,
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )

    text = notifier._build_text_message(
        product_name=event.product_name,
        detected_at="2026-10-10 12:00:00 UTC",
        price="₹29,999.00",
        product_url=event.product_url,
    )

    assert "FLIPKART RESTOCK ALERT" in text
    assert "Product: Flipkart Smart TV" in text
    assert "Status: Reported IN STOCK" in text
    assert "Detected at: 2026-10-10 12:00:00 UTC" in text
    assert "Price: ₹29,999.00" in text
    assert "Open the product here:\nhttps://www.flipkart.com/tv/p/itm12345" in text
    assert "Availability may change before checkout" in text


def test_email_format_and_clickable_html(base_settings):
    """Verify Email contains subject, plain text, and HTML link."""
    notifier = EmailNotifier(base_settings)
    event = RestockEvent(
        product_id="itm12345",
        product_name="Flipkart Smart TV",
        product_url="https://www.flipkart.com/tv/p/itm12345",
        detected_at_utc=datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc),
        price=29999.0,
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )

    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value = mock_instance

        res = notifier.send_restock_alert(event)
        assert res.status == NotificationStatus.ACCEPTED

        # Check sent message body using standard email parser
        sent_args = mock_instance.sendmail.call_args[0]
        raw_msg = sent_args[2]

        import email
        from email.header import decode_header, make_header

        msg_obj = email.message_from_string(raw_msg)
        decoded_subject = str(make_header(decode_header(msg_obj["Subject"])))
        assert decoded_subject == "Flipkart Restock Alert — Flipkart Smart TV"

        body_parts = [
            part.get_payload(decode=True).decode("utf-8")
            for part in msg_obj.walk()
            if part.get_content_type() in ("text/plain", "text/html")
        ]
        combined_body = "\n".join(body_parts)
        assert 'href="https://www.flipkart.com/tv/p/itm12345"' in combined_body
        assert "View Product on Flipkart" in combined_body


def test_failed_whatsapp_triggers_email_fallback(base_settings, db_manager, test_whatsapp_notifier, test_email_notifier):
    """Scenario 8: A failed WhatsApp delivery triggers configured email fallback."""
    base_settings.NOTIFICATION_CHANNELS = "whatsapp"
    test_whatsapp_notifier.fail_always = True

    service = NotificationService(
        settings=base_settings,
        db_manager=db_manager,
        whatsapp_notifier=test_whatsapp_notifier,
        email_notifier=test_email_notifier,
    )

    db_manager.get_or_create_product(
        product_id="itm123", name="Product", url="https://www.flipkart.com/p/itm123"
    )

    event = RestockEvent(
        product_id="itm123",
        product_name="Product",
        product_url="https://www.flipkart.com/p/itm123",
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )
    event_id = db_manager.record_restock_event(event, cooldown_seconds=3600)
    event.id = event_id

    results = service.dispatch_restock_alert(event, restock_event_id=event_id)

    assert len(results) == 2
    assert results[0].channel == NotificationChannel.WHATSAPP
    assert results[0].status == NotificationStatus.FAILED
    # Email fallback was triggered
    assert results[1].channel == NotificationChannel.EMAIL
    assert results[1].status == NotificationStatus.ACCEPTED
    assert test_email_notifier.call_count == 1


def test_duplicate_notification_prevention(base_settings, db_manager, test_whatsapp_notifier, test_email_notifier):
    """Scenario 10: Duplicate notifications are prevented during cooldown."""
    base_settings.ALERT_COOLDOWN_SECONDS = 3600
    service = NotificationService(
        settings=base_settings,
        db_manager=db_manager,
        whatsapp_notifier=test_whatsapp_notifier,
        email_notifier=test_email_notifier,
    )

    db_manager.get_or_create_product(
        product_id="itm123", name="Product", url="https://www.flipkart.com/p/itm123"
    )

    event = RestockEvent(
        product_id="itm123",
        product_name="Product",
        product_url="https://www.flipkart.com/p/itm123",
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )
    event_id = db_manager.record_restock_event(event, cooldown_seconds=3600)
    event.id = event_id

    # Initial dispatch
    service.dispatch_restock_alert(event, restock_event_id=event_id)
    assert test_whatsapp_notifier.call_count == 1
    assert test_email_notifier.call_count == 1

    # In cooldown check
    assert db_manager.is_in_cooldown("itm123") is True
