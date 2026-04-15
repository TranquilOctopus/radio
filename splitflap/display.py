# Motor control logic ported from https://github.com/ManlyMorgan/Split-Flap-Display
import logging
import time
import threading

from .characters import STEPS_PER_ROTATION
from .module import SplitFlapModule

logger = logging.getLogger(__name__)

HALL_POLL_INTERVAL = 0.02   # 20 ms, matching original firmware
MOTOR_SETTLE_DELAY = 0.20   # 200 ms settle after start/stop
LOOP_YIELD = 0.0005         # 500 µs — yields CPU without materially affecting step timing


class SplitFlapDisplay:
    """
    Coordinates a set of SplitFlapModule instances, driving them concurrently
    to display a string or time value.
    """

    def __init__(self, modules: list[SplitFlapModule], speed_rpm: float = 10.0):
        self.modules = modules
        self.speed_rpm = speed_rpm
        self._lock = threading.Lock()

        steps_per_second = (speed_rpm / 60.0) * STEPS_PER_ROTATION
        self._time_per_step = 1.0 / steps_per_second  # seconds

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def init_all(self) -> None:
        """Initialise every module."""
        for module in self.modules:
            module.init()

    def home_all(self) -> None:
        """
        Home all modules: step forward until the hall sensor fires, then
        move to the blank character (position 0).
        """
        with self._lock:
            logger.info("Homing all modules...")
            for module in self.modules:
                module.start()

            time.sleep(MOTOR_SETTLE_DELAY)
            homed = [False] * len(self.modules)

            # Step forward up to one full rotation looking for the magnet
            for _ in range(STEPS_PER_ROTATION):
                for i, module in enumerate(self.modules):
                    if not homed[i]:
                        module.step()
                        if module.read_hall_sensor():
                            module.magnet_detected()
                            homed[i] = True

                if all(homed):
                    break

                time.sleep(self._time_per_step)

            for module in self.modules:
                module.stop()

            unhomed = [i for i, h in enumerate(homed) if not h]
            if unhomed:
                logger.warning("Modules at indices %s did not find home position", unhomed)

            # Move every module to the blank character
            targets = [0] * len(self.modules)
            self._move_to(targets)
            logger.info("Homing complete")

    def write_time(self, hh: int, mm: int) -> None:
        """Display the time as HH MM across 4 modules."""
        s = f"{hh:02d}{mm:02d}"
        self.write_string(s)

    def write_string(self, s: str) -> None:
        """
        Display a string across the modules.  Characters are left-aligned;
        excess modules are set to blank.
        """
        s = s.upper()
        padded = s.ljust(len(self.modules))[:len(self.modules)]
        targets = [m.get_char_position(c) for m, c in zip(self.modules, padded)]
        with self._lock:
            self._move_to(targets)

    # ------------------------------------------------------------------
    # Internal movement
    # ------------------------------------------------------------------

    def _move_to(self, target_positions: list[int]) -> None:
        """
        Step all modules concurrently until each reaches its target position.
        Uses perf_counter for microsecond-level step timing, matching the
        original firmware's micros()-based loop.
        """
        n = len(self.modules)
        needs_stepping = [
            self.modules[i].position != target_positions[i]
            for i in range(n)
        ]

        if not any(needs_stepping):
            return

        last_step_times = [time.perf_counter()] * n
        last_sensor_check = time.perf_counter()
        reset_latches = [True] * n

        for module in self.modules:
            module.start()
        time.sleep(MOTOR_SETTLE_DELAY)

        while any(needs_stepping):
            now = time.perf_counter()

            for i, module in enumerate(self.modules):
                if needs_stepping[i] and (now - last_step_times[i]) >= self._time_per_step:
                    module.step()
                    last_step_times[i] = now
                    if module.position == target_positions[i]:
                        needs_stepping[i] = False

            if now - last_sensor_check >= HALL_POLL_INTERVAL:
                for i, module in enumerate(self.modules):
                    if needs_stepping[i]:
                        if module.read_hall_sensor():
                            if not reset_latches[i]:
                                module.magnet_detected()
                                reset_latches[i] = True
                        else:
                            reset_latches[i] = False
                last_sensor_check = now

            time.sleep(LOOP_YIELD)

        time.sleep(MOTOR_SETTLE_DELAY)
        for module in self.modules:
            module.stop()
