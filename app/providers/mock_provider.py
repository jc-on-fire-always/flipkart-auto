"""Mock stock provider for deterministic testing, development, and demonstrations."""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any, List, Optional

from app.models import AvailabilityState, StockObservation
from app.providers.base import StockProvider


class MockStockProvider(StockProvider):
    """
    Mock stock provider simulating Flipkart product availability transitions.

    Supports programmed sequences (e.g., OUT_OF_STOCK -> IN_STOCK),
    network error injection, and price reporting for testing and verification.
    """

    def __init__(
        self,
        default_state: AvailabilityState = AvailabilityState.OUT_OF_STOCK,
        product_name: str = "Mock Flipkart Product",
        price: Optional[float] = 69999.0,
    ):
        self._provider_name = "mock_provider"
        self._current_state = default_state
        self._product_name = product_name
        self._price = price
        self._state_sequence: List[AvailabilityState] = []
        self._sequence_index = 0
        self._raise_network_error = False
        self._raise_malformed_error = False
        self._last_observed: dict[str, AvailabilityState] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def set_state(self, state: AvailabilityState, price: Optional[float] = None) -> None:
        """Explicitly set current stock state."""
        self._current_state = state
        if price is not None:
            self._price = price

    def set_sequence(self, sequence: List[AvailabilityState]) -> None:
        """Program a sequence of states returned on successive calls."""
        self._state_sequence = sequence
        self._sequence_index = 0

    def trigger_network_error_next(self, enable: bool = True) -> None:
        """Simulate a network timeout or connection reset on next check."""
        self._raise_network_error = enable

    def trigger_malformed_error_next(self, enable: bool = True) -> None:
        """Simulate a malformed / unparseable response on next check."""
        self._raise_malformed_error = enable

    def extract_product_id(self, product_url: str) -> str:
        """Extract Flipkart PID or fallback to URL hash."""
        match = re.search(r"/p/(itm[a-zA-Z0-9]+)", product_url)
        if match:
            return match.group(1)
        # Fallback to sanitized URL slug
        clean = re.sub(r"[^a-zA-Z0-9]", "_", product_url)
        return clean[:32]

    def check_availability(self, product_url: str) -> StockObservation:
        if self._raise_network_error:
            self._raise_network_error = False
            raise ConnectionError("Simulated network timeout connecting to stock provider")

        if self._raise_malformed_error:
            self._raise_malformed_error = False
            raise ValueError("Simulated malformed JSON response from stock source")

        # Determine state from programmed sequence or static state
        if self._state_sequence and self._sequence_index < len(self._state_sequence):
            state = self._state_sequence[self._sequence_index]
            self._sequence_index += 1
        else:
            state = self._current_state

        product_id = self.extract_product_id(product_url)
        self._last_observed[product_id] = state

        return StockObservation(
            product_id=product_id,
            product_name=self._product_name,
            product_url=product_url,
            availability=state,
            observed_at_utc=datetime.now(timezone.utc),
            source_timestamp=datetime.now(timezone.utc),
            price=self._price if state == AvailabilityState.IN_STOCK else None,
            provider_name=self.provider_name,
            confidence_score=1.0,
            raw_evidence={
                "mock": True,
                "state": state.value,
                "sequence_index": self._sequence_index,
            },
        )

    def get_product_details(self, product_url: str) -> dict[str, Any]:
        product_id = self.extract_product_id(product_url)
        return {
            "product_id": product_id,
            "title": self._product_name,
            "price": self._price,
            "url": product_url,
        }

    def get_last_observed_state(self, product_id: str) -> Optional[AvailabilityState]:
        return self._last_observed.get(product_id)
