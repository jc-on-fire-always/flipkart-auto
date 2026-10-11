"""Pytest shared fixtures and test harness."""

import pytest
from app.config import Settings
from app.database import DatabaseManager
from app.models import (
    AvailabilityState,
    NotificationChannel,
    NotificationResult,
    NotificationStatus,
    RestockEvent,
)
from app.notifications.base import BaseNotifier
from app.providers.mock_provider import MockStockProvider


class InMemoryTestNotifier(BaseNotifier):
    """Test notifier capturing dispatch calls in memory."""

    def __init__(self, channel: NotificationChannel, fail_always: bool = False):
        self._channel = channel
        self.fail_always = fail_always
        self.dispatched_events: list[RestockEvent] = []
        self.call_count = 0

    @property
    def channel_name(self) -> NotificationChannel:
        return self._channel

    def send_restock_alert(self, event: RestockEvent) -> NotificationResult:
        self.call_count += 1
        self.dispatched_events.append(event)
        if self.fail_always:
            return NotificationResult(
                channel=self._channel,
                status=NotificationStatus.FAILED,
                error_message="Simulated test provider failure",
                attempt_count=1,
            )
        return NotificationResult(
            channel=self._channel,
            status=NotificationStatus.ACCEPTED,
            provider_message_id=f"test-msg-{self.call_count}",
            attempt_count=1,
        )

    def send_test_message(self) -> NotificationResult:
        self.call_count += 1
        if self.fail_always:
            return NotificationResult(
                channel=self._channel,
                status=NotificationStatus.FAILED,
                error_message="Simulated test failure",
            )
        return NotificationResult(
            channel=self._channel,
            status=NotificationStatus.ACCEPTED,
            provider_message_id="test-msg-diag",
        )


@pytest.fixture
def temp_db_path(tmp_path):
    db_file = tmp_path / "test_monitor.db"
    return f"sqlite:///{db_file}"


@pytest.fixture
def db_manager(temp_db_path):
    return DatabaseManager(temp_db_path)


@pytest.fixture
def base_settings(temp_db_path):
    return Settings(
        _env_file=None,
        PRODUCT_NAME="Test Flipkart Product",
        PRODUCT_URL="https://www.flipkart.com/test-product/p/itm1234567890",
        POLL_INTERVAL_SECONDS=5,
        NOTIFICATION_CHANNELS="both",
        WHATSAPP_PHONE_NUMBER_ID="123456789",
        WHATSAPP_ACCESS_TOKEN="EAAGTestAccessToken123456",
        WHATSAPP_TO="+919876543210",
        WHATSAPP_MESSAGE_MODE="text",
        SMTP_HOST="smtp.test.com",
        SMTP_PORT=587,
        SMTP_USERNAME="tester",
        SMTP_PASSWORD="secretpassword",
        SMTP_FROM="alerts@test.com",
        EMAIL_TO="user@test.com",
        DATABASE_URL=temp_db_path,
        STOCK_PROVIDER_TYPE="mock",
        ALERT_COOLDOWN_SECONDS=3600,
        INITIAL_ALERT_ON_STARTUP=False,
        VERIFICATION_COUNT=1,
    )


@pytest.fixture
def mock_stock_provider():
    return MockStockProvider(
        default_state=AvailabilityState.OUT_OF_STOCK,
        product_name="Test Flipkart Product",
        price=49999.0,
    )


@pytest.fixture
def test_whatsapp_notifier():
    return InMemoryTestNotifier(NotificationChannel.WHATSAPP)


@pytest.fixture
def test_email_notifier():
    return InMemoryTestNotifier(NotificationChannel.EMAIL)
