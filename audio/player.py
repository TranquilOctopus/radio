"""
MPD client wrapper for internet radio playback.

MPD runs as a system service. This module connects to it via the local socket,
manages station playback, and exposes a simple volume/play/stop API.
"""
import logging
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

try:
    from mpd import MPDClient, ConnectionError as MPDConnectionError
except ImportError:
    # Stub for development on non-Pi machines
    class MPDClient:  # type: ignore
        def __init__(self): pass
        def connect(self, *a, **k): pass
        def disconnect(self): pass
        def clear(self): pass
        def add(self, url): pass
        def play(self): pass
        def stop(self): pass
        def pause(self, state): pass
        def setvol(self, vol): pass
        def status(self): return {"state": "stop", "volume": "70"}
        def ping(self): pass
    class MPDConnectionError(Exception): pass  # type: ignore
    logger.warning("python-mpd2 not available — using stub MPD client")


@dataclass
class Station:
    name: str
    url: str


class RadioPlayer:
    def __init__(
        self,
        host: str = "localhost",
        port: int = 6600,
        stations: list[dict] | None = None,
    ):
        self._host = host
        self._port = port
        self.stations: list[Station] = [
            Station(**s) for s in (stations or [])
        ]
        self._client = MPDClient()
        self._client.timeout = 5
        self._lock = threading.Lock()
        self._current_station_index: int = 0
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> None:
        try:
            self._client.connect(self._host, self._port)
            self._connected = True
            logger.info("Connected to MPD at %s:%d", self._host, self._port)
        except Exception:
            logger.exception("Failed to connect to MPD")
            self._connected = False

    def _ensure_connected(self) -> bool:
        if not self._connected:
            self.connect()
        else:
            try:
                self._client.ping()
            except Exception:
                self._connected = False
                self.connect()
        return self._connected

    # ------------------------------------------------------------------
    # Playback control
    # ------------------------------------------------------------------

    def add_station(self, name: str, url: str) -> None:
        self.stations.append(Station(name=name, url=url))

    def play_station(self, index: int | None = None) -> None:
        if index is not None:
            self._current_station_index = index
        if not self.stations:
            logger.warning("No stations configured")
            return
        station = self.stations[self._current_station_index]
        with self._lock:
            if not self._ensure_connected():
                return
            try:
                self._client.clear()
                self._client.add(station.url)
                self._client.play()
                logger.info("Playing station: %s", station.name)
            except Exception:
                logger.exception("Error starting playback")

    def stop(self) -> None:
        with self._lock:
            if not self._ensure_connected():
                return
            try:
                self._client.stop()
                logger.info("Playback stopped")
            except Exception:
                logger.exception("Error stopping playback")

    def set_volume(self, level: int) -> None:
        """Set volume 0–100."""
        level = max(0, min(100, level))
        with self._lock:
            if not self._ensure_connected():
                return
            try:
                self._client.setvol(level)
            except Exception:
                logger.exception("Error setting volume to %d", level)

    def fade_in(self, target: int, duration: float = 10.0) -> None:
        """Gradually increase volume from 0 to `target` over `duration` seconds."""
        def _fade():
            steps = 20
            delay = duration / steps
            for step in range(steps + 1):
                vol = int((step / steps) * target)
                self.set_volume(vol)
                time.sleep(delay)
        threading.Thread(target=_fade, daemon=True, name="radio-fade-in").start()

    # ------------------------------------------------------------------
    # Status
    # ------------------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            if not self._ensure_connected():
                return {"state": "disconnected", "volume": 0}
            try:
                s = self._client.status()
                return {
                    "state": s.get("state", "stop"),
                    "volume": int(s.get("volume", 0)),
                    "station_index": self._current_station_index,
                    "station_name": (
                        self.stations[self._current_station_index].name
                        if self.stations else None
                    ),
                }
            except Exception:
                logger.exception("Error reading MPD status")
                return {"state": "error", "volume": 0}

    @property
    def is_playing(self) -> bool:
        return self.status().get("state") == "play"
