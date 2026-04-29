"""
GPIO button handler.

Snooze button:
  - Single press  → snooze active alarm
  - Double press  → dismiss active alarm / stop radio

LED button:
  - Single press  → toggle LED strip

Uses gpiozero (which sits on top of lgpio on Bookworm/Trixie). The older
RPi.GPIO library is unmaintained and its add_event_detect is broken on
modern Pi kernels.
"""
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

try:
    from gpiozero import Button
    _HAS_GPIO = True
except ImportError as _exc:
    _HAS_GPIO = False
    logger.warning("gpiozero not available — buttons disabled (%s)", _exc)


class ButtonHandler:
    def __init__(
        self,
        snooze_pin: int,
        led_pin: int,
        double_press_ms: int,
        led_hold_seconds: float,
        on_snooze: Callable,
        on_dismiss: Callable,
        on_led_press: Callable,
        on_led_release: Callable,
    ):
        self.snooze_pin = snooze_pin
        self.led_pin = led_pin
        self.double_press_window = double_press_ms / 1000.0
        self._led_hold_seconds = led_hold_seconds

        self._on_snooze = on_snooze
        self._on_dismiss = on_dismiss
        self._on_led_press = on_led_press
        self._on_led_release = on_led_release

        self._snooze_last_press = 0.0
        self._snooze_timer: threading.Timer | None = None
        self._led_off_timer: threading.Timer | None = None
        self._snooze_button: Button | None = None
        self._led_button: Button | None = None

        if not _HAS_GPIO:
            return

        try:
            self._snooze_button = Button(snooze_pin, pull_up=True, bounce_time=0.05)
            self._led_button = Button(led_pin, pull_up=True, bounce_time=0.05)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Button init failed (%s) — buttons disabled", exc)
            self._snooze_button = None
            self._led_button = None
            return

        self._snooze_button.when_pressed = self._snooze_pressed
        self._led_button.when_pressed = self._led_pressed
        self._led_button.when_released = self._led_released
        logger.info("Button handler ready (snooze=GPIO%d, led=GPIO%d)", snooze_pin, led_pin)

    def _snooze_pressed(self) -> None:
        now = time.monotonic()

        if self._snooze_timer is not None:
            self._snooze_timer.cancel()
            self._snooze_timer = None

        if now - self._snooze_last_press < self.double_press_window:
            self._snooze_last_press = 0.0
            logger.debug("Snooze button: double press → dismiss")
            threading.Thread(target=self._on_dismiss, daemon=True).start()
        else:
            self._snooze_last_press = now
            self._snooze_timer = threading.Timer(
                self.double_press_window, self._fire_single_snooze
            )
            self._snooze_timer.start()

    def _fire_single_snooze(self) -> None:
        logger.debug("Snooze button: single press → snooze")
        self._snooze_timer = None
        threading.Thread(target=self._on_snooze, daemon=True).start()

    def _led_pressed(self) -> None:
        # Cancel any pending off-timer from a previous release.
        if self._led_off_timer is not None:
            self._led_off_timer.cancel()
            self._led_off_timer = None
        logger.debug("LED button pressed → fade on")
        threading.Thread(target=self._on_led_press, daemon=True).start()

    def _led_released(self) -> None:
        logger.debug("LED button released → fade off in %.1fs", self._led_hold_seconds)
        if self._led_off_timer is not None:
            self._led_off_timer.cancel()
        self._led_off_timer = threading.Timer(self._led_hold_seconds, self._fire_led_off)
        self._led_off_timer.start()

    def _fire_led_off(self) -> None:
        self._led_off_timer = None
        logger.debug("LED hold expired → fade off")
        threading.Thread(target=self._on_led_release, daemon=True).start()

    def cleanup(self) -> None:
        if self._snooze_timer:
            self._snooze_timer.cancel()
        if self._led_off_timer:
            self._led_off_timer.cancel()
        if self._snooze_button is not None:
            self._snooze_button.close()
        if self._led_button is not None:
            self._led_button.close()
