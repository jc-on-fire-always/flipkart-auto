"""Notification service orchestrating multi-channel alerts and fallback delivery."""

from __future__ import annotations

import logging
from typing import List, Optional

from app.config import Settings
from app.database import DatabaseManager
from app.models import (
    NotificationChannel,
    NotificationResult,
    NotificationStatus,
    RestockEvent,
)
from app.notifications.base import BaseNotifier
from app.notifications.email import EmailNotifier
from app.notifications.whatsapp import WhatsAppNotifier

logger = logging.getLogger(__name__)


class NotificationService:
    """
    Coordinates alert delivery across configured notification channels.

    Guarantees:
      - Automatic fallback to Email if WhatsApp fails and Email is configured.
      - Persistence of all delivery attempts and provider statuses.
      - Duplicate message suppression.
    """

    def __init__(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        whatsapp_notifier: Optional[BaseNotifier] = None,
        email_notifier: Optional[BaseNotifier] = None,
    ):
        self._settings = settings
        self._db = db_manager
        self._whatsapp = whatsapp_notifier or WhatsAppNotifier(settings)
        self._email = email_notifier or EmailNotifier(settings)

    def dispatch_restock_alert(
        self, event: RestockEvent, restock_event_id: Optional[int] = None
    ) -> List[NotificationResult]:
        """Dispatch restock alert across configured channels with automatic fallback."""
        results: List[NotificationResult] = []
        channel_mode = self._settings.NOTIFICATION_CHANNELS

        whatsapp_attempted = False
        whatsapp_succeeded = False

        # Attempt WhatsApp if requested or both
        if channel_mode in ("whatsapp", "both"):
            whatsapp_attempted = True
            try:
                res = self._whatsapp.send_restock_alert(event)
                results.append(res)
                self._db.record_notification_log(restock_event_id, res)
                if res.status in (NotificationStatus.ACCEPTED, NotificationStatus.DELIVERED):
                    whatsapp_succeeded = True
            except Exception as exc:
                logger.error("Unhandled exception dispatching WhatsApp alert: %s", exc)
                fail_res = NotificationResult(
                    channel=NotificationChannel.WHATSAPP,
                    status=NotificationStatus.FAILED,
                    error_message=str(exc),
                )
                results.append(fail_res)
                self._db.record_notification_log(restock_event_id, fail_res)

        # Trigger Email if configured for 'both', 'email', OR as fallback when WhatsApp fails
        need_email = False
        if channel_mode in ("email", "both"):
            need_email = True
        elif channel_mode == "whatsapp" and not whatsapp_succeeded and self._settings.is_email_configured():
            logger.warning("WhatsApp alert delivery failed; triggering configured Email fallback.")
            need_email = True

        if need_email:
            try:
                res = self._email.send_restock_alert(event)
                results.append(res)
                self._db.record_notification_log(restock_event_id, res)
            except Exception as exc:
                logger.error("Unhandled exception dispatching Email alert: %s", exc)
                fail_res = NotificationResult(
                    channel=NotificationChannel.EMAIL,
                    status=NotificationStatus.FAILED,
                    error_message=str(exc),
                )
                results.append(fail_res)
                self._db.record_notification_log(restock_event_id, fail_res)

        # Update service health if at least one channel accepted delivery
        any_success = any(r.status in (NotificationStatus.ACCEPTED, NotificationStatus.DELIVERED) for r in results)
        if any_success:
            self._db.update_health(is_success=True, notification_success=True)

        return results

    def dispatch_test_notifications(self) -> List[NotificationResult]:
        """Send diagnostic test messages across configured channels."""
        results: List[NotificationResult] = []
        mode = self._settings.NOTIFICATION_CHANNELS

        if mode in ("whatsapp", "both"):
            logger.info("Sending WhatsApp test notification...")
            res = self._whatsapp.send_test_message()
            results.append(res)

        if mode in ("email", "both"):
            logger.info("Sending Email test notification...")
            res = self._email.send_test_message()
            results.append(res)

        return results
