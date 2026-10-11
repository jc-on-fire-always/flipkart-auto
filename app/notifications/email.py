"""SMTP Email notification dispatcher with HTML alternative and retry resilience."""

from __future__ import annotations

import logging
import random
import smtplib
import time
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

from app.config import Settings
from app.models import (
    NotificationChannel,
    NotificationResult,
    NotificationStatus,
    RestockEvent,
)
from app.notifications.base import BaseNotifier

logger = logging.getLogger(__name__)


class EmailNotifier(BaseNotifier):
    """
    SMTP Email notification dispatcher.

    Sends multipart plain-text and rich HTML emails with clickable links.
    Handles STARTTLS/SSL, retries transient network errors with backoff and jitter,
    and protects sensitive credentials from log output.
    """

    def __init__(self, settings: Settings):
        self._host = settings.SMTP_HOST
        self._port = settings.SMTP_PORT
        self._username = settings.SMTP_USERNAME
        self._password = settings.SMTP_PASSWORD
        self._from_addr = settings.SMTP_FROM
        self._to_addr = settings.EMAIL_TO
        self._use_tls = settings.SMTP_USE_TLS
        self._max_retries = settings.MAX_RETRIES
        self._timeout_seconds = settings.REQUEST_TIMEOUT_SECONDS

    @property
    def channel_name(self) -> NotificationChannel:
        return NotificationChannel.EMAIL

    def is_configured(self) -> bool:
        """Check if minimum required SMTP settings are present."""
        return bool(self._host and self._from_addr and self._to_addr)

    def send_restock_alert(self, event: RestockEvent) -> NotificationResult:
        """Send formatted restock alert via email with HTML alternative."""
        if not self.is_configured():
            logger.warning("Email notifier is not configured.")
            return NotificationResult(
                channel=self.channel_name,
                status=NotificationStatus.FAILED,
                error_message="SMTP configuration or recipient address missing",
                timestamp_utc=datetime.now(timezone.utc),
            )

        detected_str = event.detected_at_utc.strftime("%Y-%m-%d %H:%M:%S UTC")
        price_str = f"₹{event.price:,.2f}" if event.price is not None else "Check official listing"
        subject = f"Flipkart Restock Alert — {event.product_name}"

        plain_text = (
            "A restock has been detected for your monitored product.\n\n"
            f"Product: {event.product_name}\n\n"
            "Availability: Reported IN STOCK\n\n"
            f"Detected at: {detected_str}\n\n"
            f"Price: {price_str}\n\n"
            f"Open the product on Flipkart:\n{event.product_url}\n\n"
            "Please verify availability, product variant, delivery details, and final price before placing the order."
        )

        html_text = f"""\
<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>{subject}</title>
</head>
<body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333333; max-width: 600px; margin: 0 auto; padding: 20px;">
  <div style="background-color: #2874f0; color: #ffffff; padding: 15px 20px; border-radius: 6px 6px 0 0;">
    <h2 style="margin: 0; font-size: 20px;">Flipkart Restock Alert</h2>
  </div>
  <div style="border: 1px solid #e0e0e0; border-top: none; padding: 20px; border-radius: 0 0 6px 6px; background-color: #ffffff;">
    <p style="font-size: 16px;">A restock has been detected for your monitored product:</p>
    <table style="width: 100%; border-collapse: collapse; margin: 15px 0;">
      <tr>
        <td style="padding: 8px 0; font-weight: bold; width: 120px;">Product:</td>
        <td style="padding: 8px 0;">{event.product_name}</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; font-weight: bold;">Availability:</td>
        <td style="padding: 8px 0; color: #2e7d32; font-weight: bold;">Reported IN STOCK</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; font-weight: bold;">Detected at:</td>
        <td style="padding: 8px 0;">{detected_str}</td>
      </tr>
      <tr>
        <td style="padding: 8px 0; font-weight: bold;">Price:</td>
        <td style="padding: 8px 0; font-size: 18px; color: #1565c0; font-weight: bold;">{price_str}</td>
      </tr>
    </table>
    <div style="margin: 25px 0 20px 0; text-align: center;">
      <a href="{event.product_url}" style="background-color: #fb641b; color: #ffffff; padding: 12px 28px; text-decoration: none; font-size: 16px; font-weight: bold; border-radius: 4px; display: inline-block;">
        View Product on Flipkart
      </a>
    </div>
    <p style="font-size: 12px; color: #757575; margin-top: 25px; border-top: 1px solid #eeeeee; padding-top: 15px;">
      Direct Link: <a href="{event.product_url}" style="color: #2874f0;">{event.product_url}</a><br>
      Please verify availability, product variant, delivery details, and final price before placing the order.
    </p>
  </div>
</body>
</html>
"""
        return self._send_email(subject, plain_text, html_text)

    def send_test_message(self) -> NotificationResult:
        """Send a diagnostic test email to verify SMTP credentials and routing."""
        if not self.is_configured():
            return NotificationResult(
                channel=self.channel_name,
                status=NotificationStatus.FAILED,
                error_message="SMTP configuration or recipient address missing",
                timestamp_utc=datetime.now(timezone.utc),
            )

        subject = "Flipkart Restock Monitor — Diagnostic Test"
        plain = (
            "This is a test notification confirming that SMTP email integration is operational.\n"
            f"Timestamp: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}"
        )
        html = f"<p><strong>Diagnostic Test</strong></p><p>{plain}</p>"
        return self._send_email(subject, plain, html)

    def _send_email(self, subject: str, plain_text: str, html_text: str) -> NotificationResult:
        """Transmit email message through authenticated SMTP with exponential backoff."""
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = self._from_addr
        msg["To"] = self._to_addr
        msg["Date"] = datetime.now(timezone.utc).strftime("%a, %d %b %Y %H:%M:%S +0000")

        msg.attach(MIMEText(plain_text, "plain", "utf-8"))
        msg.attach(MIMEText(html_text, "html", "utf-8"))

        attempts = 0
        last_error = ""

        for attempt in range(1, self._max_retries + 1):
            attempts = attempt
            server: Optional[smtplib.SMTP] = None
            try:
                logger.info("Connecting to SMTP server %s:%s (attempt %s/%s)...", self._host, self._port, attempt, self._max_retries)

                if self._port == 465:
                    server = smtplib.SMTP_SSL(self._host, self._port, timeout=self._timeout_seconds)
                else:
                    server = smtplib.SMTP(self._host, self._port, timeout=self._timeout_seconds)
                    if self._use_tls:
                        server.starttls()

                if self._username and self._password:
                    # Authenticate without logging credentials
                    server.login(self._username, self._password)

                server.sendmail(self._from_addr, [self._to_addr], msg.as_string())
                logger.info("Email alert successfully accepted by SMTP server for delivery to recipient.")

                return NotificationResult(
                    channel=self.channel_name,
                    status=NotificationStatus.ACCEPTED,
                    attempt_count=attempts,
                    timestamp_utc=datetime.now(timezone.utc),
                )

            except (smtplib.SMTPException, OSError, TimeoutError) as exc:
                last_error = f"SMTP error: {exc}"
                logger.warning("SMTP transmission failure on attempt %s: %s", attempt, exc)

            finally:
                if server:
                    try:
                        server.quit()
                    except Exception:
                        pass

            if attempt < self._max_retries:
                delay = (2 ** attempt) + random.uniform(0.1, 0.9)
                time.sleep(delay)

        logger.error("Email delivery failed after %s attempts. Last error: %s", attempts, last_error)
        return NotificationResult(
            channel=self.channel_name,
            status=NotificationStatus.FAILED,
            error_message=last_error,
            attempt_count=attempts,
            timestamp_utc=datetime.now(timezone.utc),
        )
