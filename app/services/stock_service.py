"""Stock monitoring service managing state transitions, debouncing, and event emission."""

from __future__ import annotations

import logging
from typing import Optional, Tuple

from app.config import Settings
from app.database import DatabaseManager
from app.models import (
    AvailabilityState,
    MonitoredProduct,
    ProductRecord,
    RestockEvent,
    RestockEventStatus,
    StockObservation,
)
from app.providers.base import StockProvider
from app.utils import is_valid_product_title, resolve_product_title

logger = logging.getLogger(__name__)


class StockService:
    """
    Manages stock availability inspections, state transitions, debouncing, and restock events.

    Guarantees:
      - OUT_OF_STOCK -> IN_STOCK triggers exactly one restock event.
      - UNKNOWN never triggers an alert and never corrupts valid prior state.
      - Network/provider errors leave the persisted state unchanged.
      - Duplicate alerts during cooldown or continuous IN_STOCK states are suppressed.
      - Configurable verification count debounces temporary stock fluctuations.
    """

    def __init__(
        self,
        settings: Settings,
        db_manager: DatabaseManager,
        provider: StockProvider,
    ):
        self._settings = settings
        self._db = db_manager
        self._provider = provider
        self._first_checks: dict[str, bool] = {}

        # Initialize default product in persistence
        self._product = self._db.get_or_create_product(
            product_id=self._get_product_id(settings.PRODUCT_URL),
            name=settings.PRODUCT_NAME,
            url=settings.PRODUCT_URL,
            max_price=settings.MAX_PRODUCT_PRICE,
        )

    def _get_product_id(self, url: str) -> str:
        try:
            return self._provider.extract_product_id(url)  # type: ignore[attr-defined]
        except Exception:
            return "product_monitored"

    def execute_stock_check(
        self, product: Optional[MonitoredProduct] = None
    ) -> Tuple[Optional[StockObservation], Optional[RestockEvent]]:
        """
        Execute an isolated stock check for the monitored product.

        Returns:
          Tuple of (observation, restock_event).
          restock_event will be None if no valid restock transition occurred.
        """
        prod_name = product.name if product else self._settings.PRODUCT_NAME
        url = product.url if product else self._settings.PRODUCT_URL
        prod_max_price = product.max_price if product and product.max_price is not None else self._settings.MAX_PRODUCT_PRICE
        product_id = self._get_product_id(url)

        # 1. Retrieve current product state from persistence BEFORE the check
        product_record = self._db.get_product(product_id)
        if not product_record:
            product_record = self._db.get_or_create_product(
                product_id=product_id,
                name=prod_name,
                url=url,
                max_price=prod_max_price,
            )

        previous_state = AvailabilityState(product_record.last_known_availability)
        previous_consecutive = product_record.consecutive_in_stock_count

        # 2. Fetch observation from provider
        try:
            observation = self._provider.check_availability(url)
            self._db.record_observation(observation)
            self._db.update_health(
                is_success=True,
                availability=observation.availability,
                active_provider=self._provider.provider_name,
            )
        except Exception as exc:
            err_msg = f"Check failed: {exc}"
            logger.warning("Stock provider check failure: %s", exc)
            self._db.update_health(
                is_success=False,
                error_summary=err_msg,
                active_provider=self._provider.provider_name,
            )
            # Critical requirement: Network timeout/error must not overwrite valid state
            return None, None

        # 3. Handle UNKNOWN state
        if observation.availability == AvailabilityState.UNKNOWN:
            logger.info("Stock observation is UNKNOWN (inconclusive). No state transition applied.")
            return observation, None

        current_state = observation.availability

        # 4. Handle initial startup check
        is_first = self._first_checks.get(product_id, True)
        if is_first:
            self._first_checks[product_id] = False
            if previous_state == AvailabilityState.UNKNOWN:
                logger.info(
                    "Initial check completed. Baselining state to %s (Product: '%s').",
                    current_state.value,
                    product_record.name,
                )
                new_consecutive = 1 if current_state == AvailabilityState.IN_STOCK else 0
                self._db.update_product_state(
                    product_id,
                    availability=current_state,
                    consecutive_in_stock_count=new_consecutive,
                )
                if current_state == AvailabilityState.IN_STOCK and self._settings.INITIAL_ALERT_ON_STARTUP:
                    if (
                        prod_max_price is not None
                        and observation.price is not None
                        and observation.price > prod_max_price
                    ):
                        logger.info(
                            "Product '%s' in stock on startup, but price Rs. %s exceeds configured maximum Rs. %s. Alert suppressed.",
                            product_record.name,
                            observation.price,
                            prod_max_price,
                        )
                        return observation, None

                    logger.info("INITIAL_ALERT_ON_STARTUP is enabled; triggering initial restock alert.")
                    event = self._build_restock_event(product_record, observation, previous_state)
                    return observation, event
                return observation, None

        # 5. Handle transitions
        if current_state == AvailabilityState.OUT_OF_STOCK:
            self._db.update_product_state(
                product_id,
                availability=AvailabilityState.OUT_OF_STOCK,
                consecutive_in_stock_count=0,
            )
            if previous_state == AvailabilityState.IN_STOCK:
                logger.info("Product '%s' is now OUT OF STOCK.", product_record.name)
            return observation, None

        if current_state == AvailabilityState.IN_STOCK:
            new_consecutive = previous_consecutive + 1

            # Check price ceiling if configured and price is known
            if (
                prod_max_price is not None
                and observation.price is not None
                and observation.price > prod_max_price
            ):
                logger.info(
                    "Product '%s' in stock, but price Rs. %s exceeds configured maximum Rs. %s. Alert suppressed.",
                    product_record.name,
                    observation.price,
                    prod_max_price,
                )
                self._db.update_product_state(product_id, consecutive_in_stock_count=new_consecutive)
                return observation, None

            # Verify debouncing policy (consecutive in-stock counts)
            required_verifications = self._settings.VERIFICATION_COUNT
            if new_consecutive < required_verifications:
                logger.info(
                    "IN_STOCK observed (%s/%s confirmations). Waiting for confirmation.",
                    new_consecutive,
                    required_verifications,
                )
                self._db.update_product_state(product_id, consecutive_in_stock_count=new_consecutive)
                return observation, None

            # Verification threshold reached! Commit confirmed state to IN_STOCK
            self._db.update_product_state(
                product_id,
                availability=AvailabilityState.IN_STOCK,
                consecutive_in_stock_count=new_consecutive,
            )

            # Check if this is a transition from OUT_OF_STOCK
            if previous_state == AvailabilityState.OUT_OF_STOCK:
                # Check cooldown to prevent duplicate alerts
                if self._db.is_in_cooldown(product_id):
                    logger.info("Restock detected for '%s', but alert is currently in cooldown.", product_record.name)
                    return observation, None

                logger.info("RESTOCK DETECTED for product '%s'! Triggering alert.", product_record.name)
                event = self._build_restock_event(product_record, observation, previous_state)
                return observation, event
            elif previous_state == AvailabilityState.IN_STOCK:
                # Already in stock - suppress duplicate alert
                logger.debug("Product '%s' remains IN STOCK. Suppressing duplicate alert.", product_record.name)
                return observation, None

        return observation, None

    def _build_restock_event(
        self, product: ProductRecord, obs: StockObservation, previous_state: AvailabilityState
    ) -> RestockEvent:
        # Safely resolve product title, ensuring placeholders like {TITLE} or generic names
        # never override a clean, configured product name.
        resolved_title = resolve_product_title(
            primary=obs.product_name,
            secondary=product.name,
            url=product.url,
            fallback_default="Flipkart Monitored Product",
        )
        if is_valid_product_title(product.name) and (
            not is_valid_product_title(obs.product_name)
            or obs.product_name.strip().lower() in ("flipkart product", "flipkart listing", "flipkart")
        ):
            resolved_title = product.name.strip()

        event = RestockEvent(
            product_id=product.product_id,
            product_name=resolved_title,
            product_url=product.url,
            detected_at_utc=obs.observed_at_utc,
            price=obs.price,
            previous_state=previous_state,
            current_state=obs.availability,
            status=RestockEventStatus.DETECTED,
        )
        event_id = self._db.record_restock_event(event, self._settings.ALERT_COOLDOWN_SECONDS)
        event.id = event_id
        return event
