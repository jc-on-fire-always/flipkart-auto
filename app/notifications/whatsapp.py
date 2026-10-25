"""Meta WhatsApp Cloud API notification dispatcher."""
from __future__ import annotations
from datetime import timedelta

import logging
import random
import re
import time
from datetime import datetime, timezone
from typing import Any

import requests

from app.config import Settings
from app.models import (
    NotificationChannel,
    NotificationResult,
    NotificationStatus,
    RestockEvent,
)
from app.notifications.base import BaseNotifier
from app.utils import resolve_product_title, shorten_url

logger = logging.getLogger(__name__)


class WhatsAppNotifier(BaseNotifier):
    """
    Official Meta WhatsApp Cloud API notifier.

    Dispatches business-initiated messages using pre-approved templates
    or standard text messages (in sandbox / customer-service windows).
    """

    GRAPH_API_VERSION = "v21.0"
    GRAPH_API_URL = "https://graph.facebook.com"

    def __init__(self, settings: Settings):
        self._phone_number_id = settings.WHATSAPP_PHONE_NUMBER_ID
        self._access_token = settings.WHATSAPP_ACCESS_TOKEN
        self._recipient = self._sanitize_phone(settings.WHATSAPP_TO) if settings.WHATSAPP_TO else None
        self._message_mode = settings.WHATSAPP_MESSAGE_MODE
        self._template_name = settings.WHATSAPP_TEMPLATE_NAME
        self._template_language = settings.WHATSAPP_TEMPLATE_LANGUAGE
        self._max_retries = settings.MAX_RETRIES
        self._timeout_seconds = settings.REQUEST_TIMEOUT_SECONDS

    @property
    def channel_name(self) -> NotificationChannel:
        return NotificationChannel.WHATSAPP

    def is_configured(self) -> bool:
        """Check whether WhatsApp credentials and destination are available."""
        return bool(self._phone_number_id and self._access_token and self._recipient)

    def send_restock_alert(self, event: RestockEvent) -> NotificationResult:
        """Send restock alert via WhatsApp with bounded retry and jitter."""
        if not self.is_configured():
            logger.warning("WhatsApp notifier is not fully configured.")
            return NotificationResult(
                channel=self.channel_name,
                status=NotificationStatus.FAILED,
                error_message="WhatsApp credentials or recipient unconfigured",
                timestamp_utc=datetime.now(timezone.utc),
            )

        product_name = resolve_product_title(
            primary=event.product_name,
            url=event.product_url,
            fallback_default="Flipkart Monitored Product",
        )
        sanitized_name = self._sanitize_text(product_name)
        ist_tz = timezone(timedelta(hours=5.5))
        detected_str = event.detected_at_utc.astimezone(ist_tz).strftime("%Y-%m-%d %H:%M:%S IST")
        price_str = f"₹{event.price:,.2f}" if event.price is not None else "Check official listing"
        shortened_url = shorten_url(event.product_url)

        if self._message_mode == "template":
            payload = self._build_template_payload(
                product_name=sanitized_name,
                detected_at=detected_str,
                price=price_str,
                product_url=shortened_url,
            )
        else:
            message_text = self._build_text_message(
                product_name=sanitized_name,
                detected_at=detected_str,
                price=price_str,
                product_url=shortened_url,
            )
            payload = self._build_text_payload(message_text)

        return self._dispatch_with_retry(payload)

    def send_test_message(self) -> NotificationResult:
        """Send a test diagnostic message via WhatsApp."""
        if not self.is_configured():
            return NotificationResult(
                channel=self.channel_name,
                status=NotificationStatus.FAILED,
                error_message="WhatsApp credentials or recipient unconfigured",
                timestamp_utc=datetime.now(timezone.utc),
            )

        if self._message_mode == "template":
            payload = self._build_template_payload(
                product_name="[DIAGNOSTIC TEST] Flipkart Monitor",
                detected_at=datetime.now(timezone.utc).astimezone(timezone(timedelta(hours=5.5))).strftime("%Y-%m-%d %H:%M:%S IST"),
                price="Rs. 0.00 (Test Only)",
                product_url="https://www.flipkart.com",
            )
        else:
            test_text = (
                "FLIPKART RESTOCK MONITOR — DIAGNOSTIC TEST\n\n"
                "This is a test notification confirming that Meta WhatsApp Cloud API integration is operational.\n"
                f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
            )
            payload = self._build_text_payload(test_text)
        return self._dispatch_with_retry(payload)

    def _dispatch_with_retry(self, payload: dict[str, Any]) -> NotificationResult:
        """Execute HTTP POST to Meta Graph API with exponential backoff and jitter."""
        url = f"{self.GRAPH_API_URL}/{self.GRAPH_API_VERSION}/{self._phone_number_id}/messages"
        headers = {
            "Authorization": f"Bearer {self._access_token}",
            "Content-Type": "application/json",
        }

        attempts = 0
        last_error = ""
        alt_lang_attempted = False

        for attempt in range(1, self._max_retries + 1):
            attempts = attempt
            try:
                logger.info("Dispatching WhatsApp alert (attempt %s/%s)...", attempt, self._max_retries)
                response = requests.post(url, headers=headers, json=payload, timeout=self._timeout_seconds)

                # Check for rate limiting
                if response.status_code == 429:
                    retry_after = int(response.headers.get("Retry-After", 2 ** attempt))
                    logger.warning("WhatsApp API rate limit (429). Waiting %s seconds...", retry_after)
                    time.sleep(retry_after)
                    continue

                if response.status_code in (200, 201):
                    data = response.json()
                    messages = data.get("messages", [])
                    msg_id = messages[0].get("id") if messages else None
                    logger.info("WhatsApp notification accepted for delivery. Message ID: %s", msg_id)
                    return NotificationResult(
                        channel=self.channel_name,
                        status=NotificationStatus.ACCEPTED,
                        provider_message_id=msg_id,
                        attempt_count=attempts,
                        timestamp_utc=datetime.now(timezone.utc),
                    )

                # Client-side 4xx errors (non-retryable like bad template or un-opted recipient)
                if 400 <= response.status_code < 500:
                    error_json = {}
                    try:
                        error_json = response.json().get("error", {})
                    except Exception:
                        pass
                    err_msg = error_json.get("message", response.text)
                    code = error_json.get("code")

                    # Handle #132001: Template name does not exist in the translation (e.g. en vs en_US)
                    if code == 132001 and payload.get("type") == "template" and not alt_lang_attempted:
                        alt_lang_attempted = True
                        curr_code = payload.get("template", {}).get("language", {}).get("code")
                        alt_code = "en" if curr_code == "en_US" else ("en_US" if curr_code == "en" else None)
                        if alt_code:
                            logger.warning(
                                "Template translation '%s' not found (#132001). Retrying with alternative '%s'...",
                                curr_code,
                                alt_code,
                            )
                            payload["template"]["language"]["code"] = alt_code
                            continue

                    logger.error("WhatsApp API client error (HTTP %s, Code %s): %s", response.status_code, code, err_msg)
                    return NotificationResult(
                        channel=self.channel_name,
                        status=NotificationStatus.FAILED,
                        error_message=f"HTTP {response.status_code} ({code}): {err_msg}",
                        attempt_count=attempts,
                        timestamp_utc=datetime.now(timezone.utc),
                    )

                # 5xx Server errors - retryable
                last_error = f"HTTP {response.status_code}: {response.text}"
                logger.warning("WhatsApp API server error on attempt %s: %s", attempt, last_error)

            except (requests.RequestException, ConnectionError, TimeoutError) as exc:
                last_error = f"Network exception: {exc}"
                logger.warning("Network failure dispatching WhatsApp notification (attempt %s): %s", attempt, exc)

            if attempt < self._max_retries:
                # Bounded exponential backoff with jitter
                delay = (2 ** attempt) + random.uniform(0.1, 0.9)
                time.sleep(delay)

        logger.error("WhatsApp delivery failed after %s attempts. Last error: %s", attempts, last_error)
        return NotificationResult(
            channel=self.channel_name,
            status=NotificationStatus.FAILED,
            error_message=last_error,
            attempt_count=attempts,
            timestamp_utc=datetime.now(timezone.utc),
        )

    def _build_template_payload(
        self, product_name: str, detected_at: str, price: str, product_url: str
    ) -> dict[str, Any]:
        """Construct official WhatsApp Business template message payload."""
        template_obj: dict[str, Any] = {
            "name": self._template_name,
            "language": {"code": self._template_language},
        }
        if self._template_name != "hello_world":
            template_obj["components"] = [
                {
                    "type": "body",
                    "parameters": [
                        {"type": "text", "text": product_name},
                        {"type": "text", "text": detected_at},
                        {"type": "text", "text": price},
                        {"type": "text", "text": product_url},
                    ],
                }
            ]

        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._recipient,
            "type": "template",
            "template": template_obj,
        }

    def _build_text_payload(self, body_text: str) -> dict[str, Any]:
        """Construct standard text payload for testing or 24h customer window."""
        return {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": self._recipient,
            "type": "text",
            "text": {
                "preview_url": True,
                "body": body_text,
            },
        }

    @staticmethod
    def _build_text_message(product_name: str, detected_at: str, price: str, product_url: str) -> str:
        return (
            "FLIPKART RESTOCK ALERT\n\n"
            f"Product: {product_name}\n\n"
            "Status: Reported IN STOCK\n\n"
            f"Detected at: {detected_at}\n\n"
            f"Price: {price}\n\n"
            "Open the product here:\n"
            f"{product_url}\n\n"
            "Availability may change before checkout. Open the official listing to verify the item, delivery eligibility, and final price."
        )

    @staticmethod
    def _sanitize_phone(phone: str) -> str:
        """Strip spaces, dashes, parentheses and leading plus sign."""
        clean = re.sub(r"[^\d]", "", phone)
        return clean

    @staticmethod
    def _sanitize_text(text: str) -> str:
        """Remove control characters to prevent message corruption."""
        return re.sub(r"[\r\n\t]+", " ", text).strip()[:200]
