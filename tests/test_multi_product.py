"""Unit and integration tests for multi-product configuration and monitoring."""

import json
from unittest.mock import MagicMock, patch

import pytest

from app.config import Settings
from app.models import AvailabilityState, MonitoredProduct, StockObservation
from app.monitor import FlipkartMonitor


def test_load_multiple_products_from_json(tmp_path, monkeypatch):
    """Ensure products are loaded from products.json when available."""
    json_path = tmp_path / "products.json"
    data = [
        {
            "name": "Shoes",
            "url": "https://www.flipkart.com/shoes/p/itm1111",
            "max_price": 2000.0,
            "enabled": True,
        },
        {
            "name": "Jeans",
            "url": "https://www.flipkart.com/jeans/p/itm2222",
            "max_price": 1200.0,
            "enabled": False,
        },
    ]
    json_path.write_text(json.dumps(data), encoding="utf-8")

    settings = Settings(
        PRODUCTS_FILE=str(json_path),
        PRODUCT_NAME="Fallback Product",
        PRODUCT_URL="https://www.flipkart.com/fallback/p/itm0000",
        NOTIFICATION_CHANNELS="both",
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+911234567890",
        SMTP_HOST="smtp.example.com",
        SMTP_FROM="alerts@example.com",
        EMAIL_TO="user@example.com",
    )

    # By default, only enabled products are returned
    enabled = settings.get_monitored_products(include_disabled=False)
    assert len(enabled) == 1
    assert enabled[0].name == "Shoes"
    assert enabled[0].max_price == 2000.0

    # With include_disabled=True, all products are returned
    all_prods = settings.get_monitored_products(include_disabled=True)
    assert len(all_prods) == 2
    assert all_prods[1].name == "Jeans"
    assert not all_prods[1].enabled


def test_ssrf_host_rejection_in_products_json(tmp_path):
    """Ensure malicious hosts in products.json trigger SSRF validation error."""
    json_path = tmp_path / "products.json"
    data = [
        {
            "name": "Malicious",
            "url": "http://169.254.169.254/latest/meta-data/",
            "max_price": 100.0,
            "enabled": True,
        }
    ]
    json_path.write_text(json.dumps(data), encoding="utf-8")

    settings = Settings(
        PRODUCTS_FILE=str(json_path),
        PRODUCT_NAME="Safe Fallback",
        PRODUCT_URL="https://www.flipkart.com/safe/p/itm0000",
        NOTIFICATION_CHANNELS="whatsapp",
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+911234567890",
    )

    # SSRF invalid product in file causes fallback to safe environment product
    products = settings.get_monitored_products()
    assert len(products) == 1
    assert products[0].name == "Safe Fallback"


def test_fallback_when_products_file_missing(tmp_path):
    """Ensure settings fall back gracefully to environment PRODUCT_NAME / PRODUCT_URL when file doesn't exist."""
    missing_path = tmp_path / "non_existent.json"
    settings = Settings(
        PRODUCTS_FILE=str(missing_path),
        PRODUCT_NAME="Default Product",
        PRODUCT_URL="https://www.flipkart.com/default/p/itm9999",
        MAX_PRODUCT_PRICE=1500.0,
        NOTIFICATION_CHANNELS="whatsapp",
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+911234567890",
    )

    products = settings.get_monitored_products()
    assert len(products) == 1
    assert products[0].name == "Default Product"
    assert products[0].url == "https://www.flipkart.com/default/p/itm9999"
    assert products[0].max_price == 1500.0


