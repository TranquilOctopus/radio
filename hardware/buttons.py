"""
GPIO button handler.

Snooze button:
  - Single press  → snooze active alarm
  - Double press  → dismiss active alarm / stop radio

LED button:
  - Single press  → toggle LED strip
"""
import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

try:
    import RPi.GPIO as GPIO
except ImportError:
    from hardware.mock import MockGPIO as GPIO  # type: ignore
    logger.warning("RPi.GPIO not available — using mock GPIO")


class ButtonHandler:
    def __init__(
        self,
        snooze_pin: int,
        led_pin: int,
        double_press_ms: int,
        on_snooze: Callable,
        on_dismiss: Callable,
        on_led_toggle: Callable,
    ):
        self.snooze_pin = snooze_pin
        self.led_pin = led_pin
        self.double_press_window = double_press_ms / 1000.0

        self._on_snooze = on_snooze
        self._on_dismiss = on_dismiss
        self._on_led_toggle = on_led_toggle

        self._snooze_last_press = 0.0
        self._snooze_timer: threading.Timer | None = None

        GPIO.setmode(GPIO.BCM)
        GPIO.setup(snooze_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.setup(led_pin, GPIO.IN, pull_up_down=GPIO.PUD_UP)

        GPIO.add_event_detect(
            snooze_pin, GPIO.FALLING,
            callback=self._snooze_pressed,
            bouncetime=50,
        )
        GPIO.add_event_detect(
            led_pin, GPIO.FALLING,
            callback=self._led_pressed,
            bouncetime=50,
        )
        logger.info("Button handler ready (snooze=GPIO%d, led=GPIO%d)", snooze_pin, led_pin)

    def _snooze_pressed(self, channel: int) -> None:
        now = time.monotonic()

        if self._snooze_timer is not None:
            self._snooze_timer.cancel()
            self._snooze_timer = None

        if now - self._snooze_last_press < self.double_press_window:
            # Double press → dismiss
            self._snooze_last_press = 0.0
            logger.debug("Snooze button: double press → dismiss")
            threading.Thread(target=self._on_dismiss, daemon=True).start()
        else:
            # Might be a single press — wait to see if a second follows
            self._snooze_last_press = now
            self._snooze_timer = threading.Timer(
                self.double_press_window, self._fire_single_snooze
            )
            self._snooze_timer.start()

    def _fire_single_snooze(self) -> None:
        logger.debug("Snooze button: single press → snooze")
        self._snooze_timer = None
        threading.Thread(target=self._on_snooze, daemon=True).start()

    def _led_pressed(self, channel: int) -> None:
        logger.debug("LED button pressed → toggle")
        threading.Thread(target=self._on_led_toggle, daemon=True).start()

    def cleanup(self) -> None:
        if self._snooze_timer:
            self._snooze_timer.cancel()
        GPIO.cleanup()
