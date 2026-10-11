"""Tests for FlipkartMonitor orchestrator lifecycle and state persistence across restarts."""

from app.models import AvailabilityState
from app.monitor import FlipkartMonitor
from app.providers.mock_provider import MockStockProvider


def test_monitor_check_once_cycle(base_settings, db_manager, mock_stock_provider, test_whatsapp_notifier, test_email_notifier):
    """Test single monitor check cycle detecting restock and firing alerts."""
    monitor = FlipkartMonitor(
        settings=base_settings,
        custom_provider=mock_stock_provider,
        custom_whatsapp=test_whatsapp_notifier,
        custom_email=test_email_notifier,
    )

    # 1. First check baselines OUT_OF_STOCK
    mock_stock_provider.set_state(AvailabilityState.OUT_OF_STOCK)
    obs1, event1, notifs1 = monitor.check_once()
    assert obs1.availability == AvailabilityState.OUT_OF_STOCK
    assert event1 is None
    assert len(notifs1) == 0

    # 2. Restock occurs
    mock_stock_provider.set_state(AvailabilityState.IN_STOCK)
    obs2, event2, notifs2 = monitor.check_once()
    assert obs2.availability == AvailabilityState.IN_STOCK
    assert event2 is not None
    assert len(notifs2) == 2  # WhatsApp and Email


def test_restarting_application_preserves_state(base_settings, temp_db_path):
    """Scenario 7: Restarting the application preserves state from persistent storage."""
    # Instance 1: Runs and sets product to OUT_OF_STOCK
    provider1 = MockStockProvider(default_state=AvailabilityState.OUT_OF_STOCK)
    monitor1 = FlipkartMonitor(settings=base_settings, custom_provider=provider1)
    monitor1.check_once()

    # Simulate shutdown and restart: Instance 2 starts with same DATABASE_URL
    provider2 = MockStockProvider(default_state=AvailabilityState.OUT_OF_STOCK)
    monitor2 = FlipkartMonitor(settings=base_settings, custom_provider=provider2)

    # Product in DB should still be OUT_OF_STOCK
    product = monitor2._db_manager.get_product("itm1234567890")
    assert product is not None
    assert product.last_known_availability == AvailabilityState.OUT_OF_STOCK.value

    # Now restock happens on Instance 2
    provider2.set_state(AvailabilityState.IN_STOCK)
    obs, event, _ = monitor2.check_once()
    # It must detect that previous state was OUT_OF_STOCK and fire restock event!
    assert event is not None
    assert event.previous_state == AvailabilityState.OUT_OF_STOCK
    assert event.current_state == AvailabilityState.IN_STOCK
