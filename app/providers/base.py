"""Abstract base class defining the stock provider interface."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Optional

from app.models import AvailabilityState, StockObservation


class StockProvider(ABC):
    """Abstract contract for availability detection sources."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Name of the availability source adapter."""
        pass

    @abstractmethod
    def check_availability(self, product_url: str) -> StockObservation:
        """
        Check current stock availability for the given Flipkart product URL.

        Returns a StockObservation model.
        Must NEVER return IN_STOCK when the status cannot be conclusively established.
        """
        pass

    @abstractmethod
    def get_product_details(self, product_url: str) -> dict[str, Any]:
        """Fetch descriptive product metadata (title, product ID, current listed price)."""
        pass

    @abstractmethod
    def get_last_observed_state(self, product_id: str) -> Optional[AvailabilityState]:
        """Return the last recorded state for this product from the provider cache if applicable."""
        pass
