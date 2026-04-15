"""
Mock implementations of hardware libraries so the full stack can be developed
and tested on any Linux/Mac machine without physical Raspberry Pi hardware.

Usage — each hardware module does a graceful fallback:

    try:
        import RPi.GPIO as GPIO
    except ImportError:
        from hardware.mock import MockGPIO as GPIO
"""
import logging

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# smbus2 mocks
# ---------------------------------------------------------------------------

class MockI2CMsg:
    """Minimal stand-in for smbus2.i2c_msg."""

    def __init__(self, data: list[int] | None = None):
        self._data = data or []

    def __iter__(self):
        return iter(self._data)

    @staticmethod
    def write(addr: int, data: list[int]) -> "MockI2CMsg":
        logger.debug("MockI2C write addr=0x%02X data=%s", addr, data)
        return MockI2CMsg(data)

    @staticmethod
    def read(addr: int, length: int) -> "MockI2CMsg":
        logger.debug("MockI2C read addr=0x%02X len=%d", addr, length)
        return MockI2CMsg([0x00] * length)


class MockSMBus:
    """Stand-in for smbus2.SMBus."""

    def __init__(self, bus_id: int = 1):
        self.bus_id = bus_id

    def i2c_rdwr(self, *msgs) -> None:
        pass

    def write_i2c_block_data(self, addr: int, register: int, data: list[int]) -> None:
        logger.debug("MockSMBus write addr=0x%02X reg=0x%02X data=%s", addr, register, data)

    def read_i2c_block_data(self, addr: int, register: int, length: int) -> list[int]:
        logger.debug("MockSMBus read addr=0x%02X reg=0x%02X len=%d", addr, register, length)
        return [0x00] * length

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# RPi.GPIO mock
# ---------------------------------------------------------------------------

class MockGPIO:
    BCM = "BCM"
    BOARD = "BOARD"
    IN = "IN"
    OUT = "OUT"
    PUD_UP = "PUD_UP"
    PUD_DOWN = "PUD_DOWN"
    FALLING = "FALLING"
    RISING = "RISING"
    BOTH = "BOTH"

    @staticmethod
    def setmode(mode) -> None:
        pass

    @staticmethod
    def setup(pin, direction, pull_up_down=None, initial=None) -> None:
        pass

    @staticmethod
    def add_event_detect(pin, edge, callback=None, bouncetime=None) -> None:
        pass

    @staticmethod
    def remove_event_detect(pin) -> None:
        pass

    @staticmethod
    def input(pin) -> int:
        return 0

    @staticmethod
    def output(pin, value) -> None:
        pass

    @staticmethod
    def cleanup() -> None:
        pass


# ---------------------------------------------------------------------------
# rpi_ws281x mock
# ---------------------------------------------------------------------------

class MockColor:
    def __init__(self, r: int, g: int, b: int, w: int = 0):
        self.r, self.g, self.b, self.w = r, g, b, w


class MockPixelStrip:
    def __init__(self, num: int, pin: int, *args, **kwargs):
        self._num = num
        self._pixels = [MockColor(0, 0, 0, 0)] * num

    def begin(self) -> None:
        pass

    def numPixels(self) -> int:
        return self._num

    def setPixelColor(self, n: int, color) -> None:
        if 0 <= n < self._num:
            self._pixels[n] = color

    def setBrightness(self, brightness: int) -> None:
        logger.debug("MockPixelStrip brightness=%d", brightness)

    def show(self) -> None:
        logger.debug("MockPixelStrip show()")

    def getPixelColor(self, n: int):
        return self._pixels[n] if 0 <= n < self._num else 0


def mock_color(r: int, g: int, b: int, w: int = 0) -> MockColor:
    return MockColor(r, g, b, w)
