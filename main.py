"""
Splitflap Alarm Clock — entry point.

Startup sequence:
  1. Load config
  2. Check WiFi — if not connected, start provisioning AP and serve setup UI
  3. Initialise hardware (I2C bus, splitflap display, LED strip, buttons, ADC)
  4. Home the splitflap display, then start the live time-display thread
  5. Connect to MPD and start AirPlay monitor
  6. Restore alarm from config
  7. Start FastAPI web server
"""
import logging
import signal
import sys
from pathlib import Path

import uvicorn
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)


def build_i2c_bus(cfg: dict):
    try:
        import smbus2
        return smbus2.SMBus(cfg["hardware"]["i2c_bus"])
    except (ImportError, FileNotFoundError):
        logger.warning("smbus2 / I2C not available — using mock I2C bus")
        from hardware.mock import MockSMBus
        return MockSMBus()


def _any_device_responds(bus, addresses: list[int]) -> bool:
    """Return True if any of the given I2C addresses ACKs a 1-byte read."""
    try:
        from smbus2 import i2c_msg
    except ImportError:
        return True  # mock bus path — nothing to probe
    for addr in addresses:
        try:
            bus.i2c_rdwr(i2c_msg.read(addr, 1))
            return True
        except OSError:
            continue
    return False


def build_display(bus, cfg: dict):
    from splitflap.module import SplitFlapModule
    from splitflap.display import SplitFlapDisplay

    addresses = [0x20, 0x21, 0x22, 0x23]
    if not _any_device_responds(bus, addresses):
        logger.warning(
            "No PCF8575 splitflap modules detected on I2C — using mock bus for display"
        )
        from hardware.mock import MockSMBus
        bus = MockSMBus()

    offsets = cfg["splitflap"]["module_offsets"]
    magnet_pos = cfg["splitflap"]["magnet_position"]

    modules = [
        SplitFlapModule(bus, addr, offset=offsets[i], magnet_position=magnet_pos)
        for i, addr in enumerate(addresses)
    ]
    return SplitFlapDisplay(modules, speed_rpm=cfg["splitflap"]["speed_rpm"])


def build_led(cfg: dict):
    # LED count is stored in config once known; default to 30 until bench is built
    led_cfg = cfg.get("led", {})
    num_leds = led_cfg.get("num_leds", 30)
    brightness = led_cfg["brightness"]
    strip_type = led_cfg.get("strip_type", "SK6812")
    from hardware.led_strip import LEDStrip
    return LEDStrip(num_leds=num_leds, brightness=brightness, strip_type=strip_type)


def build_player(cfg: dict):
    from audio.player import RadioPlayer
    player = RadioPlayer(stations=cfg["radio"]["stations"])
    player.connect()
    return player


def build_potentiometer(bus, cfg: dict, on_change):
    from hardware.potentiometer import Potentiometer
    hw = cfg["hardware"]
    address = hw["ads1115"]["address"]
    if not _any_device_responds(bus, [address]):
        logger.warning(
            "ADS1115 not detected at 0x%02X — using mock bus for potentiometer",
            address,
        )
        from hardware.mock import MockSMBus
        bus = MockSMBus()
    pot = Potentiometer(
        bus,
        address=address,
        poll_hz=hw["ads1115"]["poll_hz"],
        on_change=on_change,
    )
    pot.start()
    return pot


def main() -> None:
    cfg = load_config()

    # ── WiFi check ────────────────────────────────────────────────────────
    from provision.ap_mode import wifi_connected, start_hotspot, provision_app

    if not wifi_connected():
        logger.info("No WiFi connection — starting provisioning hotspot")
        try:
            start_hotspot()
        except Exception:
            logger.exception("Failed to start hotspot — continuing anyway")

        logger.info("Serving provisioning UI on http://192.168.4.1:80")
        uvicorn.run(provision_app, host="0.0.0.0", port=80, log_level="warning")
        # After provisioning the Pi should reboot or restart this service.
        return

    # ── Hardware init ─────────────────────────────────────────────────────
    bus = build_i2c_bus(cfg)
    display = build_display(bus, cfg)
    led = build_led(cfg)
    player = build_player(cfg)

    display.init_all()
    display.home_all()

    # ── Clock subsystems ──────────────────────────────────────────────────
    from clock.time_display import TimeDisplay
    from clock.scheduler import AlarmScheduler
    from clock.alarm_actions import AlarmController
    from audio.airplay import AirPlayMonitor

    alarm_ctrl_ref: list = []   # forward reference for circular dependency

    def on_alarm():
        if alarm_ctrl_ref:
            alarm_ctrl_ref[0].on_alarm()

    def on_snooze_end():
        if alarm_ctrl_ref:
            alarm_ctrl_ref[0].on_snooze_end()

    scheduler = AlarmScheduler(on_alarm=on_alarm, on_snooze_end=on_snooze_end)
    alarm_ctrl = AlarmController(player, display, led, scheduler, cfg)
    alarm_ctrl_ref.append(alarm_ctrl)

    airplay = AirPlayMonitor(player)
    airplay.start()

    time_display = TimeDisplay(display)
    time_display.start()
    time_display.force_update()

    # ── Potentiometer (volume) ────────────────────────────────────────────
    pot = build_potentiometer(bus, cfg, on_change=player.set_volume)

    # ── Buttons ───────────────────────────────────────────────────────────
    from hardware.buttons import ButtonHandler
    hw = cfg["hardware"]["buttons"]
    led_ramp_ms = cfg.get("led", {}).get("ramp_ms", 500)
    buttons = ButtonHandler(
        snooze_pin=hw["snooze_pin"],
        led_pin=hw["led_pin"],
        double_press_ms=hw["double_press_ms"],
        led_hold_seconds=hw.get("led_hold_seconds", 5.0),
        on_snooze=alarm_ctrl.snooze,
        on_dismiss=alarm_ctrl.dismiss,
        on_led_press=lambda: led.fade_on(led_ramp_ms),
        on_led_release=lambda: led.fade_off(led_ramp_ms),
    )

    # ── Restore alarm from config ─────────────────────────────────────────
    if cfg["alarm"].get("enabled") and cfg["alarm"].get("time"):
        scheduler.set_alarm(cfg["alarm"]["time"])
        logger.info("Restored alarm: %s", cfg["alarm"]["time"])

    # ── Web server ────────────────────────────────────────────────────────
    from web.server import app, state
    state.update({
        "config": cfg,
        "player": player,
        "display": display,
        "led": led,
        "scheduler": scheduler,
        "alarm_ctrl": alarm_ctrl,
        "airplay": airplay,
    })

    # ── Graceful shutdown ─────────────────────────────────────────────────
    def shutdown(sig, frame):
        logger.info("Shutting down…")
        time_display.stop()
        airplay.stop()
        pot.stop()
        scheduler.shutdown()
        buttons.cleanup()
        led.turn_off()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    logger.info("Starting web server — http://splitflapclock.local")
    uvicorn.run(app, host="0.0.0.0", port=80, log_level="warning")


if __name__ == "__main__":
    main()
