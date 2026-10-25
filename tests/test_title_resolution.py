"""Tests for product title validation, placeholder rejection, and safe resolution."""

import pytest
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from app.models import AvailabilityState, ProductRecord, RestockEvent, StockObservation
from app.notifications.email import EmailNotifier
from app.notifications.whatsapp import WhatsAppNotifier
from app.providers.live_flipkart_web import FlipkartWebStockProvider
from app.services.stock_service import StockService
from app.utils import (
    clean_flipkart_url,
    extract_title_from_flipkart_url,
    is_valid_product_title,
    resolve_product_title,
    shorten_url,
)


@pytest.mark.parametrize(
    "placeholder",
    [
        "{TITLE}",
        "{title}",
        "{{TITLE}}",
        "{PRODUCT_TITLE}",
        "{product_title}",
        "{NAME}",
        "{name}",
        "[TITLE]",
        "%7BTITLE%7D",
        "&#123;TITLE&#125;",
        "Flipkart Product",
        "Flipkart Listing",
        "Flipkart.com",
        "Search Icon",
        "Search",
        "Unknown",
        "none",
        "",
        "   ",
        None,
    ],
)
def test_is_valid_product_title_rejects_placeholders(placeholder):
    """Ensure template placeholders and generic strings are strictly rejected."""
    assert not is_valid_product_title(placeholder)


@pytest.mark.parametrize(
    "valid_title",
    [
        "PUMA ST Runner v4 SD Sneakers For Men",
        "Apple iPhone 15 (Black, 128 GB)",
        "NUMERO UNO Men Loose Fit Mid Rise Blue Jeans",
        "Sony WH-1000XM5 Wireless Headphones",
    ],
)
def test_is_valid_product_title_accepts_real_titles(valid_title):
    """Ensure legitimate product names are accepted."""
    assert is_valid_product_title(valid_title)


def test_extract_title_from_flipkart_url():
    """Verify that URL slugs are converted to clean, capitalized titles."""
    url1 = "https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c?pid=SHOHDMPE"
    assert extract_title_from_flipkart_url(url1) == "PUMA St Runner V4 SD Sneakers Men"

    url2 = "https://www.flipkart.com/numero-uno-loose-fit-men-blue-jeans/p/itm0d72205b89486"
    assert extract_title_from_flipkart_url(url2) == "Numero Uno Loose Fit Men Blue Jeans"

    # Non-product or root URL should return None
    assert extract_title_from_flipkart_url("https://www.flipkart.com/p/itm123") is None
    assert extract_title_from_flipkart_url("https://www.flipkart.com/") is None
    assert extract_title_from_flipkart_url(None) is None


def test_resolve_product_title_priority():
    """Verify cascading fallback resolution."""
    url = "https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c"

    # 1. Primary valid title is used
    assert (
        resolve_product_title("Scraped Title", "Config Title", url)
        == "Scraped Title"
    )

    # 2. When primary is {TITLE}, fallback to secondary
    assert (
        resolve_product_title("{TITLE}", "Config Title", url)
        == "Config Title"
    )

    # 3. When primary and secondary are invalid, fallback to URL slug
    assert (
        resolve_product_title("{TITLE}", "Flipkart Product", url)
        == "PUMA St Runner V4 SD Sneakers Men"
    )

    # 4. When everything is invalid, fallback to default
    assert (
        resolve_product_title("{TITLE}", "Flipkart Product", None, "Default Product")
        == "Default Product"
    )


def test_parse_json_ld_filters_out_title_placeholder():
    """Ensure JSON-LD parser does not extract '{TITLE}' placeholder as valid name."""
    provider = FlipkartWebStockProvider()
    html_with_placeholder = """
    <script type="application/ld+json">
    {
        "@context": "https://schema.org",
        "@type": "Product",
        "name": "{TITLE}",
        "offers": {
            "@type": "Offer",
            "availability": "https://schema.org/InStock",
            "price": "2499"
        }
    }
    </script>
    """
    state, price, title = provider._parse_json_ld(html_with_placeholder)
    assert state == AvailabilityState.IN_STOCK
    assert price == 2499.0
    # Must NOT be '{TITLE}'
    assert title is None


