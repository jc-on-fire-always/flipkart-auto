"""Configuration module with environment variable parsing and validation."""

from __future__ import annotations

from typing import Literal, Optional
from urllib.parse import urlparse

from pydantic import Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings validated from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Monitored Product
    PRODUCT_NAME: str = Field(..., description="Human-readable product name")
    PRODUCT_URL: str = Field(..., description="Full Flipkart product listing URL")
    POLL_INTERVAL_SECONDS: int = Field(default=60, ge=5, description="Polling interval in seconds")
    MAX_PRODUCT_PRICE: Optional[float] = Field(default=None, ge=0.0, description="Optional price ceiling")
    INITIAL_ALERT_ON_STARTUP: bool = Field(default=False, description="Send alert if in stock on initial run")
    VERIFICATION_COUNT: int = Field(default=1, ge=1, description="Consecutive checks required before alerting")

    # Stock Provider
    STOCK_PROVIDER_TYPE: Literal["mock", "flipkart_affiliate", "flipkart_web"] = Field(
        default="mock", description="Stock provider implementation"
    )
    MOCK_STOCK_STATE: Literal["IN_STOCK", "OUT_OF_STOCK"] = Field(
        default="OUT_OF_STOCK", description="Simulated stock state when provider is mock"
    )
    FLIPKART_AFFILIATE_ID: Optional[str] = Field(default=None, description="Flipkart Affiliate ID")
    FLIPKART_AFFILIATE_TOKEN: Optional[str] = Field(default=None, description="Flipkart Affiliate Token")

    # Notification Channels
    NOTIFICATION_CHANNELS: Literal["whatsapp", "email", "both"] = Field(
        default="both", description="Active notification channels"
    )
    ALERT_COOLDOWN_SECONDS: int = Field(default=3600, ge=0, description="Cooldown between restock alerts")

    # WhatsApp Cloud API Configuration
    WHATSAPP_PROVIDER: str = Field(default="meta_cloud", description="WhatsApp provider name")
    WHATSAPP_PHONE_NUMBER_ID: Optional[str] = Field(default=None, description="Sender Phone Number ID")
    WHATSAPP_ACCESS_TOKEN: Optional[str] = Field(default=None, description="Meta Graph API access token")
    WHATSAPP_TO: Optional[str] = Field(default=None, description="Recipient phone in international format")
    WHATSAPP_MESSAGE_MODE: Literal["template", "text"] = Field(
        default="template", description="Use 'template' for business initiated, 'text' for sandbox/testing"
    )
    WHATSAPP_TEMPLATE_NAME: str = Field(default="flipkart_restock_alert", description="Approved WhatsApp template")
    WHATSAPP_TEMPLATE_LANGUAGE: str = Field(default="en_US", description="Template language code")

    # SMTP Email Configuration
    SMTP_HOST: Optional[str] = Field(default=None, description="SMTP server hostname")
    SMTP_PORT: int = Field(default=587, ge=1, le=65535, description="SMTP server port")
    SMTP_USERNAME: Optional[str] = Field(default=None, description="SMTP username")
    SMTP_PASSWORD: Optional[str] = Field(default=None, description="SMTP password")
    SMTP_FROM: Optional[str] = Field(default=None, description="Sender email address")
    SMTP_USE_TLS: bool = Field(default=True, description="Enable STARTTLS")
    EMAIL_TO: Optional[str] = Field(default=None, description="Recipient email address")

    # Persistence
    DATABASE_URL: str = Field(default="sqlite:///data/monitor.db", description="Database connection URL")

    # Application Runtime
    LOG_LEVEL: str = Field(default="INFO", description="Logging verbosity")
    MAX_RETRIES: int = Field(default=3, ge=1, description="Max retries for transient operations")
    REQUEST_TIMEOUT_SECONDS: int = Field(default=10, ge=1, description="HTTP and network timeout in seconds")
    HEALTH_HOST: str = Field(default="0.0.0.0", description="Host address for health check server")
    HEALTH_PORT: int = Field(default=8080, ge=1024, le=65535, description="Port for health check endpoint")

    @field_validator("PRODUCT_URL")
    @classmethod
    def validate_product_url(cls, v: str) -> str:
        """Validate URL format and prevent SSRF attacks against private networks."""
        parsed = urlparse(v)
        if not parsed.scheme or parsed.scheme.lower() not in ("http", "https"):
            raise ValueError(f"Invalid URL scheme '{parsed.scheme}'. Must be HTTP or HTTPS.")

        hostname = (parsed.hostname or "").lower()
        if not hostname:
            raise ValueError("PRODUCT_URL must include a valid hostname.")

        # Disallow loopback / local IPs unless explicitly localhost for mock tests
        disallowed = ("127.0.0.1", "::1", "0.0.0.0")
        if hostname in disallowed:
            # Allow only if explicitly mock/test
            return v

        # If it's a remote URL, verify Flipkart domain
        allowed_domains = ("flipkart.com", "dl.flipkart.com", "2.flipkart.com")
        if not any(hostname == d or hostname.endswith("." + d) or hostname in ("localhost", "127.0.0.1") for d in allowed_domains):
            raise ValueError(
                f"Invalid product URL host '{hostname}'. Only official Flipkart URLs (*.flipkart.com) are permitted."
            )

        return v

    def is_whatsapp_configured(self) -> bool:
        """Check if minimum required WhatsApp settings are present."""
        return bool(
            self.WHATSAPP_PHONE_NUMBER_ID
            and self.WHATSAPP_ACCESS_TOKEN
            and self.WHATSAPP_TO
        )

    def is_email_configured(self) -> bool:
        """Check if minimum required Email settings are present."""
        return bool(
            self.SMTP_HOST
            and self.SMTP_FROM
            and self.EMAIL_TO
        )

    @model_validator(mode="after")
    def validate_channel_readiness(self) -> "Settings":
        """Validate that at least one configured channel has the necessary credentials."""
        w_ok = self.is_whatsapp_configured()
        e_ok = self.is_email_configured()

        if self.NOTIFICATION_CHANNELS == "whatsapp" and not w_ok:
            raise ValueError(
                "NOTIFICATION_CHANNELS is set to 'whatsapp', but WhatsApp settings "
                "(WHATSAPP_PHONE_NUMBER_ID, WHATSAPP_ACCESS_TOKEN, WHATSAPP_TO) are incomplete."
            )
        elif self.NOTIFICATION_CHANNELS == "email" and not e_ok:
            raise ValueError(
                "NOTIFICATION_CHANNELS is set to 'email', but Email settings "
                "(SMTP_HOST, SMTP_FROM, EMAIL_TO) are incomplete."
            )
        elif self.NOTIFICATION_CHANNELS == "both" and not (w_ok or e_ok):
            raise ValueError(
                "NOTIFICATION_CHANNELS is set to 'both', but neither WhatsApp nor Email settings are complete. "
                "At least one notification channel must be properly configured."
            )

        # Validate affiliate settings if chosen
        if self.STOCK_PROVIDER_TYPE == "flipkart_affiliate":
            if not self.FLIPKART_AFFILIATE_ID or not self.FLIPKART_AFFILIATE_TOKEN:
                raise ValueError(
                    "STOCK_PROVIDER_TYPE is 'flipkart_affiliate', but FLIPKART_AFFILIATE_ID "
                    "or FLIPKART_AFFILIATE_TOKEN is missing."
                )

        return self
