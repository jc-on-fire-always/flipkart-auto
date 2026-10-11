"""End-to-end integration test with a local mock HTTP server and notification harness."""

import http.server
import socket
import threading
from app.config import Settings
from app.models import AvailabilityState
from app.monitor import FlipkartMonitor
from app.providers.live_flipkart_web import FlipkartWebStockProvider


class LocalFlipkartMockHandler(http.server.BaseHTTPRequestHandler):
    """Local HTTP mock simulating Flipkart product responses."""

    in_stock_flag = False

    def log_message(self, format: str, *args) -> None:
        return

    def do_GET(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()

        if self.in_stock_flag:
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Integration Product | Flipkart</title></head>
            <body>
              <script type="application/ld+json">
              {
                "@context": "https://schema.org/",
                "@type": "Product",
                "name": "Integration Test Product",
                "offers": {
                  "@type": "Offer",
                  "price": "59999",
                  "availability": "https://schema.org/InStock"
                }
              }
              </script>
              <div>BUY NOW</div>
            </body>
            </html>
            """
        else:
            html = """
            <!DOCTYPE html>
            <html>
            <head><title>Integration Product | Flipkart</title></head>
            <body>
              <script type="application/ld+json">
              {
                "@context": "https://schema.org/",
                "@type": "Product",
                "name": "Integration Test Product",
                "offers": {
                  "@type": "Offer",
                  "price": "59999",
                  "availability": "https://schema.org/OutOfStock"
                }
              }
              </script>
              <div>Currently Out of Stock</div>
            </body>
            </html>
            """
        self.wfile.write(html.encode("utf-8"))


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def test_end_to_end_restock_integration(
    temp_db_path, test_whatsapp_notifier, test_email_notifier
):
    """End-to-end test simulating actual HTTP requests against local mock server."""
    port = get_free_port()
    handler_class = LocalFlipkartMockHandler
    handler_class.in_stock_flag = False

    server = http.server.HTTPServer(("127.0.0.1", port), handler_class)
    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()

    product_url = f"http://127.0.0.1:{port}/p/itm999888777"

    settings = Settings(
        _env_file=None,
        PRODUCT_NAME="Integration Test Product",
        PRODUCT_URL=product_url,
        POLL_INTERVAL_SECONDS=5,
        NOTIFICATION_CHANNELS="both",
        DATABASE_URL=temp_db_path,
        STOCK_PROVIDER_TYPE="flipkart_web",
        ALERT_COOLDOWN_SECONDS=3600,
        INITIAL_ALERT_ON_STARTUP=False,
        VERIFICATION_COUNT=1,
        WHATSAPP_PHONE_NUMBER_ID="123",
        WHATSAPP_ACCESS_TOKEN="token",
        WHATSAPP_TO="+919876543210",
        SMTP_HOST="smtp.test.com",
        SMTP_FROM="a@b.com",
        EMAIL_TO="c@d.com",
    )

    try:
        # Use FlipkartWebStockProvider inspecting local mock HTTP server
        web_provider = FlipkartWebStockProvider(timeout_seconds=5)
        monitor = FlipkartMonitor(
            settings=settings,
            custom_provider=web_provider,
            custom_whatsapp=test_whatsapp_notifier,
            custom_email=test_email_notifier,
        )

        # 1. First check: Server returns OUT_OF_STOCK
        obs1, event1, notifs1 = monitor.check_once()
        assert obs1 is not None
        assert obs1.availability == AvailabilityState.OUT_OF_STOCK
        assert event1 is None
        assert len(notifs1) == 0

        # 2. Server transitions to IN_STOCK
        handler_class.in_stock_flag = True

        # 3. Second check: Server returns IN_STOCK, triggers restock event & notifications
        obs2, event2, notifs2 = monitor.check_once()
        assert obs2 is not None
        assert obs2.availability == AvailabilityState.IN_STOCK
        assert event2 is not None
        assert event2.price == 59999.0
        assert len(notifs2) == 2  # WhatsApp and Email delivered
        assert test_whatsapp_notifier.call_count == 1
        assert test_email_notifier.call_count == 1

        # 4. Third check: Still in stock, duplicate alerts suppressed
        obs3, event3, notifs3 = monitor.check_once()
        assert obs3.availability == AvailabilityState.IN_STOCK
        assert event3 is None
        assert len(notifs3) == 0

    finally:
        server.shutdown()
        server.server_close()
