"""
Slide potentiometer reader via ADS1115 I2C ADC.

Polls AIN0 in continuous mode at `poll_hz` Hz, applies a small rolling average
to smooth jitter, and exposes the current volume as an integer 0–100.
"""
import logging
import struct
import threading
import time
from collections import deque
from typing import Callable

logger = logging.getLogger(__name__)

# ADS1115 register addresses
_REG_CONVERSION = 0x00
_REG_CONFIG = 0x01

# Config word: OS=1 (start), MUX=100 (AIN0 vs GND), PGA=001 (±4.096 V),
# MODE=0 (continuous), DR=100 (128 SPS), COMP_QUE=11 (disabled)
# High byte: 1100_0010 = 0xC2   Low byte: 1000_0011 = 0x83
_CONFIG_HIGH = 0xC2
_CONFIG_LOW = 0x83

_SMOOTH_WINDOW = 5      # Rolling average over this many samples
_CHANGE_THRESHOLD = 2   # Minimum volume change (0-100) to fire the callback


class Potentiometer:
    def __init__(
        self,
        bus,
        address: int = 0x48,
        poll_hz: float = 5.0,
        on_change: Callable[[int], None] | None = None,
    ):
        self._bus = bus
        self._address = address
        self._poll_interval = 1.0 / poll_hz
        self._on_change = on_change
        self._volume = 0
        self._samples: deque[int] = deque(maxlen=_SMOOTH_WINDOW)
        self._running = False
        self._thread: threading.Thread | None = None

        # Start continuous conversion
        self._bus.write_i2c_block_data(
            self._address, _REG_CONFIG, [_CONFIG_HIGH, _CONFIG_LOW]
        )

    @property
    def volume(self) -> int:
        return self._volume

    def start(self) -> None:
        self._running = True
        self._thread = threading.Thread(target=self._poll_loop, daemon=True, name="potentiometer")
        self._thread.start()
        logger.info("Potentiometer polling started at ADS1115 0x%02X", self._address)

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)

    def _read_raw(self) -> int:
        data = self._bus.read_i2c_block_data(self._address, _REG_CONVERSION, 2)
        raw = struct.unpack(">h", bytes(data))[0]   # signed 16-bit big-endian
        return max(0, raw)                           # clamp negatives (wiring error) to 0

    def _poll_loop(self) -> None:
        while self._running:
            try:
                raw = self._read_raw()
                self._samples.append(raw)
                smoothed = int(sum(self._samples) / len(self._samples))
                new_volume = min(100, int((smoothed / 32767) * 100))

                if abs(new_volume - self._volume) >= _CHANGE_THRESHOLD:
                    self._volume = new_volume
                    logger.debug("Volume changed to %d", new_volume)
                    if self._on_change:
                        try:
                            self._on_change(new_volume)
                        except Exception:
                            logger.exception("Error in volume change callback")
            except Exception:
                logger.exception("Potentiometer read error")

            time.sleep(self._poll_interval)
