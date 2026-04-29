"""
WS2812 / SK6812 LED strip controller using adafruit-circuitpython-neopixel-spi.

The strip is driven via SPI (GPIO 10 / SPI0 MOSI) so it doesn't conflict with
the I2S DAC on GPIO 18. The neopixel_spi library shifts WS281x-compatible
timing out of the SPI peripheral, so it works on any Pi without
hardware-revision detection (unlike rpi_ws281x).

Strip type is selected via config.yaml:led.strip_type — "WS2812" (RGB) or
"SK6812" (RGBW). The W channel is dropped automatically for RGB strips.
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
    def __init__(self, num_leds: int, brightness: int = 128, strip_type: str = "SK6812"):
        self._num_leds = num_leds
        self._brightness = brightness  # target — 0–255, mapped to 0.0–1.0 for neopixel_spi
        self._displayed_brightness = 0  # what the strip is currently showing
        self._on = False
        self._mock = False
        self._pixels = None
        self._strip_type = strip_type.upper()
        self._has_white = self._strip_type == "SK6812"
        self._init_pixels()
        self._pulse_thread: threading.Thread | None = None
        self._pulse_running = False
        self._fade_thread: threading.Thread | None = None
        self._fade_stop = threading.Event()

    def _init_pixels(self) -> None:
        if not _HAS_NEOPIXEL:
            self._mock = True
            return
        try:
            spi = board.SPI()
            pixel_order = neopixel_spi.GRBW if self._has_white else neopixel_spi.GRB
            self._pixels = neopixel_spi.NeoPixel_SPI(
                spi,
                self._num_leds,
                brightness=0.0,
                pixel_order=pixel_order,
                auto_write=False,
            )
            logger.info("LED strip ready: %d × %s", self._num_leds, self._strip_type)
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
        """Instant on — used by web API and alarm."""
        self._stop_pulse()
        self._stop_fade()
        if brightness is not None:
            self._brightness = max(0, min(255, brightness))
        self._fill_warm_no_show()
        self._set_displayed(self._brightness)
        self._on = True

    def turn_off(self) -> None:
        """Instant off — used by web API, alarm dismiss, and shutdown."""
        self._stop_pulse()
        self._stop_fade()
        self._set_displayed(0)
        self._on = False

    def fade_on(self, duration_ms: int = 500) -> None:
        """Smoothly ramp from current brightness up to the configured target."""
        self._stop_pulse()
        self._start_fade(self._displayed_brightness, self._brightness, duration_ms)
        self._on = True

    def fade_off(self, duration_ms: int = 500) -> None:
        """Smoothly ramp from current brightness down to off."""
        self._stop_pulse()
        self._start_fade(self._displayed_brightness, 0, duration_ms)
        self._on = False

    def toggle(self) -> None:
        if self._on:
            self.turn_off()
        else:
            self.turn_on()

    def set_brightness(self, brightness: int) -> None:
        self._brightness = max(0, min(255, brightness))
        if self._on:
            self._set_displayed(self._brightness)

    def start_pulse(self) -> None:
        """Gentle breathing animation used during alarm."""
        self._stop_pulse()
        self._stop_fade()
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

    def _set_displayed(self, value: int) -> None:
        """Set the currently shown brightness (0–255), pushing colours if needed."""
        self._displayed_brightness = max(0, min(255, value))
        if self._mock or self._pixels is None:
            return
        if self._displayed_brightness > 0:
            self._fill_warm_no_show()
        self._pixels.brightness = self._displayed_brightness / 255.0
        self._pixels.show()

    def _fill_warm_no_show(self) -> None:
        if self._mock or self._pixels is None:
            return
        color = (_WARM_R, _WARM_G, _WARM_B, _WARM_W) if self._has_white else (_WARM_R, _WARM_G, _WARM_B)
        for i in range(self._num_leds):
            self._pixels[i] = color

    def _start_fade(self, start: int, end: int, duration_ms: int) -> None:
        self._stop_fade()
        self._fade_stop = threading.Event()
        self._fade_thread = threading.Thread(
            target=self._fade_loop,
            args=(start, end, duration_ms),
            daemon=True,
            name="led-fade",
        )
        self._fade_thread.start()

    def _stop_fade(self) -> None:
        self._fade_stop.set()
        if self._fade_thread is not None:
            self._fade_thread.join(timeout=1.0)
        self._fade_thread = None

    def _fade_loop(self, start: int, end: int, duration_ms: int) -> None:
        if self._mock or self._pixels is None:
            self._displayed_brightness = end
            return
        # Pre-fill colours so the brightness ramp is smooth from step 1.
        self._fill_warm_no_show()
        steps = max(1, duration_ms // 20)   # ~50 fps
        step_dt = (duration_ms / 1000.0) / steps
        for i in range(1, steps + 1):
            if self._fade_stop.is_set():
                return
            cur = start + (end - start) * i // steps
            self._displayed_brightness = cur
            self._pixels.brightness = cur / 255.0
            self._pixels.show()
            time.sleep(step_dt)

    def _stop_pulse(self) -> None:
        if self._pulse_running:
            self._pulse_running = False
            if self._pulse_thread:
                self._pulse_thread.join(timeout=_PULSE_PERIOD + 0.5)
            self._pulse_thread = None

    def _pulse_loop(self) -> None:
        step_time = _PULSE_PERIOD / (_PULSE_STEPS * 2)
        self._fill_warm_no_show()
        while self._pulse_running:
            for step in range(_PULSE_STEPS):
                if not self._pulse_running:
                    return
                self._set_displayed(int((step / _PULSE_STEPS) * self._brightness))
                time.sleep(step_time)
            for step in range(_PULSE_STEPS, 0, -1):
                if not self._pulse_running:
                    return
                self._set_displayed(int((step / _PULSE_STEPS) * self._brightness))
                time.sleep(step_time)
