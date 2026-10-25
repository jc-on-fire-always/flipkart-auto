"""Utility helper functions for title validation, URL parsing, and sanitization."""

from __future__ import annotations

import re
from typing import Optional
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse, urlunparse

# Generic or placeholder strings that should never be displayed as product titles
GENERIC_PLACEHOLDERS = frozenset(
    {
        "title",
        "{title}",
        "product title",
        "{product_title}",
        "product_title",
        "product name",
        "{product_name}",
        "product_name",
        "name",
        "{name}",
        "flipkart product",
        "flipkart listing",
        "flipkart.com",
        "flipkart",
        "search icon",
        "search",
        "access denied",
        "security challenge",
        "online shopping",
        "online shopping india",
        "unknown",
        "none",
        "null",
        "undefined",
    }
)


def is_valid_product_title(title: Optional[str]) -> bool:
    """
    Determine whether a string is a valid, descriptive product title.

    Rejects template placeholders (e.g., '{TITLE}', '{title}', '{{TITLE}}'),
    generic fallback strings ('Flipkart Product', 'Search Icon'), and empty or malformed strings.
    """
    if not title or not isinstance(title, str):
        return False

    cleaned = title.strip()
    if len(cleaned) < 2:
        return False

    # Check for template placeholder brackets or URL/HTML-encoded equivalents
    if any(char in cleaned for char in ("{", "}", "<", ">")):
        return False

    cleaned_upper = cleaned.upper()
    if "%7B" in cleaned_upper or "%7D" in cleaned_upper or "&#123;" in cleaned or "&LCUB;" in cleaned_upper:
        return False

    if "{TITLE}" in cleaned_upper or "TITLE" == cleaned_upper:
        return False

    # Check exact match against generic placeholder phrases
    cleaned_lower = cleaned.lower()
    if cleaned_lower in GENERIC_PLACEHOLDERS:
        return False

    # Check if the title starts and ends with bracket/delimiter pairs like [TITLE]
    if re.match(r"^[\{\<\[].*?[\}\>\]]$", cleaned):
        return False

    return True


def extract_title_from_flipkart_url(url: Optional[str]) -> Optional[str]:
    """
    Derive a clean, human-readable product title from a Flipkart URL slug.

    Examples:
      'https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c'
      -> 'Puma St Runner V4 Sd Sneakers Men'

      'https://www.flipkart.com/numero-uno-loose-fit-men-blue-jeans/p/itm0d72205b89486'
      -> 'Numero Uno Loose Fit Men Blue Jeans'
    """
    if not url or not isinstance(url, str):
        return None

    try:
        parsed = urlparse(url)
        path = unquote(parsed.path).strip("/")
        parts = [p for p in path.split("/") if p]
        if not parts:
            return None

        slug = parts[0]
        # Ignore system endpoints, search URLs, or non-product slugs
        reserved_slugs = {
            "p",
            "search",
            "store",
            "account",
            "checkout",
            "rv",
            "view-cart",
            "orders",
            "plus",
        }
        if slug.lower() in reserved_slugs:
            return None

        # Replace hyphens and underscores with spaces
        words = re.split(r"[-_]+", slug)
        words = [w.strip() for w in words if w.strip()]
        if not words:
            return None

        # Reconstruct title with proper capitalization
        formatted_words: list[str] = []
        brand_acronyms = {"puma", "nike", "asus", "hp", "dell", "lenovo", "rgb", "sd", "v4", "v5", "v3", "v2", "led", "tv", "gb", "mb", "tb", "ssd", "ram", "cpu", "gpu", "usb"}
        for word in words:
            w_lower = word.lower()
            if w_lower in brand_acronyms:
                formatted_words.append(w_lower.upper())
            else:
                formatted_words.append(word.capitalize())

        candidate = " ".join(formatted_words).strip()
        if is_valid_product_title(candidate):
            return candidate
    except Exception:
        pass

    return None


def resolve_product_title(
    primary: Optional[str] = None,
    secondary: Optional[str] = None,
    url: Optional[str] = None,
    fallback_default: str = "Flipkart Monitored Product",
) -> str:
    """
    Safely resolve the best human-readable product title through priority cascading.

    Priority:
      1. Primary title (if valid, descriptive, and non-generic)
      2. Secondary title (e.g. user-configured product name)
      3. Title extracted from the product URL slug
      4. Fallback default string
    """
    if is_valid_product_title(primary):
        return primary.strip()  # type: ignore[union-attr]

    if is_valid_product_title(secondary):
        return secondary.strip()  # type: ignore[union-attr]

    from_url = extract_title_from_flipkart_url(url)
    if from_url:
        return from_url

    return fallback_default


def clean_flipkart_url(url: Optional[str]) -> str:
    """
    Strip tracking, affiliate, and analytics query parameters from a Flipkart product URL,
    leaving a clean canonical link with the essential product identifier (e.g., pid).

    Example:
      'https://www.flipkart.com/.../p/itm...?pid=SHOH...&marketplace=FLIPKART&lid=...&q=...'
      -> 'https://www.flipkart.com/.../p/itm...?pid=SHOH...'
    """
    if not url or not isinstance(url, str):
        return ""

    try:
        parsed = urlparse(url)
        if "flipkart.com" not in parsed.netloc.lower():
            return url.strip()

        qs = parse_qs(parsed.query)
        clean_params = {}
        if "pid" in qs and qs["pid"]:
            clean_params["pid"] = qs["pid"][0]

        new_query = urlencode(clean_params)
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", new_query, ""))
    except Exception:
        return url.strip()


def shorten_url(url: Optional[str], timeout_seconds: float = 3.0) -> str:
    """
    Shorten a URL using pyshorteners (TinyURL) with automatic fallback to cleaned canonical URL.

    - First strips 100-200 characters of tracking parameters.
    - Attempts shortening via pyshorteners (TinyURL) within bounded timeout.
    - If network fails, times out, or library is unavailable, gracefully falls back
      to the cleaned Flipkart canonical URL to guarantee delivery is never blocked.
    """
    if not url or not isinstance(url, str):
        return ""

    cleaned = clean_flipkart_url(url)
    try:
        import pyshorteners

        s = pyshorteners.Shortener(timeout=timeout_seconds)
        shortened = s.tinyurl.short(cleaned)
        if shortened and isinstance(shortened, str) and shortened.startswith("http"):
            return shortened.strip()
    except Exception:
        pass

    # Secondary fallback via direct requests
    try:
        import requests

        api_url = f"https://tinyurl.com/api-create.php?url={quote(cleaned)}"
        resp = requests.get(api_url, timeout=timeout_seconds)
        if resp.status_code == 200 and resp.text.strip().startswith("http"):
            return resp.text.strip()
    except Exception:
        pass

    return cleaned

