"""Command-line interface commands for running, testing, and managing the Flipkart monitor."""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from typing import Optional

from app.config import Settings
from app.database import DatabaseManager
from app.logging_config import setup_logging
from app.models import (
    AvailabilityState,
    MonitoredProduct,
    RestockEvent,
    RestockEventStatus,
)
from app.monitor import FlipkartMonitor
from app.services.notification_service import NotificationService


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="flipkart-monitor",
        description="24/7 Flipkart Restock Monitor with WhatsApp & Email Alerts (Multi-Product Support)",
    )
    subparsers = parser.add_subparsers(dest="command", help="Operational commands")

    # Command: run (default)
    subparsers.add_parser("run", help="Start continuous 24/7 background monitor daemon")

    # Command: check-once
    check_parser = subparsers.add_parser("check-once", help="Execute stock check and exit")
    check_parser.add_argument(
        "-p", "--product",
        dest="product_target",
        help="Specific product index (1-based) or name substring to check (default: check all)",
    )

    # Command: list-products
    subparsers.add_parser("list-products", help="List all configured products")

    # Command: add-product
    add_parser = subparsers.add_parser("add-product", help="Add a new product to monitor")
    add_parser.add_argument("--name", required=True, help="Friendly name of the product")
    add_parser.add_argument("--url", required=True, help="Flipkart product URL (must be *.flipkart.com)")
    add_parser.add_argument("--max-price", type=float, default=None, help="Optional price ceiling threshold")
    add_parser.add_argument("--disabled", action="store_true", help="Add in disabled state")

    # Command: remove-product
    rem_parser = subparsers.add_parser("remove-product", help="Remove a product from monitoring")
    rem_parser.add_argument("target", help="Product index (1-based), name substring, or URL to remove")

    # Command: status
    subparsers.add_parser("status", help="Inspect local database health and tracked stock states")

    # Command: test-notifications
    subparsers.add_parser("test-notifications", help="Send diagnostic test notifications to configured channels")

    # Command: simulate-restock
    sim_parser = subparsers.add_parser("simulate-restock", help="Simulate a real restock event and trigger alerts immediately")
    sim_parser.add_argument(
        "-p", "--product",
        dest="product_target",
        help="Specific product index (1-based) or name substring to simulate (default: first product)",
    )

    # Command: set-state
    state_parser = subparsers.add_parser("set-state", help="Manually change product availability state in local database")
    state_parser.add_argument("target", help="Product index (1-based), product ID, or name substring")
    state_parser.add_argument("--state", required=True, choices=["IN_STOCK", "OUT_OF_STOCK", "UNKNOWN"], help="Target stock state")
    state_parser.add_argument("--clear-cooldown", action="store_true", help="Clear active alert cooldowns for this product")

    return parser.parse_args()


def _resolve_target_product(
    products: list[MonitoredProduct], target: Optional[str]
) -> Optional[MonitoredProduct]:
    """Find a product by 1-based index or case-insensitive name/URL substring."""
    if not target:
        return products[0] if products else None

    # Check 1-based index
    if target.isdigit():
        idx = int(target) - 1
        if 0 <= idx < len(products):
            return products[idx]

    # Substring match on name or URL
    lower = target.lower()
    for p in products:
        if lower in p.name.lower() or lower in p.url.lower():
            return p

    return None


