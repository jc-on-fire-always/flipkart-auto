"""Abstract base class for notification channel dispatchers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from app.models import NotificationChannel, NotificationResult, RestockEvent


class BaseNotifier(ABC):
    """Abstract contract for alert notification dispatchers."""

    @property
    @abstractmethod
    def channel_name(self) -> NotificationChannel:
        """Name of the notification channel (whatsapp or email)."""
        pass

    @abstractmethod
    def send_restock_alert(self, event: RestockEvent) -> NotificationResult:
        """
        Send restock alert message containing product details and direct link.

        Must return a NotificationResult with ACCEPTED or DELIVERED status,
        or FAILED status if all bounded retries are exhausted.
        """
        pass

    @abstractmethod
    def send_test_message(self) -> NotificationResult:
        """Send a diagnostic test message to verify credentials and routing."""
        pass