def test_check_all_executes_each_product(tmp_path):
    """Ensure FlipkartMonitor.check_all iterates over all configured products."""
    json_path = tmp_path / "products.json"
    data = [
        {
            "name": "Product 1",
            "url": "https://www.flipkart.com/prod1/p/itm1111",
            "max_price": 1000.0,
            "enabled": True,
        },
        {
            "name": "Product 2",
            "url": "https://www.flipkart.com/prod2/p/itm2222",
            "max_price": 2000.0,
            "enabled": True,
        },
    ]
    json_path.write_text(json.dumps(data), encoding="utf-8")

    settings = Settings(
        DATABASE_URL=f"sqlite:///{tmp_path}/test_multi.db",
        PRODUCTS_FILE=str(json_path),
        REQUEST_STAGGER_SECONDS=0.01,
        NOTIFICATION_CHANNELS="whatsapp",
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+911234567890",
        STOCK_PROVIDER_TYPE="mock",
    )

    mock_provider = MagicMock()
    mock_provider.provider_name = "mock_test"
    mock_provider.extract_product_id.side_effect = lambda url: url.split("/")[-1]
    # Return out of stock for both
    mock_provider.check_availability.side_effect = [
        StockObservation(
            product_id="itm1111",
            product_name="Product 1",
            product_url="https://www.flipkart.com/prod1/p/itm1111",
            availability=AvailabilityState.OUT_OF_STOCK,
            price=899.0,
            provider_name="mock_test",
        ),
        StockObservation(
            product_id="itm2222",
            product_name="Product 2",
            product_url="https://www.flipkart.com/prod2/p/itm2222",
            availability=AvailabilityState.OUT_OF_STOCK,
            price=1500.0,
            provider_name="mock_test",
        ),
    ]

    monitor = FlipkartMonitor(settings=settings, custom_provider=mock_provider)
    results = monitor.check_all()

    assert len(results) == 2
    assert results[0][0].product_id == "itm1111"
    assert results[1][0].product_id == "itm2222"
    assert mock_provider.check_availability.call_count == 2


def test_product_specific_price_ceiling(tmp_path):
    """Ensure individual max_price is evaluated correctly per product."""
    json_path = tmp_path / "products.json"
    data = [
        {
            "name": "Under Budget Product",
            "url": "https://www.flipkart.com/under/p/itm100",
            "max_price": 1000.0,
            "enabled": True,
        },
        {
            "name": "Over Budget Product",
            "url": "https://www.flipkart.com/over/p/itm200",
            "max_price": 500.0,
            "enabled": True,
        },
    ]
    json_path.write_text(json.dumps(data), encoding="utf-8")

    settings = Settings(
        DATABASE_URL=f"sqlite:///{tmp_path}/test_ceiling.db",
        PRODUCTS_FILE=str(json_path),
        REQUEST_STAGGER_SECONDS=0.01,
        NOTIFICATION_CHANNELS="whatsapp",
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+911234567890",
        INITIAL_ALERT_ON_STARTUP=True,
        VERIFICATION_COUNT=1,
    )

    mock_provider = MagicMock()
    mock_provider.provider_name = "mock_test"
    mock_provider.extract_product_id.side_effect = lambda url: url.split("/")[-1]
    # Under budget product price is 800 (under 1000 limit) -> restock event triggered
    # Over budget product price is 700 (over 500 limit) -> suppressed
    mock_provider.check_availability.side_effect = [
        StockObservation(
            product_id="itm100",
            product_name="Under Budget Product",
            product_url="https://www.flipkart.com/under/p/itm100",
            availability=AvailabilityState.IN_STOCK,
            price=800.0,
            provider_name="mock_test",
        ),
        StockObservation(
            product_id="itm200",
            product_name="Over Budget Product",
            product_url="https://www.flipkart.com/over/p/itm200",
            availability=AvailabilityState.IN_STOCK,
            price=700.0,
            provider_name="mock_test",
        ),
    ]

    from tests.conftest import InMemoryTestNotifier
    from app.models import NotificationChannel

    mock_notifier = InMemoryTestNotifier(NotificationChannel.WHATSAPP)
    monitor = FlipkartMonitor(
        settings=settings,
        custom_provider=mock_provider,
        custom_whatsapp=mock_notifier,
    )

    res1 = monitor.check_once(product=MonitoredProduct(
        name="Under Budget Product",
        url="https://www.flipkart.com/under/p/itm100",
        max_price=1000.0,
    ))
    res2 = monitor.check_once(product=MonitoredProduct(
        name="Over Budget Product",
        url="https://www.flipkart.com/over/p/itm200",
        max_price=500.0,
    ))

    # Product 1 should trigger restock event
    assert res1[1] is not None
    assert res1[1].current_state == AvailabilityState.IN_STOCK

    # Product 2 should be suppressed due to price ceiling
    assert res2[1] is None
