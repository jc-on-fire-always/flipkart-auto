"""Lightweight HTTP health status server for Docker and uptime monitoring."""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Optional

from app.database import DatabaseManager

logger = logging.getLogger(__name__)


class HealthCheckHandler(BaseHTTPRequestHandler):
    """HTTP handler returning sanitized system health state in JSON format."""

    db_manager: DatabaseManager
    start_time: float

    def log_message(self, format: str, *args) -> None:
        """Suppress standard HTTP server access logging to avoid noise."""
        return

    def do_GET(self) -> None:
        if self.path in ("/health", "/", "/healthz"):
            snapshot = self.db_manager.get_health_snapshot()
            uptime_seconds = int(time.time() - self.start_time)

            consecutive_errors = snapshot.get("consecutive_errors", 0)
            if consecutive_errors == 0:
                health_status = "healthy"
                http_code = 200
            elif consecutive_errors < 5:
                health_status = "degraded"
                http_code = 200
            else:
                health_status = "unhealthy"
                http_code = 503

            payload = {
                "status": health_status,
                "service_running": True,
                "uptime_seconds": uptime_seconds,
                "last_successful_stock_check": snapshot.get("last_successful_check_utc"),
                "last_observed_availability": snapshot.get("last_observed_availability"),
                "last_successful_notification": snapshot.get("last_successful_notification_utc"),
                "consecutive_errors": consecutive_errors,
                "last_error_summary": snapshot.get("last_error_summary"),
                "active_provider": snapshot.get("active_provider"),
                "checked_at_utc": datetime.now(timezone.utc).isoformat(),
            }

            body = json.dumps(payload, indent=2).encode("utf-8")
            self.send_response(http_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()


class HealthServer:
    """Manages the background HTTP health server thread."""

    def __init__(self, host: str, port: int, db_manager: DatabaseManager):
        self._host = host
        self._port = port
        self._db_manager = db_manager
        self._server: Optional[HTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self._start_time = time.time()

    def start(self) -> None:
        """Start the HTTP server on a daemon thread."""
        handler_class = HealthCheckHandler
        handler_class.db_manager = self._db_manager
        handler_class.start_time = self._start_time

        try:
            self._server = HTTPServer((self._host, self._port), handler_class)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
            self._thread.start()
            logger.info("Health server listening on http://%s:%s/health", self._host, self._port)
        except Exception as exc:
            logger.error("Failed to start health server on port %s: %s", self._port, exc)

    def stop(self) -> None:
        """Shut down the health server."""
        if self._server:
            logger.info("Shutting down health server...")
            self._server.shutdown()
            self._server.server_close()
            self._server = None
