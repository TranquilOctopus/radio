"""
AirPlay session monitor.

shairport-sync is configured to write a status file when a session starts or
ends (via sessioncontrol scripts).  This module watches that file and ducks
the MPD volume when AirPlay is active, restoring it when the session ends.

shairport-sync config required (add to /etc/shairport-sync.conf):

    sessioncontrol = {
        run_this_before_play_begins = "/usr/local/bin/airplay-start";
        run_this_after_play_ends = "/usr/local/bin/airplay-stop";
    };

Those two scripts simply write/delete /run/airplay-active.
See systemd/README for setup instructions.
"""
import logging
import os
import threading
import time

logger = logging.getLogger(__name__)

_STATUS_FILE = "/run/airplay-active"
_POLL_INTERVAL = 1.0    # seconds
_DUCK_VOLUME = 0        # MPD volume while AirPlay is active


class AirPlayMonitor:
    def __init__(self, player):
        self._player = player
        self._active = False
        self._pre_duck_volume: int = 70
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(
            target=self._monitor_loop, daemon=True, name="airplay-monitor"
        )
        self._thread.start()
        logger.info("AirPlay monitor started (watching %s)", _STATUS_FILE)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    @property
    def is_active(self) -> bool:
        return self._active

    def _monitor_loop(self) -> None:
        while self._running:
            active_now = os.path.exists(_STATUS_FILE)

            if active_now and not self._active:
                self._on_airplay_start()
            elif not active_now and self._active:
                self._on_airplay_end()

            self._active = active_now
            time.sleep(_POLL_INTERVAL)

    def _on_airplay_start(self) -> None:
        logger.info("AirPlay session started — ducking MPD")
        status = self._player.status()
        self._pre_duck_volume = status.get("volume", 70)
        self._player.set_volume(_DUCK_VOLUME)

    def _on_airplay_end(self) -> None:
        logger.info("AirPlay session ended — restoring MPD volume to %d", self._pre_duck_volume)
        self._player.set_volume(self._pre_duck_volume)
