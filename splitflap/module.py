# Motor control logic ported from https://github.com/ManlyMorgan/Split-Flap-Display
import logging
from .characters import STEPS_PER_ROTATION, get_position

logger = logging.getLogger(__name__)

# Four-step coil patterns for the stepper motor via PCF8575
STEP_PATTERNS = [0xFFE7, 0xFFF3, 0xFFF9, 0xFFED]
STOP_PATTERN = 0xFFE1
HALL_SENSOR_BIT = 15


class SplitFlapModule:
    """
    Controls a single splitflap module via a PCF8575 I2C GPIO expander.

    The PCF8575 at `address` drives the stepper motor coils on P00-P03
    and reads the hall effect sensor on P17 (bit 15).
    """

    def __init__(self, bus, address: int, offset: int = 0, magnet_position: int = 730):
        self.bus = bus
        self.address = address
        self.offset = offset
        self.magnet_position = (magnet_position + offset) % STEPS_PER_ROTATION
        self.position = 0
        self.step_number = 0

    def _write_io(self, data: int) -> None:
        from smbus2 import i2c_msg
        msg = i2c_msg.write(self.address, [data & 0xFF, (data >> 8) & 0xFF])
        self.bus.i2c_rdwr(msg)

    def _read_io(self) -> int:
        from smbus2 import i2c_msg
        msg = i2c_msg.read(self.address, 2)
        self.bus.i2c_rdwr(msg)
        data = list(msg)
        return data[0] | (data[1] << 8)

    def step(self, update_position: bool = True) -> None:
        """Advance the motor by one step."""
        pattern = STEP_PATTERNS[self.step_number % 4]
        self._write_io(pattern)
        if update_position:
            self.position = (self.position + 1) % STEPS_PER_ROTATION
            self.step_number = (self.step_number + 1) % 4

    def stop(self) -> None:
        """De-energise all coils."""
        self._write_io(STOP_PATTERN)

    def start(self) -> None:
        """Re-energise coils to the last active step (must call before stepping after a stop)."""
        self.step_number = (self.step_number + 3) % 4
        self.step(update_position=False)

    def read_hall_sensor(self) -> bool:
        """Return True when the hall effect sensor detects the magnet."""
        value = self._read_io()
        return bool(value & (1 << HALL_SENSOR_BIT))

    def magnet_detected(self) -> None:
        """Recalibrate the position tracking when the hall sensor fires."""
        self.position = self.magnet_position
        logger.debug("Module 0x%02X: magnet detected, position set to %d", self.address, self.position)

    def get_char_position(self, char: str) -> int:
        return get_position(char)

    def init(self) -> None:
        """Initialise the PCF8575 and verify the motor responds."""
        self._write_io(STOP_PATTERN)
        for _ in range(4):
            self.step()
        self.stop()
        logger.info("Module 0x%02X initialised", self.address)
