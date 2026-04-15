"""
Actions performed when the alarm fires, snoozes, or is dismissed.

All state (player, display, LED, scheduler, config) is injected at
construction time so this module has no global imports.
"""
import logging
import threading

logger = logging.getLogger(__name__)

_FADE_IN_DURATION = 30.0    # seconds to fade radio from silence to full volume


class AlarmController:
    def __init__(self, player, display, led, scheduler, config: dict):
        self._player = player
        self._display = display
        self._led = led
        self._scheduler = scheduler
        self._config = config
        self._alarm_active = False
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Called by AlarmScheduler
    # ------------------------------------------------------------------

    def on_alarm(self) -> None:
        with self._lock:
            if self._alarm_active:
                return
            self._alarm_active = True

        logger.info("Alarm fired")
        station_index = self._config["radio"]["default_station"]
        target_volume = self._config["radio"]["volume"]

        self._player.set_volume(0)
        self._player.play_station(station_index)
        self._player.fade_in(target_volume, _FADE_IN_DURATION)
        self._led.start_pulse()

    def on_snooze_end(self) -> None:
        """Called when a snooze period expires — re-fire the alarm."""
        self._scheduler.cancel_snooze()
        self.on_alarm()

    # ------------------------------------------------------------------
    # Called by buttons / web API
    # ------------------------------------------------------------------

    def snooze(self) -> None:
        if not self._alarm_active:
            return
        duration = self._config["snooze"]["duration_minutes"]
        logger.info("Snooze pressed — pausing for %d min", duration)
        self._player.stop()
        self._led.turn_off()
        with self._lock:
            self._alarm_active = False
        self._scheduler.snooze(duration)

    def dismiss(self) -> None:
        logger.info("Alarm dismissed")
        self._player.stop()
        self._led.turn_off()
        self._scheduler.cancel_snooze()
        with self._lock:
            self._alarm_active = False

    @property
    def is_active(self) -> bool:
        return self._alarm_active