def test_extract_title_and_price_filters_template_html():
    """Ensure _extract_title_and_price does not return '{TITLE}' from og:title or title tags."""
    provider = FlipkartWebStockProvider()
    html_with_template = """
    <html>
    <head>
      <meta property="og:title" content="{TITLE} Online at Best Prices in India">
      <title>{TITLE} - Buy {TITLE} Online at Best Prices In India | Flipkart.com</title>
    </head>
    <body>
      <div class="Nx9bqj">₹2,499</div>
    </body>
    </html>
    """
    url = "https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c"
    title, price = provider._extract_title_and_price(html_with_template, product_url=url)

    assert price == 2499.0
    assert "{TITLE}" not in title
    assert "{" not in title
    assert title == "PUMA St Runner V4 SD Sneakers Men"


def test_stock_service_build_restock_event_never_uses_placeholder(base_settings, db_manager, mock_stock_provider):
    """Ensure StockService._build_restock_event prefers configured product.name over placeholder in observation."""
    service = StockService(base_settings, db_manager, mock_stock_provider)

    product = db_manager.get_or_create_product(
        product_id="itm_test_123",
        name="PUMA ST Runner v4 SD Sneakers For Men",
        url="https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c",
    )
    # Simulate an observation that was returned with '{TITLE}'
    obs = StockObservation(
        product_id="itm_test_123",
        product_name="{TITLE}",
        product_url=product.url,
        availability=AvailabilityState.IN_STOCK,
        observed_at_utc=datetime.now(timezone.utc),
        price=2199.0,
        provider_name="flipkart_web_inspector",
        confidence_score=0.95,
    )

    event = service._build_restock_event(product, obs, AvailabilityState.OUT_OF_STOCK)
    assert "{TITLE}" not in event.product_name
    assert event.product_name == "PUMA ST Runner v4 SD Sneakers For Men"


def test_email_notifier_sanitizes_title_placeholder(base_settings):
    """Ensure EmailNotifier never transmits '{TITLE}' in subject or body even if event has placeholder."""
    notifier = EmailNotifier(base_settings)
    event = RestockEvent(
        product_id="itm_test_123",
        product_name="{TITLE}",
        product_url="https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c",
        detected_at_utc=datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc),
        price=2199.0,
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )

    with patch("smtplib.SMTP") as mock_smtp:
        mock_instance = MagicMock()
        mock_smtp.return_value = mock_instance

        res = notifier.send_restock_alert(event)
        from app.models import NotificationStatus
        assert res.status == NotificationStatus.ACCEPTED

        # Inspect the message sent
        send_args = mock_instance.sendmail.call_args[0]
        raw_msg = send_args[2]

        import email
        from email.header import decode_header, make_header
        msg_obj = email.message_from_string(raw_msg)
        decoded_subject = str(make_header(decode_header(msg_obj["Subject"])))
        assert "{TITLE}" not in decoded_subject
        assert "PUMA St Runner V4 SD Sneakers Men" in decoded_subject

        # Inspect MIME parts
        for part in msg_obj.walk():
            payload = part.get_payload(decode=True)
            if payload:
                text_content = payload.decode("utf-8")
                assert "{TITLE}" not in text_content
                assert "PUMA St Runner V4 SD Sneakers Men" in text_content


def test_whatsapp_notifier_sanitizes_title_placeholder(base_settings):
    """Ensure WhatsAppNotifier resolves product title safely instead of forwarding placeholder."""
    notifier = WhatsAppNotifier(base_settings)
    event = RestockEvent(
        product_id="itm_test_123",
        product_name="{TITLE}",
        product_url="https://www.flipkart.com/puma-st-runner-v4-sd-sneakers-men/p/itmaae3d5af4902c",
        detected_at_utc=datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc),
        price=2199.0,
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"messages": [{"id": "wamid.12345"}]}
        mock_post.return_value = mock_resp

        res = notifier.send_restock_alert(event)
        from app.models import NotificationStatus
        assert res.status == NotificationStatus.ACCEPTED

        # Check payload
        called_payload = mock_post.call_args[1]["json"]
        payload_str = str(called_payload)
        assert "{TITLE}" not in payload_str
        assert "PUMA St Runner V4 SD Sneakers Men" in payload_str


