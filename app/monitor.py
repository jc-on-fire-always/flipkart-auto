"""Core 24/7 background monitor daemon coordinating stock checking and alerts."""

from __future__ import annotations

import logging
import signal
import threading
from typing import Optional, Tuple, List

from app.config import Settings
from app.database import DatabaseManager
from app.health import HealthServer
from app.models import NotificationResult, RestockEvent, StockObservation
from app.notifications.base import BaseNotifier
from app.providers.base import StockProvider
from app.providers.factory import get_stock_provider
from app.scheduler import PollingScheduler
from app.services.notification_service import NotificationService
from app.services.stock_service import StockService

logger = logging.getLogger(__name__)


class FlipkartMonitor:
    """
    Continuous 24/7 background worker monitoring product availability.

    Coordinates database persistence, provider inspection, transition logic,
    multi-channel notifications, and health monitoring.
    """

    def __init__(
        self,
        settings: Settings,
        custom_provider: Optional[StockProvider] = None,
        custom_whatsapp: Optional[BaseNotifier] = None,
        custom_email: Optional[BaseNotifier] = None,
    ):
        self._settings = settings
        self._db_manager = DatabaseManager(settings.DATABASE_URL)
        self._provider = get_stock_provider(settings, custom_mock=custom_provider)
        self._stock_service = StockService(settings, self._db_manager, self._provider)
        self._notification_service = NotificationService(
            settings=settings,
            db_manager=self._db_manager,
            whatsapp_notifier=custom_whatsapp,
            email_notifier=custom_email,
        )
        self._health_server = HealthServer(
            host=settings.HEALTH_HOST,
            port=settings.HEALTH_PORT,
            db_manager=self._db_manager,
        )
        self._stop_event = threading.Event()
        self._scheduler = PollingScheduler(
            interval_seconds=settings.POLL_INTERVAL_SECONDS,
            stop_event=self._stop_event,
        )

    def check_once(self) -> Tuple[Optional[StockObservation], Optional[RestockEvent], List[NotificationResult]]:
        """Perform a single isolated stock check and dispatch alert if a restock is detected."""
        obs, restock_event = self._stock_service.execute_stock_check()
        notifications: List[NotificationResult] = []

        if restock_event:
            notifications = self._notification_service.dispatch_restock_alert(
                restock_event, restock_event_id=restock_event.id
            )

        return obs, restock_event, notifications

    def run(self) -> None:
        """Run the continuous 24/7 monitoring loop."""
        self._setup_signals()
        self._health_server.start()

        logger.info("=" * 60)
        logger.info("Flipkart 24/7 Restock Monitor started successfully.")
        logger.info("Target Product : %s", self._settings.PRODUCT_NAME)
        logger.info("Product URL    : %s", self._settings.PRODUCT_URL)
        logger.info("Active Provider: %s", self._provider.provider_name)
        logger.info("Poll Interval  : %s seconds", self._settings.POLL_INTERVAL_SECONDS)
        logger.info("Alert Channels : %s", self._settings.NOTIFICATION_CHANNELS)
        logger.info("Health Check   : http://%s:%s/health", self._settings.HEALTH_HOST, self._settings.HEALTH_PORT)
        logger.info("=" * 60)

        try:
            while not self._stop_event.is_set():
                try:
                    obs, restock_event, notifs = self.check_once()
                    if obs:
                        logger.debug(
                            "Check completed: Status=%s Price=%s",
                            obs.availability.value,
                            f"₹{obs.price}" if obs.price is not None else "N/A",
                        )
                except Exception as exc:
                    logger.error("Unexpected error in monitoring cycle: %s", exc, exc_info=True)

                # Wait for next cycle or shutdown interrupt
                interrupted = self._scheduler.wait()
                if interrupted:
                    break

        finally:
            logger.info("Initiating graceful shutdown...")
            self._health_server.stop()
            logger.info("Flipkart Restock Monitor stopped cleanly.")

    def stop(self) -> None:
        """Trigger termination of the monitoring loop."""
        self._stop_event.set()

    def _setup_signals(self) -> None:
        """Register POSIX and Windows termination signals."""
        def _handle_signal(signum, frame):
            logger.info("Received termination signal (%s). Stopping monitor...", signum)
            self.stop()

        try:
            signal.signal(signal.SIGINT, _handle_signal)
            signal.signal(signal.SIGTERM, _handle_signal)
        except (ValueError, AttributeError):
            # Not in main thread or signal unsupported on environment
            pass
