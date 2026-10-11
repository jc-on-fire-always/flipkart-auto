"""Tests for lightweight health check HTTP server."""

import json
import socket
import urllib.request
from app.health import HealthServer


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("", 0))
        return s.getsockname()[1]


def test_health_server_endpoint(db_manager):
    """Verify health server serves JSON health status and closes cleanly."""
    port = get_free_port()
    server = HealthServer(host="127.0.0.1", port=port, db_manager=db_manager)
    server.start()

    try:
        url = f"http://127.0.0.1:{port}/health"
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=3) as response:
            assert response.status == 200
            data = json.loads(response.read().decode("utf-8"))

            assert data["status"] in ("healthy", "degraded")
            assert data["service_running"] is True
            assert "uptime_seconds" in data
            # Check secrets are absent
            assert "password" not in data
            assert "access_token" not in data

    finally:
        server.stop()