def main() -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

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

    elif command == "list-products":
        products = settings.get_monitored_products(include_disabled=True)
        print("=" * 80)
        print(f"CONFIGURED MONITORED PRODUCTS ({len(products)} total)")
        print("=" * 80)
        if not products:
            print("No products currently configured.")
            return 0

        for i, p in enumerate(products, 1):
            status = "ENABLED" if p.enabled else "DISABLED"
            price_limit = f"Rs. {p.max_price:,.2f}" if p.max_price else "No ceiling"
            print(f"[{i}] {p.name}")
            print(f"    Status   : {status}")
            print(f"    Max Price: {price_limit}")
            print(f"    URL      : {p.url}")
            print("-" * 80)
        return 0

    elif command == "add-product":
        name = args.name.strip()
        url = args.url.strip()
        max_price = args.max_price
        enabled = not args.disabled

        try:
            settings.validate_product_url(url)
        except Exception as exc:
            print(f"ERROR: Invalid Flipkart URL: {exc}", file=sys.stderr)
            return 1

        existing = settings.get_monitored_products(include_disabled=True)
        # Check if already present by URL
        for p in existing:
            if p.url == url:
                print(f"Product with this URL already exists: '{p.name}'. Updating metadata...")
                p.name = name
                p.max_price = max_price
                p.enabled = enabled
                settings.save_monitored_products(existing)
                print(f"Successfully updated product: {name}")
                return 0

        new_prod = MonitoredProduct(
            name=name,
            url=url,
            max_price=max_price,
            enabled=enabled,
        )
        existing.append(new_prod)
        settings.save_monitored_products(existing)
        print(f"Successfully added product [{len(existing)}]: {name}")
        return 0

    elif command == "remove-product":
        target = args.target.strip()
        existing = settings.get_monitored_products(include_disabled=True)
        match = _resolve_target_product(existing, target)
        if not match:
            print(f"ERROR: No product matched '{target}'. Use 'list-products' to view products.", file=sys.stderr)
            return 1

        remaining = [p for p in existing if p.url != match.url]
        settings.save_monitored_products(remaining)
        print(f"Successfully removed product: '{match.name}'")
        print(f"Remaining products: {len(remaining)}")
        return 0

    elif command == "check-once":
        monitor = FlipkartMonitor(settings=settings)
        all_enabled = settings.get_monitored_products(include_disabled=False)

        if not all_enabled:
            print("No enabled products found to check.")
            return 1

        targets: list[MonitoredProduct] = []
        if getattr(args, "product_target", None):
            match = _resolve_target_product(all_enabled, args.product_target)
            if not match:
                print(f"ERROR: Product matching '{args.product_target}' not found.", file=sys.stderr)
                return 1
            targets = [match]
        else:
            targets = all_enabled

        print("=" * 80)
        print(f"CHECKING STOCK FOR {len(targets)} PRODUCT(S)")
        print("=" * 80)

        for i, prod in enumerate(targets, 1):
            print(f"\n--- Product [{i}/{len(targets)}]: {prod.name} ---")
            print(f"URL: {prod.url}")
            obs, event, notifs = monitor.check_once(product=prod)
            if obs:
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

        return 0

    elif command == "status":
        db_mgr = DatabaseManager(settings.DATABASE_URL)
        health = db_mgr.get_health_snapshot()

        print("=" * 80)
        print("FLIPKART RESTOCK MONITOR - SERVICE STATUS")
        print("=" * 80)
        print(f"Active Provider        : {health.get('active_provider') or 'Not yet executed'}")
        print(f"Last Successful Check  : {health.get('last_successful_check_utc') or 'None'}")
        print(f"Last Observed Stock    : {health.get('last_observed_availability') or 'None'}")
        print(f"Last Notification Sent : {health.get('last_successful_notification_utc') or 'None'}")
        print(f"Consecutive Failures   : {health.get('consecutive_errors', 0)}")
        if health.get("last_error_summary"):
            print(f"Last Error Summary     : {health.get('last_error_summary')}")

        db_products = db_mgr.get_all_products()
        print("\n" + "=" * 80)
        print(f"DATABASE TRACKED PRODUCTS ({len(db_products)} total in DB)")
        print("=" * 80)
        if not db_products:
            print("No product observations recorded in database yet.")
        else:
            for p in db_products:
                print(f"- ID: {p.product_id}")
                print(f"  Name        : {p.name}")
                print(f"  State       : {p.last_known_availability}")
                print(f"  In-Stock Run: {p.consecutive_in_stock_count}")
                print(f"  Last Checked: {p.last_checked_at_utc.isoformat() if p.last_checked_at_utc else 'Never'}")
                print(f"  URL         : {p.url}")

        recent_events = db_mgr.get_recent_events(limit=5)
        print("\n" + "=" * 80)
        print(f"RECENT RESTOCK EVENTS ({len(recent_events)} most recent)")
        print("=" * 80)
        if not recent_events:
            print("No restock events recorded yet.")
        else:
            for ev in recent_events:
                print(f"Event #{ev.id} [{ev.detected_at_utc.isoformat()}]")
                print(f"  Product ID  : {ev.product_id}")
                print(f"  Transition  : {ev.previous_state} -> {ev.current_state}")
                print(f"  Price       : {f'Rs. {ev.price:,.2f}' if ev.price else 'N/A'}")
                print(f"  Status      : {ev.status}")
                print(f"  Cooldown To : {ev.cooldown_until_utc.isoformat()}")

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
        products = settings.get_monitored_products(include_disabled=False)
        target_prod = _resolve_target_product(products, getattr(args, "product_target", None))
        if not target_prod:
            print("ERROR: No enabled product found to simulate.", file=sys.stderr)
            return 1

        print("=" * 60)
        print(f"SIMULATING LIVE RESTOCK EVENT FOR: {target_prod.name}")
        print(f"Product URL: {target_prod.url}")
        print("=" * 60)

        db_mgr = DatabaseManager(settings.DATABASE_URL)
        notif_service = NotificationService(settings=settings, db_manager=db_mgr)

        product = db_mgr.get_or_create_product(
            product_id=target_prod.name.lower().replace(" ", "_")[:32],
            name=target_prod.name,
            url=target_prod.url,
            max_price=target_prod.max_price,
        )

        sim_price = target_prod.max_price or 1999.0
        event = RestockEvent(
            product_id=product.product_id,
            product_name=target_prod.name,
            product_url=target_prod.url,
            detected_at_utc=datetime.now(timezone.utc),
            price=sim_price,
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
        return 0

    elif command == "set-state":
        db_mgr = DatabaseManager(settings.DATABASE_URL)
        all_products = db_mgr.get_all_products()

        target = args.target.strip()
        matched = None

        # 1. Match by 1-based index from configured products list
        if target.isdigit():
            idx = int(target) - 1
            configured = settings.get_monitored_products(include_disabled=True)
            if 0 <= idx < len(configured):
                cfg_url = configured[idx].url
                for p in all_products:
                    if p.url == cfg_url:
                        matched = p
                        break

        # 2. Match by exact product_id
        if not matched:
            for p in all_products:
                if p.product_id == target:
                    matched = p
                    break

        # 3. Match by case-insensitive name or ID substring
        if not matched:
            lower = target.lower()
            for p in all_products:
                if lower in p.name.lower() or lower in p.product_id.lower() or lower in p.url.lower():
                    matched = p
                    break

        if not matched:
            print(f"ERROR: Product matching '{target}' not found in database.", file=sys.stderr)
            print("Run 'python -m app.cli status' to see all tracked product IDs.")
            return 1

        target_state = AvailabilityState(args.state)
        new_count = 1 if target_state == AvailabilityState.IN_STOCK else 0
        db_mgr.update_product_state(
            product_id=matched.product_id,
            availability=target_state,
            consecutive_in_stock_count=new_count,
        )

        cleared_cooldowns = 0
        if getattr(args, "clear_cooldown", False):
            cleared_cooldowns = db_mgr.clear_cooldown(matched.product_id)

        print("=" * 60)
        print("PRODUCT DATABASE STATE UPDATED")
        print("=" * 60)
        print(f"Product ID   : {matched.product_id}")
        print(f"Product Name : {matched.name}")
        print(f"New State    : {target_state.value}")
        print(f"In-Stock Run : {new_count}")
        if cleared_cooldowns > 0:
            print(f"Cooldown     : Cleared ({cleared_cooldowns} active cooldowns expired)")
        elif getattr(args, "clear_cooldown", False):
            print("Cooldown     : No active cooldown found")
        print("=" * 60)
        return 0

    return 0


if __name__ == "__main__":
    sys.exit(main())
