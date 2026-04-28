# Splitflap Alarm Clock

Raspberry Pi controller for a splitflap alarm clock with internet radio, AirPlay,
and iPhone sleep-schedule sync.

## Architecture

```
main.py → wires all subsystems together at startup

splitflap/   — I2C driver for PCF8575-based splitflap modules (ported from C++)
hardware/    — GPIO buttons, ADS1115 ADC potentiometer, SK6812 LED strip
audio/       — MPD radio client, AirPlay session monitor
clock/       — time display thread, APScheduler alarm, alarm actions
web/         — FastAPI REST API + mobile web UI
provision/   — first-boot WiFi AP mode via NetworkManager
```

## Running locally (no hardware)

```bash
python3 -m venv venv
venv/bin/pip install -r requirements.txt
venv/bin/python main.py
```

Hardware libraries (gpiozero/lgpio, smbus2, neopixel_spi) fall back to mocks
in `hardware/mock.py` (or in-place no-op stubs) when not on a Pi. MPD must be running for audio; without it
the player stub logs warnings but does not crash.

Web UI: http://localhost  
API docs: http://localhost/docs

## Hardware (Pi 3A+)

| Bus | Devices |
|-----|---------|
| I2C (bus 1, GPIO 2/3) | PCF8575 × 4 at 0x20–0x23 (splitflap), ADS1115 at 0x48 (volume pot) |
| SPI (GPIO 10 MOSI) | SK6812 RGBW LED strip |
| GPIO 17 | Snooze button (pull-up, active low) |
| GPIO 27 | LED toggle button (pull-up, active low) |
| I2S (GPIO 18–21) | HAT DAC → MAX9744 → speakers |

**Important:** SK6812 uses SPI mode (GPIO 10), not PWM, to avoid conflict with
the I2S DAC on GPIO 18. Driven via `adafruit-circuitpython-neopixel-spi`,
which works on any Pi without hardware-revision detection (unlike rpi_ws281x,
whose hard-coded board table rejects new revisions).

## Key conventions

- All hardware imports are wrapped in try/except; mock equivalents live in
  `hardware/mock.py`. Never import hardware libs at module level without a fallback.
- Config is read from and written to `config.yaml` at runtime — do not cache it
  at startup in long-lived objects.
- The splitflap display holds a threading.Lock; never call display methods from
  two threads concurrently.
- Alarm time is stored and scheduled in **local wall-clock time**. The
  scheduler resolves the system timezone via `tzlocal.get_localzone()` (no
  explicit `timezone=` kwarg — `"local"` is not a valid IANA zone name).
  Do not convert to UTC; APScheduler's CronTrigger handles DST inside the
  resolved zone. Set the Pi's zone with `sudo timedatectl set-timezone ...`.
- AirPlay detection uses a sentinel file `/run/airplay-active` written by
  shairport-sync sessioncontrol scripts (see `systemd/README.md`).

## Open questions (deferred until hardware bench)

- DAC HAT I2C address — check for collision with ADS1115 at 0x48
- Per-module mechanical step offsets → fill in `config.yaml:splitflap.module_offsets`
- SK6812 strip LED count → fill in `config.yaml:led.num_leds`
- Additional ADS1115 channels needed?
- Snooze button behaviour when no alarm is active

## System services required on Pi

- `mpd` — audio playback (PipeWire output)
- `shairport-sync` — AirPlay receiver (PipeWire output)
- `pipewire` / `pipewire-pulse` — audio router
- `avahi-daemon` — mDNS (`splitflapclock.local`)
- `systemd-timesyncd` — NTP via `time.cloudflare.com`

Setup instructions: `systemd/README.md`

## Roadmap

- **Phase 1** (done) — web interface, all hardware drivers, alarm, radio, AirPlay
- **Phase 2** — sunrise LED ramp, sleep mode, multi-profile alarms, HomeKit (HAP-python)
- **Phase 3** — native iOS app (SwiftUI, HealthKit wake-up integration)
