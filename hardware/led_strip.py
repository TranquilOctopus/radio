"""
SK6812 RGBW LED strip controller using adafruit-circuitpython-neopixel-spi.

The strip is driven via SPI (GPIO 10 / SPI0 MOSI) so it doesn't conflict with
the I2S DAC on GPIO 18. The neopixel_spi library shifts WS281x-compatible
timing out of the SPI peripheral, so it works on any Pi without
hardware-revision detection (unlike rpi_ws281x).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

try:
    import board
    import neopixel_spi
    _HAS_NEOPIXEL = True
except (ImportError, NotImplementedError, Exception) as _exc:  # noqa: BLE001
    # ImportError on non-Pi machines; NotImplementedError / generic Exception
    # if Blinka can't detect the platform at import time.
    _HAS_NEOPIXEL = False
    _import_error = _exc
    logger.warning("neopixel_spi not available — LED strip will use mock (%s)", _exc)


# Warm white in RGBW — incandescent-feeling
_WARM_R = 255
_WARM_G = 180
_WARM_B = 80
_WARM_W = 255

_PULSE_STEPS = 50
_PULSE_PERIOD = 2.0   # seconds per full pulse cycle


class LEDStrip:
    def __init__(self, num_leds: int, brightness: int = 128):
        self._num_leds = num_leds
        self._brightness = brightness  # 0–255, mapped to 0.0–1.0 for neopixel_spi
        self._on = False
        self._mock = False
        self._pixels = None
        self._init_pixels()
        self._pulse_thread: threading.Thread | None = None
        self._pulse_running = False

    def _init_pixels(self) -> None:
        if not _HAS_NEOPIXEL:
            self._mock = True
            return
        try:
            spi = board.SPI()
            self._pixels = neopixel_spi.NeoPixel_SPI(
                spi,
                self._num_leds,
                brightness=self._brightness / 255.0,
                pixel_order=neopixel_spi.GRBW,
                auto_write=False,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "neopixel_spi init failed (%s) — falling back to mock LED strip",
                exc,
            )
            self._mock = True
            self._pixels = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def turn_on(self, brightness: int | None = None) -> None:
        self._stop_pulse()
        if brightness is not None:
            self._brightness = max(0, min(255, brightness))
        self._apply_brightness()
        self._fill_warm()
        self._on = True

    def turn_off(self) -> None:
        self._stop_pulse()
        self._fill(0, 0, 0, 0)
        self._on = False

    def toggle(self) -> None:
        if self._on:
            self.turn_off()
        else:
            self.turn_on()

    def set_brightness(self, brightness: int) -> None:
        self._brightness = max(0, min(255, brightness))
        if self._on:
            self._apply_brightness()
            self._show()

    def start_pulse(self) -> None:
        """Gentle breathing animation used during alarm."""
        self._stop_pulse()
        self._pulse_running = True
        self._pulse_thread = threading.Thread(
            target=self._pulse_loop, daemon=True, name="led-pulse"
        )
        self._pulse_thread.start()

    def stop_pulse(self) -> None:
        self.turn_on()

    @property
    def is_on(self) -> bool:
        return self._on

    @property
    def brightness(self) -> int:
        return self._brightness

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _apply_brightness(self) -> None:
        if self._mock or self._pixels is None:
            return
        self._pixels.brightness = self._brightness / 255.0

    def _fill_warm(self) -> None:
        self._fill(_WARM_R, _WARM_G, _WARM_B, _WARM_W)

    def _fill(self, r: int, g: int, b: int, w: int) -> None:
        if self._mock or self._pixels is None:
            return
        color = (r, g, b, w)
        for i in range(self._num_leds):
            self._pixels[i] = color
        self._show()

    def _show(self) -> None:
        if self._mock or self._pixels is None:
            return
        self._pixels.show()

    def _stop_pulse(self) -> None:
        if self._pulse_running:
            self._pulse_running = False
            if self._pulse_thread:
                self._pulse_thread.join(timeout=_PULSE_PERIOD + 0.5)
            self._pulse_thread = None

    def _pulse_loop(self) -> None:
        step_time = _PULSE_PERIOD / (_PULSE_STEPS * 2)
        while self._pulse_running:
            for step in range(_PULSE_STEPS):
                if not self._pulse_running:
                    return
                self._brightness_step(step / _PULSE_STEPS)
                time.sleep(step_time)
            for step in range(_PULSE_STEPS, 0, -1):
                if not self._pulse_running:
                    return
                self._brightness_step(step / _PULSE_STEPS)
                time.sleep(step_time)

    def _brightness_step(self, fraction: float) -> None:
        target = int(fraction * self._brightness)
        if self._mock or self._pixels is None:
            return
        self._pixels.brightness = target / 255.0
        self._fill_warm()
