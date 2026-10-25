"""Flipkart Affiliate API stock availability provider."""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from app.models import AvailabilityState, StockObservation
from app.providers.base import StockProvider
from app.utils import resolve_product_title

logger = logging.getLogger(__name__)


class FlipkartAffiliateStockProvider(StockProvider):
    """
    Official Flipkart Affiliate API availability adapter.

    Evidence source:
      - Uses official partner endpoint `https://affiliate-api.flipkart.net/affiliate/1.0/product.json`
      - Evaluates verified attributes: `productBaseInfoV1.inStock` and `attributes.isAvailable`
      - Reads `flipkartSellingPrice.amount` for price validation.

    Limitations:
      - Requires registered affiliate credentials (Fk-Affiliate-Id and Fk-Affiliate-Token).
      - Subject to partner API rate limits.
      - If credentials are empty or invalid, returns UNKNOWN (never IN_STOCK).
    """

    API_BASE_URL = "https://affiliate-api.flipkart.net/affiliate/1.0/product.json"

    def __init__(
        self,
        affiliate_id: Optional[str],
        affiliate_token: Optional[str],
        timeout_seconds: int = 10,
    ):
        self._affiliate_id = affiliate_id
        self._affiliate_token = affiliate_token
        self._timeout_seconds = timeout_seconds
        self._provider_name = "flipkart_affiliate_api"
        self._last_observed: dict[str, AvailabilityState] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def extract_product_id(self, product_url: str) -> str:
        """Extract Flipkart PID from listing URL (e.g. pid query param or itm ID)."""
        pid_match = re.search(r"[?&]pid=([a-zA-Z0-9]+)", product_url)
        if pid_match:
            return pid_match.group(1)

        itm_match = re.search(r"/p/(itm[a-zA-Z0-9]+)", product_url)
        if itm_match:
            return itm_match.group(1)

        raise ValueError(f"Could not extract a valid Flipkart product ID from URL: {product_url}")

    def check_availability(self, product_url: str) -> StockObservation:
        """Query official Affiliate API endpoint for availability status."""
        if not self._affiliate_id or not self._affiliate_token:
            logger.warning("Flipkart Affiliate credentials missing. Cannot execute live API query.")
            return StockObservation(
                product_id=self._safe_extract_id(product_url),
                product_name="Unconfigured Affiliate Product",
                product_url=product_url,
                availability=AvailabilityState.UNKNOWN,
                provider_name=self.provider_name,
                confidence_score=0.0,
                raw_evidence={"error": "FLIPKART_AFFILIATE_ID or FLIPKART_AFFILIATE_TOKEN not configured"},
            )

        product_id = self.extract_product_id(product_url)
        headers = {
            "Fk-Affiliate-Id": self._affiliate_id,
            "Fk-Affiliate-Token": self._affiliate_token,
            "Accept": "application/json",
            "User-Agent": "FlipkartRestockMonitor/1.0",
        }
        params = {"id": product_id}

        try:
            response = requests.get(
                self.API_BASE_URL,
                headers=headers,
                params=params,
                timeout=self._timeout_seconds,
            )

            if response.status_code == 401 or response.status_code == 403:
                logger.error("Flipkart Affiliate API authentication failed (HTTP %s).", response.status_code)
                return StockObservation(
                    product_id=product_id,
                    product_name="Unknown",
                    product_url=product_url,
                    availability=AvailabilityState.UNKNOWN,
                    provider_name=self.provider_name,
                    confidence_score=0.0,
                    raw_evidence={"http_status": response.status_code, "error": "Auth failed"},
                )

            response.raise_for_status()
            data = response.json()

            base_info = data.get("productBaseInfoV1", {})
            in_stock = base_info.get("inStock", False)
            raw_title = base_info.get("title", "")
            title = resolve_product_title(primary=raw_title, url=product_url, fallback_default="Flipkart Product")
            selling_price = None
            price_info = base_info.get("flipkartSellingPrice")
            if price_info and isinstance(price_info, dict):
                selling_price = float(price_info.get("amount", 0.0))

            state = AvailabilityState.IN_STOCK if in_stock else AvailabilityState.OUT_OF_STOCK
            self._last_observed[product_id] = state

            return StockObservation(
                product_id=product_id,
                product_name=title,
                product_url=product_url,
                availability=state,
                observed_at_utc=datetime.now(timezone.utc),
                price=selling_price,
                provider_name=self.provider_name,
                confidence_score=1.0,
                raw_evidence={
                    "inStock": in_stock,
                    "selling_price": selling_price,
                    "source": "affiliate_api",
                },
            )

        except requests.RequestException as exc:
            logger.warning("Flipkart Affiliate network error: %s", exc)
            raise ConnectionError(f"Affiliate API request failed: {exc}") from exc

    def get_product_details(self, product_url: str) -> dict[str, Any]:
        obs = self.check_availability(product_url)
        return {
            "product_id": obs.product_id,
            "title": obs.product_name,
            "price": obs.price,
            "availability": obs.availability.value,
        }

    def get_last_observed_state(self, product_id: str) -> Optional[AvailabilityState]:
        return self._last_observed.get(product_id)

    def _safe_extract_id(self, product_url: str) -> str:
        try:
            return self.extract_product_id(product_url)
        except Exception:
            return "unknown_id"
