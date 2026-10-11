"""Stock providers package."""

from app.providers.base import StockProvider
from app.providers.factory import get_stock_provider
from app.providers.live_flipkart_affiliate import FlipkartAffiliateStockProvider
from app.providers.live_flipkart_web import FlipkartWebStockProvider
from app.providers.mock_provider import MockStockProvider

__all__ = [
    "StockProvider",
    "MockStockProvider",
    "FlipkartAffiliateStockProvider",
    "FlipkartWebStockProvider",
    "get_stock_provider",
]
