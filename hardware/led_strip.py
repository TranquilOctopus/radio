"""
SK6812 RGBW LED strip controller using rpi_ws281x in SPI mode.

SPI mode is required to avoid conflict with the I2S DAC on GPIO 18.
Connect the LED strip data line to GPIO 10 (SPI0 MOSI).
"""
import logging
import threading
import time

logger = logging.getLogger(__name__)

try:
    from rpi_ws281x import PixelStrip, Color
except ImportError:
    from hardware.mock import MockPixelStrip as PixelStrip, mock_color as Color  # type: ignore
    logger.warning("rpi_ws281x not available — using mock LED strip")

# SPI mode: use pin 10 (SPI MOSI). The rpi_ws281x library maps GPIO 10 to SPI.
_LED_PIN = 10
_LED_FREQ_HZ = 800_000
_LED_DMA = 5        # DMA channel — use 5 for SPI mode
_LED_INVERT = False
_LED_CHANNEL = 0

# Warm white in RGBW: R=255, G=200, B=100, W=255 gives a warm incandescent feel.
_WARM_R = 255
_WARM_G = 180
_WARM_B = 80
_WARM_W = 255

_PULSE_STEPS = 50
_PULSE_PERIOD = 2.0   # seconds for one full pulse cycle


class LEDStrip:
    def __init__(self, num_leds: int, brightness: int = 128):
        self._num_leds = num_leds
        self._brightness = brightness
        self._on = False
        self._strip = PixelStrip(
            num_leds, _LED_PIN, _LED_FREQ_HZ, _LED_DMA,
            _LED_INVERT, brightness, _LED_CHANNEL,
        )
        self._strip.begin()
        self._pulse_thread: threading.Thread | None = None
        self._pulse_running = False

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def turn_on(self, brightness: int | None = None) -> None:
        self._stop_pulse()
        if brightness is not None:
            self._brightness = brightness
        self._strip.setBrightness(self._brightness)
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
            self._strip.setBrightness(self._brightness)
            self._strip.show()

    def start_pulse(self) -> None:
        """Gentle breathing animation used during alarm."""
        self._stop_pulse()
        self._pulse_running = True
        self._pulse_thread = threading.Thread(
            target=self._pulse_loop, daemon=True, name="led-pulse"
        )
        self._pulse_thread.start()

    def stop_pulse(self) -> None:
        # turn_on() handles the pulse thread teardown
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

    def _fill_warm(self) -> None:
        self._fill(_WARM_R, _WARM_G, _WARM_B, _WARM_W)

    def _fill(self, r: int, g: int, b: int, w: int) -> None:
        color = Color(r, g, b, w)
        for i in range(self._num_leds):
            self._strip.setPixelColor(i, color)
        self._strip.show()

    def _stop_pulse(self) -> None:
        if self._pulse_running:
            self._pulse_running = False
            if self._pulse_thread:
                self._pulse_thread.join(timeout=_PULSE_PERIOD + 0.5)
            self._pulse_thread = None

    def _pulse_loop(self) -> None:
        step_time = _PULSE_PERIOD / (_PULSE_STEPS * 2)
        while self._pulse_running:
            # Fade up
            for step in range(_PULSE_STEPS):
                if not self._pulse_running:
                    return
                brightness = int((step / _PULSE_STEPS) * self._brightness)
                self._strip.setBrightness(brightness)
                self._fill_warm()
                time.sleep(step_time)
            # Fade down
            for step in range(_PULSE_STEPS, 0, -1):
                if not self._pulse_running:
                    return
                brightness = int((step / _PULSE_STEPS) * self._brightness)
                self._strip.setBrightness(brightness)
                self._fill_warm()
                time.sleep(step_time)
