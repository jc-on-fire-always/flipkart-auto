"""Command-line interface commands for running, testing, and inspecting the monitor."""

from __future__ import annotations

import argparse
import sys

from app.config import Settings
from app.database import DatabaseManager
from app.logging_config import setup_logging
from app.monitor import FlipkartMonitor
from app.services.notification_service import NotificationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flipkart-monitor",
        description="24/7 Flipkart Restock Monitor with WhatsApp & Email Alerts",
    )
    subparsers = parser.add_subparsers(dest="command", help="Operational commands")

    # Command: run (default)
    subparsers.add_parser("run", help="Start continuous 24/7 background monitor daemon")

    # Command: check-once
    subparsers.add_parser("check-once", help="Execute single stock check and exit")

    # Command: test-notifications
    subparsers.add_parser("test-notifications", help="Send diagnostic test notifications to configured channels")

    # Command: status
    subparsers.add_parser("status", help="Inspect local database health and stock observations")

    # Command: simulate-restock
    subparsers.add_parser("simulate-restock", help="Simulate a real restock event and trigger alerts immediately")

    return parser.parse_args()


def main() -> int:
    args = parse_args()
    command = args.command or "run"

    try:
        settings = Settings()
    except Exception as exc:
        print(f"CONFIGURATION ERROR: Failed to load settings from environment: {exc}", file=sys.stderr)
        return 1

    setup_logging(level=settings.LOG_LEVEL)

    if command == "run":
        monitor = FlipkartMonitor(settings=settings)
        monitor.run()
        return 0

    elif command == "check-once":
        print(f"Checking stock for: {settings.PRODUCT_NAME}")
        print(f"URL: {settings.PRODUCT_URL}")
        monitor = FlipkartMonitor(settings=settings)
        obs, event, notifs = monitor.check_once()
        if obs:
            print("\n--- Stock Observation Result ---")
            print(f"Product ID   : {obs.product_id}")
            print(f"Title        : {obs.product_name}")
            print(f"Availability : {obs.availability.value}")
            print(f"Price        : {f'Rs. {obs.price:,.2f}' if obs.price is not None else 'N/A'}")
            print(f"Provider     : {obs.provider_name}")
            print(f"Confidence   : {obs.confidence_score * 100:.1f}%")
            if event:
                print(f"RESTOCK EVENT: Triggered! Notifications sent: {len(notifs)}")
            else:
                print("RESTOCK EVENT: None triggered (state did not transition or in cooldown)")
        else:
            print("Check failed: provider could not complete request.")
            return 1
        return 0

    elif command == "test-notifications":
        print(f"Testing notification channels: {settings.NOTIFICATION_CHANNELS}")
        db_mgr = DatabaseManager(settings.DATABASE_URL)
        notif_service = NotificationService(settings=settings, db_manager=db_mgr)
        results = notif_service.dispatch_test_notifications()
        print("\n--- Diagnostic Results ---")
        for res in results:
            print(f"Channel : {res.channel.value.upper()}")
            print(f"Status  : {res.status.value}")
            if res.provider_message_id:
                print(f"Msg ID  : {res.provider_message_id}")
            if res.error_message:
                print(f"Error   : {res.error_message}")
            print("-" * 30)
        return 0

    elif command == "simulate-restock":
        from datetime import datetime, timezone
        from app.models import AvailabilityState, RestockEvent, RestockEventStatus

        print("=" * 60)
        print(f"SIMULATING LIVE RESTOCK EVENT FOR: {settings.PRODUCT_NAME}")
        print(f"Product URL: {settings.PRODUCT_URL}")
        print("=" * 60)

        db_mgr = DatabaseManager(settings.DATABASE_URL)
        notif_service = NotificationService(settings=settings, db_manager=db_mgr)

        product = db_mgr.get_or_create_product(
            product_id="simulated_restock",
            name=settings.PRODUCT_NAME,
            url=settings.PRODUCT_URL,
            max_price=settings.MAX_PRODUCT_PRICE,
        )

        event = RestockEvent(
            product_id=product.product_id,
            product_name=settings.PRODUCT_NAME,
            product_url=settings.PRODUCT_URL,
            detected_at_utc=datetime.now(timezone.utc),
            price=settings.MAX_PRODUCT_PRICE or 1999.0,
            previous_state=AvailabilityState.OUT_OF_STOCK,
            current_state=AvailabilityState.IN_STOCK,
            status=RestockEventStatus.DETECTED,
        )

        event_id = db_mgr.record_restock_event(event, cooldown_seconds=settings.ALERT_COOLDOWN_SECONDS)
        event.id = event_id

        results = notif_service.dispatch_restock_alert(event, restock_event_id=event_id)

        print("\n--- Restock Alert Notification Results ---")
        for res in results:
            print(f"Channel : {res.channel.value.upper()}")
            print(f"Status  : {res.status.value}")
            if res.provider_message_id:
                print(f"Msg ID  : {res.provider_message_id}")
            if res.error_message:
                print(f"Error   : {res.error_message}")
            print("-" * 30)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
