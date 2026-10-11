"""Polling scheduler with backoff, jitter, and graceful stop signaling."""

from __future__ import annotations

import logging
import random
import threading

logger = logging.getLogger(__name__)


class PollingScheduler:
    """Controls the periodic execution of the monitoring check with jitter and interruption support."""

    def __init__(self, interval_seconds: int, stop_event: threading.Event):
        self._interval_seconds = interval_seconds
        self._stop_event = stop_event

    def wait(self, custom_delay: float | None = None) -> bool:
        """
        Sleep for the configured interval with subtle random jitter (+-5%).

        Returns True if interrupted by stop_event, False if interval elapsed normally.
        """
        base_delay = custom_delay if custom_delay is not None else float(self._interval_seconds)
        jitter = random.uniform(-0.05, 0.05) * base_delay
        sleep_time = max(1.0, base_delay + jitter)

        # wait on stop_event with timeout allows immediate response to shutdown signals
        interrupted = self._stop_event.wait(timeout=sleep_time)
        return interrupted
