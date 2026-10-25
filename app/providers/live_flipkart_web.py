"""Flipkart Web product inspector availability adapter."""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any, Optional

import requests

from app.models import AvailabilityState, StockObservation
from app.providers.base import StockProvider
from app.utils import extract_title_from_flipkart_url, is_valid_product_title, resolve_product_title

logger = logging.getLogger(__name__)


class FlipkartWebStockProvider(StockProvider):
    """
    Direct web inspection adapter for Flipkart listings.

    Evidence evaluation:
      - Inspects schema.org JSON-LD structured metadata embedded in page source.
      - Checks for definitive availability signals (e.g., 'http://schema.org/InStock' vs 'http://schema.org/OutOfStock').
      - Scans for prominent status text: 'Currently Out of Stock', 'Sold Out'.
      - Scans for active checkout triggers: 'BUY NOW', 'ADD TO CART'.

    Limitations & Security Constraints:
      - Subject to Akamai bot management, dynamic DOM hydration, and regional inventory differences.
      - Never bypasses CAPTCHA or anti-bot defenses.
      - If a challenge page or anti-bot block is received, returns UNKNOWN with 0.0 confidence.
      - UNKNOWN is strictly treated as non-purchasable.
    """

    DEFAULT_USER_AGENT = (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
    )

    def __init__(self, timeout_seconds: int = 15, default_product_name: Optional[str] = None):
        self._timeout_seconds = timeout_seconds
        self._default_product_name = default_product_name
        self._provider_name = "flipkart_web_inspector"
        self._last_observed: dict[str, AvailabilityState] = {}

    @property
    def provider_name(self) -> str:
        return self._provider_name

    def extract_product_id(self, product_url: str) -> str:
        """Extract Flipkart PID or unique slug."""
        match = re.search(r"/p/(itm[a-zA-Z0-9]+)", product_url)
        if match:
            return match.group(1)
        pid = re.search(r"[?&]pid=([a-zA-Z0-9]+)", product_url)
        if pid:
            return pid.group(1)
        clean = re.sub(r"[^a-zA-Z0-9]", "_", product_url)
        return clean[:32]

    def _extract_title_and_price(
        self, html: str, state_str: Optional[str] = None, product_url: Optional[str] = None
    ) -> tuple[str, Optional[float]]:
        """Accurately extract the true product title and price, ignoring sponsored ads and template placeholders."""
        title: Optional[str] = None
        price: Optional[float] = None

        if state_str is None:
            state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>", html, re.DOTALL)
            state_str = state_match.group(1) if state_match else None

        if state_str:
            try:
                state = json.loads(state_str)
                slots = state.get("multiWidgetState", {}).get("widgetsData", {}).get("slots", [])
                for s in slots:
                    widget = s.get("slotData", {}).get("widget", {})
                    imp = widget.get("tracking", {}).get("impressionId", "")
                    type_name = widget.get("type", "")
                    dls = widget.get("data", {}).get("dlsData", {})

                    # Extract true title from ATLAS_PRODUCT_TITLE widget
                    if ("PRODUCT_TITLE" in imp or "PRODUCT_TITLE" in type_name) and not title:
                        candidates = [
                            dls.get("customEllipsisData_0", {}).get("value", {}).get("prependingText"),
                            dls.get("title_0", {}).get("value", {}).get("text"),
                            dls.get("name_0", {}).get("value", {}).get("text"),
                            dls.get("title", {}).get("value", {}).get("text"),
                        ]
                        for c in candidates:
                            if is_valid_product_title(c):
                                title = c.strip()
                                break

                    # Extract true selling price from ATLAS_PRODUCT_PRICING_SUMMARY widget
                    if ("PRICING_SUMMARY" in imp or "PRODUCT_PRICING" in imp) and price is None:
                        pd = dls.get("price_description_0", {}).get("value", {})
                        label_2 = pd.get("label_2", {})
                        val_obj = label_2.get("value", {})
                        text_val = None
                        if isinstance(val_obj, dict):
                            if "LOCKED" in val_obj:
                                text_val = val_obj.get("LOCKED", {}).get("value", {}).get("text")
                            elif "UNLOCKED" in val_obj:
                                text_val = val_obj.get("UNLOCKED", {}).get("value", {}).get("text")
                        if not text_val and isinstance(label_2.get("value"), dict):
                            text_val = label_2.get("value", {}).get("text")

                        if text_val:
                            nums = re.findall(r'[0-9]+', text_val.replace(',', ''))
                            if nums:
                                price = float(nums[0])

                        # Fallback within the pricing summary widget
                        if price is None:
                            def find_rupee_in_dict(d: Any) -> Optional[float]:
                                if isinstance(d, dict):
                                    for v in d.values():
                                        res = find_rupee_in_dict(v)
                                        if res is not None:
                                            return res
                                elif isinstance(d, str) and ('\u20b9' in d or '₹' in d):
                                    m = re.search(r'[\u20b9₹]\s*([0-9,]+)', d)
                                    if m:
                                        return float(m.group(1).replace(',', ''))
                                return None

                            price = find_rupee_in_dict(dls)

            except Exception as exc:
                logger.debug("Non-fatal error parsing INITIAL_STATE JSON: %s", exc)

        # Fallback price from HTML Flipkart price classes
        if price is None:
            class_m = re.search(
                r'class=["\'][^"\']*(?:Nx9bqj\s+CxhGGd|_30jeq3\s+_16J0da|Nx9bqj|_30jeq3)[^"\']*["\'][^>]*>[\u20b9₹]?\s*([0-9,]+)',
                html,
            )
            if class_m:
                price = float(class_m.group(1).replace(',', ''))

        # Fallback title from OpenGraph, H1, or <title>
        if not is_valid_product_title(title):
            cand: Optional[str] = None
            og_match = re.search(r'<meta\s+(?:property|name)=["\']og:title["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
            if og_match:
                c = og_match.group(1).split("|")[0].split(" - Buy")[0].strip()
                if is_valid_product_title(c):
                    cand = c

            if not cand:
                twitter_match = re.search(r'<meta\s+name=["\']twitter:title["\']\s+content=["\'](.*?)["\']', html, re.IGNORECASE)
                if twitter_match:
                    c = twitter_match.group(1).split("|")[0].split(" - Buy")[0].strip()
                    if is_valid_product_title(c):
                        cand = c

            if not cand:
                h1_match = re.search(r'<h1[^>]*>(.*?)</h1>', html, re.IGNORECASE | re.DOTALL)
                if h1_match:
                    c = re.sub(r'<[^>]+>', '', h1_match.group(1)).split("|")[0].split(" - Buy")[0].strip()
                    if is_valid_product_title(c):
                        cand = c

            if not cand:
                for tm in re.finditer(r"<title>(.*?)</title>", html, re.IGNORECASE):
                    raw_title = tm.group(1).strip()
                    c = raw_title.split("|")[0].split(" - Buy")[0].strip()
                    if is_valid_product_title(c):
                        cand = c
                        break

            title = resolve_product_title(
                primary=cand,
                secondary=self._default_product_name,
                url=product_url,
                fallback_default=self._default_product_name or "Flipkart Product",
            )

        return title, price

    def check_availability(self, product_url: str) -> StockObservation:
        """Inspect Flipkart product page for stock availability."""
        product_id = self.extract_product_id(product_url)
        headers = {
            "User-Agent": self.DEFAULT_USER_AGENT,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9",
            "Cache-Control": "no-cache",
        }

        try:
            response = requests.get(product_url, headers=headers, timeout=self._timeout_seconds, allow_redirects=True)

            # Check for actual bot blocks or access denials
            html_lower = response.text.lower()
            if (
                response.status_code in (403, 429)
                or "<title>access denied</title>" in html_lower
                or "please verify you are a human" in html_lower
                or "security challenge" in html_lower
            ):
                logger.warning("Flipkart web request blocked by anti-bot challenge (status %s). Returning UNKNOWN.", response.status_code)
                return StockObservation(
                    product_id=product_id,
                    product_name="Flipkart Listing",
                    product_url=product_url,
                    availability=AvailabilityState.UNKNOWN,
                    provider_name=self.provider_name,
                    confidence_score=0.0,
                    raw_evidence={"http_status": response.status_code, "reason": "Bot challenge encountered"},
                )

            if response.status_code != 200:
                logger.warning("Unexpected HTTP response %s from Flipkart product page.", response.status_code)
                return StockObservation(
                    product_id=product_id,
                    product_name="Flipkart Listing",
                    product_url=product_url,
                    availability=AvailabilityState.UNKNOWN,
                    provider_name=self.provider_name,
                    confidence_score=0.0,
                    raw_evidence={"http_status": response.status_code},
                )

            html = response.text
            state_match = re.search(r"window\.__INITIAL_STATE__\s*=\s*({.*?});\s*</script>", html, re.DOTALL)
            state_str = state_match.group(1) if state_match else None
            title, price = self._extract_title_and_price(html, state_str=state_str, product_url=product_url)

            # 1. Inspect window.__INITIAL_STATE__ (Most accurate Flipkart server-rendered state)
            if state_str:

                # Check schema.org availability within state
                if re.search(r'["\']availability["\']:\s*["\'][^"\']*OutOfStock["\']', state_str, re.IGNORECASE):
                    self._last_observed[product_id] = AvailabilityState.OUT_OF_STOCK
                    return StockObservation(
                        product_id=product_id,
                        product_name=title,
                        product_url=product_url,
                        availability=AvailabilityState.OUT_OF_STOCK,
                        observed_at_utc=datetime.now(timezone.utc),
                        price=price,
                        provider_name=self.provider_name,
                        confidence_score=0.98,
                        raw_evidence={"source": "initial_state_schema", "state": "OutOfStock", "price": price},
                    )
                elif re.search(r'["\']availability["\']:\s*["\'][^"\']*InStock["\']', state_str, re.IGNORECASE):
                    self._last_observed[product_id] = AvailabilityState.IN_STOCK
                    return StockObservation(
                        product_id=product_id,
                        product_name=title,
                        product_url=product_url,
                        availability=AvailabilityState.IN_STOCK,
                        observed_at_utc=datetime.now(timezone.utc),
                        price=price,
                        provider_name=self.provider_name,
                        confidence_score=0.98,
                        raw_evidence={"source": "initial_state_schema", "state": "InStock", "price": price},
                    )

                # Check isAvailable flag
                if re.search(r'["\']isAvailable["\']:\s*false', state_str, re.IGNORECASE):
                    self._last_observed[product_id] = AvailabilityState.OUT_OF_STOCK
                    return StockObservation(
                        product_id=product_id,
                        product_name=title,
                        product_url=product_url,
                        availability=AvailabilityState.OUT_OF_STOCK,
                        observed_at_utc=datetime.now(timezone.utc),
                        price=price,
                        provider_name=self.provider_name,
                        confidence_score=0.95,
                        raw_evidence={"source": "initial_state_isAvailable", "state": "false"},
                    )
                elif re.search(r'["\']isAvailable["\']:\s*true', state_str, re.IGNORECASE):
                    self._last_observed[product_id] = AvailabilityState.IN_STOCK
                    return StockObservation(
                        product_id=product_id,
                        product_name=title,
                        product_url=product_url,
                        availability=AvailabilityState.IN_STOCK,
                        observed_at_utc=datetime.now(timezone.utc),
                        price=price,
                        provider_name=self.provider_name,
                        confidence_score=0.95,
                        raw_evidence={"source": "initial_state_isAvailable", "state": "true", "price": price},
                    )

            # 2. Inspect embedded Schema.org JSON-LD scripts
            json_ld_state, json_ld_price, json_ld_title = self._parse_json_ld(html)
            if json_ld_state is not None:
                self._last_observed[product_id] = json_ld_state
                resolved_title = resolve_product_title(
                    primary=json_ld_title,
                    secondary=title,
                    url=product_url,
                    fallback_default=self._default_product_name or "Flipkart Product",
                )
                return StockObservation(
                    product_id=product_id,
                    product_name=resolved_title,
                    product_url=product_url,
                    availability=json_ld_state,
                    observed_at_utc=datetime.now(timezone.utc),
                    price=json_ld_price,
                    provider_name=self.provider_name,
                    confidence_score=0.95,
                    raw_evidence={"source": "json_ld", "state": json_ld_state.value, "price": json_ld_price},
                )

            # 3. Inspect visible page text heuristics
            is_out_of_stock = (
                "currently out of stock" in html_lower
                or "sold out" in html_lower
                or "notify me" in html_lower
                or "temporarily unavailable" in html_lower
                or "this product is currently out of stock" in html_lower
            )

            has_buy_button = (
                "buy now" in html_lower
                or "add to cart" in html_lower
            )

            # Extract price if not already determined
            if price is None:
                price_match = re.search(r"₹\s*([0-9,]+)", html)
                if price_match:
                    try:
                        price = float(price_match.group(1).replace(",", ""))
                    except ValueError:
                        price = None

            if is_out_of_stock:
                state = AvailabilityState.OUT_OF_STOCK
                conf = 0.90
            elif has_buy_button and not is_out_of_stock:
                state = AvailabilityState.IN_STOCK
                conf = 0.85
            else:
                state = AvailabilityState.UNKNOWN
                conf = 0.0

            self._last_observed[product_id] = state
            return StockObservation(
                product_id=product_id,
                product_name=title,
                product_url=product_url,
                availability=state,
                observed_at_utc=datetime.now(timezone.utc),
                price=price if state == AvailabilityState.IN_STOCK else None,
                provider_name=self.provider_name,
                confidence_score=conf,
                raw_evidence={"heuristics": True, "out_of_stock_found": is_out_of_stock, "buy_button_found": has_buy_button},
            )

        except requests.RequestException as exc:
            logger.warning("Network failure during web inspection of %s: %s", product_url, exc)
            raise ConnectionError(f"HTTP request to Flipkart listing failed: {exc}") from exc

    def _parse_json_ld(self, html: str) -> tuple[Optional[AvailabilityState], Optional[float], Optional[str]]:
        """Look for schema.org Product JSON-LD blocks."""
        pattern = re.compile(r'<script\s+type=["\']application/ld\+json["\']>(.*?)</script>', re.DOTALL | re.IGNORECASE)
        for match in pattern.finditer(html):
            snippet = match.group(1).strip()
            try:
                data = json.loads(snippet)
                if isinstance(data, list):
                    items = data
                else:
                    items = [data]

                for item in items:
                    if isinstance(item, dict) and item.get("@type") == "Product":
                        raw_title = item.get("name")
                        title = str(raw_title).strip() if is_valid_product_title(raw_title) else None
                        offers = item.get("offers", {})
                        if isinstance(offers, list) and offers:
                            offers = offers[0]
                        availability_str = str(offers.get("availability", "")).lower()
                        price = None
                        if "price" in offers:
                            try:
                                price = float(offers["price"])
                            except (ValueError, TypeError):
                                pass

                        if "instock" in availability_str:
                            return AvailabilityState.IN_STOCK, price, title
                        elif "outofstock" in availability_str:
                            return AvailabilityState.OUT_OF_STOCK, price, title
            except Exception:
                continue

        return None, None, None

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
