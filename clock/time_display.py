"""
Background thread that keeps the splitflap display in sync with the wall clock.

Updates once per minute, aligned to the minute boundary so the flap turns
exactly when the minute changes.  Any NTP correction is picked up automatically
on the next tick.
"""
import logging
import threading
import time
from datetime import datetime

logger = logging.getLogger(__name__)


class TimeDisplay:
    def __init__(self, display):
        self._display = display
        self._running = False
        self._thread: threading.Thread | None = None
        self._last_hhmm: tuple[int, int] | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._loop, daemon=True, name="time-display"
        )
        self._thread.start()
        logger.info("Time display thread started")

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def force_update(self) -> None:
        """Immediately push the current time to the display (e.g. after homing)."""
        self._last_hhmm = None
        self._update()

    def _loop(self) -> None:
        while self._running:
            self._update()
            self._sleep_until_next_minute()

    def _update(self) -> None:
        now = datetime.now()
        hh, mm = now.hour, now.minute
        if (hh, mm) == self._last_hhmm:
            return
        self._last_hhmm = (hh, mm)
        logger.debug("Updating display to %02d:%02d", hh, mm)
        try:
            self._display.write_time(hh, mm)
        except Exception:
            logger.exception("Failed to update splitflap display")

    @staticmethod
    def _sleep_until_next_minute() -> None:
        """Sleep until the start of the next wall-clock minute."""
        now = time.time()
        next_minute = (now // 60 + 1) * 60
        sleep_duration = next_minute - now
        time.sleep(max(0.0, sleep_duration))