def test_clean_flipkart_url():
    """Ensure Flipkart URLs are stripped of tracking query params while preserving pid."""
    raw_url = (
        "https://www.flipkart.com/puma-st-runner-v4-mesh-sneakers-men/p/itm7c49a8e84c1fc"
        "?pid=SHOHDMPEUE5GEJBN&marketplace=FLIPKART&lid=LSTSHOHDMPEUE5GEJBNXO9BMI"
        "&q=puma+st+runner+v4&fm=search-autosuggest&swatchAttr=size&pageUID=1792924781519"
    )
    cleaned = clean_flipkart_url(raw_url)
    assert cleaned == "https://www.flipkart.com/puma-st-runner-v4-mesh-sneakers-men/p/itm7c49a8e84c1fc?pid=SHOHDMPEUE5GEJBN"
    assert "marketplace" not in cleaned
    assert "pageUID" not in cleaned

    # Non-Flipkart or empty URL
    assert clean_flipkart_url("https://example.com/item?foo=bar") == "https://example.com/item?foo=bar"
    assert clean_flipkart_url("") == ""


def test_shorten_url_success():
    """Ensure shorten_url uses pyshorteners or TinyURL successfully."""
    raw_url = "https://www.flipkart.com/puma-shoes/p/itm123?pid=SHOE123&track=bad"
    with patch("pyshorteners.Shortener") as mock_shortener_cls:
        mock_instance = MagicMock()
        mock_instance.tinyurl.short.return_value = "https://tinyurl.com/mock123"
        mock_shortener_cls.return_value = mock_instance

        shortened = shorten_url(raw_url)
        assert shortened == "https://tinyurl.com/mock123"


def test_shorten_url_fallback_on_network_failure():
    """Ensure shorten_url safely falls back to clean Flipkart URL when shortener fails."""
    raw_url = "https://www.flipkart.com/puma-shoes/p/itm123?pid=SHOE123&track=bad"
    with patch("pyshorteners.Shortener", side_effect=Exception("Connection timed out")):
        with patch("requests.get", side_effect=Exception("API down")):
            shortened = shorten_url(raw_url)
            assert shortened == "https://www.flipkart.com/puma-shoes/p/itm123?pid=SHOE123"


def test_whatsapp_notifier_sends_shortened_url(base_settings):
    """Ensure WhatsApp restock alert includes shortened URL in both template and text payloads."""
    event = RestockEvent(
        product_id="itm_shoes_99",
        product_name="PUMA Running Shoes",
        product_url="https://www.flipkart.com/puma-shoes/p/itm99?pid=SHOE99&marketplace=FLIPKART&lid=LST123",
        detected_at_utc=datetime(2026, 10, 25, 12, 0, 0, tzinfo=timezone.utc),
        price=1999.0,
        previous_state=AvailabilityState.OUT_OF_STOCK,
        current_state=AvailabilityState.IN_STOCK,
    )

    with patch("requests.post") as mock_post:
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"messages": [{"id": "wamid.short_url_test"}]}
        mock_post.return_value = mock_resp

        with patch("app.notifications.whatsapp.shorten_url", return_value="https://tinyurl.com/puma99"):
            # 1. Test template mode
            base_settings.WHATSAPP_MESSAGE_MODE = "template"
            template_notifier = WhatsAppNotifier(base_settings)
            res = template_notifier.send_restock_alert(event)
            assert res.status.value == "ACCEPTED"

            called_payload = mock_post.call_args[1]["json"]
            body_params = called_payload["template"]["components"][0]["parameters"]
            url_param = body_params[3]["text"]
            assert url_param == "https://tinyurl.com/puma99"

            # 2. Test text mode
            base_settings.WHATSAPP_MESSAGE_MODE = "text"
            text_notifier = WhatsAppNotifier(base_settings)
            res = text_notifier.send_restock_alert(event)
            assert res.status.value == "ACCEPTED"

            called_payload = mock_post.call_args[1]["json"]
            text_body = called_payload["text"]["body"]
            assert "https://tinyurl.com/puma99" in text_body


