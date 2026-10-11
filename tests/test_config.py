"""Tests for configuration parsing, validation, and SSRF prevention."""

import pytest
from app.config import Settings
from pydantic import ValidationError


def test_valid_config():
    """Verify valid settings initialize without error."""
    settings = Settings(
        _env_file=None,
        PRODUCT_NAME="Test iPhone",
        PRODUCT_URL="https://www.flipkart.com/apple-iphone-15/p/itm12345",
        NOTIFICATION_CHANNELS="email",
        SMTP_HOST="smtp.example.com",
        SMTP_FROM="alerts@example.com",
        EMAIL_TO="user@example.com",
        WHATSAPP_PHONE_NUMBER_ID=None,
        WHATSAPP_ACCESS_TOKEN=None,
        WHATSAPP_TO=None,
    )
    assert settings.PRODUCT_NAME == "Test iPhone"
    assert settings.is_email_configured() is True
    assert settings.is_whatsapp_configured() is False


def test_invalid_url_scheme():
    """Reject unpermitted URL schemes."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="ftp://www.flipkart.com/item/p/itm123",
            NOTIFICATION_CHANNELS="email",
            SMTP_HOST="smtp.example.com",
            SMTP_FROM="alerts@example.com",
            EMAIL_TO="user@example.com",
        )
    assert "Invalid URL scheme" in str(exc.value)


def test_ssrf_protection_rejects_external_hosts():
    """Reject non-Flipkart domains to prevent SSRF vulnerabilities."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="https://malicious-internal-service.local/p/itm123",
            NOTIFICATION_CHANNELS="email",
            SMTP_HOST="smtp.example.com",
            SMTP_FROM="alerts@example.com",
            EMAIL_TO="user@example.com",
        )
    assert "Only official Flipkart URLs (*.flipkart.com) are permitted" in str(exc.value)


def test_whatsapp_channel_missing_credentials_fails():
    """Fail clearly when WhatsApp is chosen but credentials are missing."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="https://www.flipkart.com/product/p/itm123",
            NOTIFICATION_CHANNELS="whatsapp",
            WHATSAPP_PHONE_NUMBER_ID=None,
            WHATSAPP_ACCESS_TOKEN=None,
            WHATSAPP_TO=None,
        )
    assert "WhatsApp settings" in str(exc.value)


def test_email_channel_missing_credentials_fails():
    """Fail clearly when Email is chosen but credentials are missing."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="https://www.flipkart.com/product/p/itm123",
            NOTIFICATION_CHANNELS="email",
            SMTP_HOST=None,
            SMTP_FROM=None,
            EMAIL_TO=None,
        )
    assert "Email settings" in str(exc.value)


def test_both_channels_missing_all_credentials_fails():
    """Fail clearly when 'both' is selected but neither channel is configured."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="https://www.flipkart.com/product/p/itm123",
            NOTIFICATION_CHANNELS="both",
            WHATSAPP_PHONE_NUMBER_ID=None,
            WHATSAPP_ACCESS_TOKEN=None,
            WHATSAPP_TO=None,
            SMTP_HOST=None,
            SMTP_FROM=None,
            EMAIL_TO=None,
        )
    assert "At least one notification channel must be properly configured" in str(exc.value)


def test_affiliate_provider_missing_credentials_fails():
    """Fail clearly if flipkart_affiliate is selected without credentials."""
    with pytest.raises(ValidationError) as exc:
        Settings(
            _env_file=None,
            PRODUCT_NAME="Test",
            PRODUCT_URL="https://www.flipkart.com/product/p/itm123",
            STOCK_PROVIDER_TYPE="flipkart_affiliate",
            FLIPKART_AFFILIATE_ID=None,
            FLIPKART_AFFILIATE_TOKEN=None,
            NOTIFICATION_CHANNELS="email",
            SMTP_HOST="smtp.example.com",
            SMTP_FROM="a@b.com",
            EMAIL_TO="c@d.com",
        )
    assert "FLIPKART_AFFILIATE_ID or FLIPKART_AFFILIATE_TOKEN is missing" in str(exc.value)
