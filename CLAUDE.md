# Splitflap Alarm Clock

Raspberry Pi controller for a splitflap alarm clock with internet radio, AirPlay,
and iPhone sleep-schedule sync.

## Architecture

```
main.py → wires all subsystems together at startup

splitflap/   — I2C driver for PCF8575-based splitflap modules (ported from C++)
hardware/    — GPIO buttons, SK6812/WS2812 LED strip, DAC/amp mute lines
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

## Hardware (Pi Zero W)

The Pi Zero W sits on top of the speaker board like a HAT, **component side
down**: a normally soldered header (pins pointing up from the component side)
plugs into the board's 2×20 socket with the Pi flipped over, its body over the
board. Mounted component side up, the pinout is mirrored and 5 V lands on
GPIO 20/21. KiCad design: `pcb/splitflap_speaker_board/`.

| Bus / pin (BCM) | Devices |
|-----|---------|
| I2C (bus 1, GPIO 2/3) | PCF8575 × 4 at 0x20–0x23 (splitflap, via BSS138 level shifter to 5 V) |
| SPI (GPIO 10 MOSI) | SK6812/WS2812 LED strip data |
| GPIO 0 | Snooze button (pull-up, active low) — HAT ID_SD pin, keep `i2c_vc` off |
| GPIO 12 | LED toggle button ("Illu" header, pull-up, active low) |
| I2S (GPIO 18/19/21) | PCM5122 DAC (hardware mode) → MAX9744 → speakers |
| GPIO 22 / 16 / 23 / 25 | DAC XSMT (high = play) / ATT0 (low = 0 dB) / MAST (keep low) / FLT |
| GPIO 4 / 24 | Amp ~SHDN (high = on) / MUTE (high = muted) |

- The MAX9744 runs in **analog volume mode** (ADDR1/ADDR2 grounded): the slide
  pot wiper drives its VOL pin directly. Software volume is MPD's stream level.
- The DAC uses `dtoverlay=hifiberry-dac` (see `systemd/README.md`).
- Power: 12 V in → TPS562243 buck (6.6 V) → LM1084-5.0 (with heatsink) → 5 V
  rail, which also powers the Pi through header pins 2/4.

**Important:** the LED strip uses SPI mode (GPIO 10), not PWM, to avoid conflict with
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

- Per-module mechanical step offsets → fill in `config.yaml:splitflap.module_offsets`
- SK6812 strip LED count → fill in `config.yaml:led.num_leds`
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
