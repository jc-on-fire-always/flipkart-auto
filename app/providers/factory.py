"""Factory for instantiating the configured stock provider."""

from __future__ import annotations

import logging
from typing import Optional

from app.config import Settings
from app.models import AvailabilityState
from app.providers.base import StockProvider
from app.providers.live_flipkart_affiliate import FlipkartAffiliateStockProvider
from app.providers.live_flipkart_web import FlipkartWebStockProvider
from app.providers.mock_provider import MockStockProvider

logger = logging.getLogger(__name__)


def get_stock_provider(settings: Settings, custom_mock: Optional[StockProvider] = None) -> StockProvider:
    """Instantiate and return the stock provider based on application settings."""
    if custom_mock is not None:
        return custom_mock

    provider_type = settings.STOCK_PROVIDER_TYPE.lower()

    if provider_type == "mock":
        initial_state = (
            AvailabilityState.IN_STOCK
            if settings.MOCK_STOCK_STATE == "IN_STOCK"
            else AvailabilityState.OUT_OF_STOCK
        )
        logger.info("Initializing MockStockProvider (Simulated State=%s).", initial_state.value)
        return MockStockProvider(
            default_state=initial_state,
            product_name=settings.PRODUCT_NAME,
            price=settings.MAX_PRODUCT_PRICE or 1999.0,
        )
    elif provider_type == "flipkart_affiliate":
        logger.info("Initializing FlipkartAffiliateStockProvider using official Partner API.")
        return FlipkartAffiliateStockProvider(
            affiliate_id=settings.FLIPKART_AFFILIATE_ID,
            affiliate_token=settings.FLIPKART_AFFILIATE_TOKEN,
            timeout_seconds=settings.REQUEST_TIMEOUT_SECONDS,
        )
    elif provider_type == "flipkart_web":
        logger.warning(
            "Initializing FlipkartWebStockProvider. "
            "Note: Direct web inspection is subject to anti-bot challenges and may return UNKNOWN."
        )
        return FlipkartWebStockProvider(
            timeout_seconds=settings.REQUEST_TIMEOUT_SECONDS,
            default_product_name=settings.PRODUCT_NAME,
        )
    else:
        raise ValueError(f"Unknown STOCK_PROVIDER_TYPE: '{settings.STOCK_PROVIDER_TYPE}'")
