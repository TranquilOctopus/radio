"""
MAX9744 stereo amp volume control over I2C.

The chip accepts a single-byte write: 0 mutes, 63 is full scale (+9.5 dB).
To prevent speaker damage, a `max_volume` hardware ceiling is applied —
user-facing percentages 0–100 are mapped onto 0..max_volume rather than
0..63, so no software path can run the amp hotter than the ceiling.
"""
import logging
import threading

logger = logging.getLogger(__name__)

_MAX_HARDWARE_VOLUME = 63


class Amp:
    def __init__(self, bus, address: int = 0x4B, max_volume: int = 40):
        self._bus = bus
        self._address = address
        self._max_volume = max(0, min(_MAX_HARDWARE_VOLUME, max_volume))
        self._user_volume = 0
        self._hw_volume = 0
        self._lock = threading.Lock()
        # Boot the amp at zero so we don't pop into whatever it powered up at.
        try:
            self._write(0)
            logger.info(
                "MAX9744 ready at 0x%02X (gain ceiling %d/%d)",
                address, self._max_volume, _MAX_HARDWARE_VOLUME,
            )
        except OSError as exc:
            logger.warning("MAX9744 init write failed: %s", exc)

    def set_volume(self, percent: int) -> None:
        """Set user-facing volume 0–100. Mapped to 0..max_volume on the chip."""
        percent = max(0, min(100, percent))
        self._user_volume = percent
        target = percent * self._max_volume // 100
        self._hw_volume = target
        try:
            self._write(target)
        except OSError as exc:
            logger.warning("MAX9744 write failed: %s", exc)

    def mute(self) -> None:
        try:
            self._write(0)
        except OSError as exc:
            logger.warning("MAX9744 mute failed: %s", exc)

    @property
    def volume(self) -> int:
        return self._user_volume

    def _write(self, value: int) -> None:
        with self._lock:
            self._bus.write_byte(self._address, value & 0x3F)
