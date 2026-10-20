"""
GPIO control lines for the audio path: PCM5122 DAC → MAX9744 amp.

Neither chip is on I2C. The DAC runs in hardware (pin-strapped) mode and the
amp runs in analog-volume mode, with the slide pot wired straight to its VOL
pin. So volume is set by the pot (amp gain) and by MPD (stream level). This
module only drives the mute/shutdown/strap lines the board routes to the Pi:

  PCM5122 XSMT  — soft mute, high = play
  PCM5122 ATT0  — gain strap, low = 0 dB (high = +3 dB, which clips full-scale audio)
  PCM5122 MAST  — must stay low: the DAC is an I2S slave of the Pi
  PCM5122 FLT   — low = normal-latency interpolation filter
  MAX9744 ~SHDN — high = amp on
  MAX9744 MUTE  — high = outputs muted

The MAX9744 datasheet asks for MUTE to be held high while powering up and
down to avoid pops, so unmute() is the last step of start-up and mute() the
first step of shutdown.
"""
import logging
import time

logger = logging.getLogger(__name__)

try:
    from gpiozero import DigitalOutputDevice
    _HAS_GPIO = True
except ImportError as _exc:
    _HAS_GPIO = False
    logger.warning("gpiozero not available — audio control lines disabled (%s)", _exc)

# Default BCM pin numbers, matching the splitflap speaker board.
DEFAULT_PINS = {
    "dac_xsmt_pin": 22,
    "dac_att0_pin": 16,
    "dac_mast_pin": 23,
    "dac_flt_pin": 25,
    "amp_shdn_pin": 4,
    "amp_mute_pin": 24,
}


class _NullPin:
    def on(self) -> None: pass
    def off(self) -> None: pass
    def close(self) -> None: pass


class AudioControl:
    def __init__(self, pins: dict | None = None):
        cfg = {**DEFAULT_PINS, **(pins or {})}
        self._muted = True

        def out(name: str, initial: bool):
            if not _HAS_GPIO:
                return _NullPin()
            try:
                return DigitalOutputDevice(cfg[name], initial_value=initial)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Audio pin %s (GPIO%s) unavailable: %s", name, cfg[name], exc)
                return _NullPin()

        # Hold everything quiet while the straps settle.
        self._amp_mute = out("amp_mute_pin", True)
        self._amp_shdn = out("amp_shdn_pin", True)
        self._dac_xsmt = out("dac_xsmt_pin", False)
        self._dac_att0 = out("dac_att0_pin", False)
        self._dac_mast = out("dac_mast_pin", False)
        self._dac_flt = out("dac_flt_pin", False)
        logger.info(
            "Audio control ready (XSMT=GPIO%d, ~SHDN=GPIO%d, MUTE=GPIO%d)",
            cfg["dac_xsmt_pin"], cfg["amp_shdn_pin"], cfg["amp_mute_pin"],
        )

    def unmute(self) -> None:
        self._dac_xsmt.on()
        time.sleep(0.05)  # let the DAC's soft-unmute ramp finish first
        self._amp_mute.off()
        self._muted = False

    def mute(self) -> None:
        self._amp_mute.on()
        self._dac_xsmt.off()
        self._muted = True

    @property
    def muted(self) -> bool:
        return self._muted
