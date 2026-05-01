# System setup notes

## 0. microSD card

Flash **Raspberry Pi OS Lite 64-bit (Bookworm)** — Desktop is unnecessary for
this headless controller and would push past 8 GB on its own.

Disk usage budget:

| Component | Approx. |
|---|---|
| Pi OS Lite | ~2 GB |
| `mpd`, `shairport-sync`, `pipewire`/`pipewire-pulse`, `avahi`, NetworkManager, `i2c-tools` | ~300 MB |
| Python venv (`requirements.txt`) | ~150–250 MB |
| App code | < 10 MB |
| Logs, journal, swap, apt cache, OS update headroom | ~1–2 GB |

Total in use: **~4–5 GB**. Radio is streamed and AirPlay is transient, so no
local media storage is needed.

Recommended sizes:

- 8 GB — works but tight; apt caches and OS updates can fill it.
- **16 GB — recommended.** Comfortable headroom and lets flash wear-leveling breathe.
- 32 GB — future-proof for Phase 2/3 features.

Use an A1- or A2-rated card (e.g. SanDisk Extreme, Samsung Evo Plus).

## 1. Install the systemd service

```bash
sudo cp systemd/radio-clock.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable radio-clock
sudo systemctl start radio-clock
```

## 2. NTP — point timesyncd at Cloudflare

Edit `/etc/systemd/timesyncd.conf`:

```ini
[Time]
NTP=time.cloudflare.com
FallbackNTP=pool.ntp.org
```

Then restart: `sudo systemctl restart systemd-timesyncd`

## 3. mDNS hostname

```bash
sudo hostnamectl set-hostname splitflapclock
sudo systemctl restart avahi-daemon
```

The clock is then reachable at `http://splitflapclock.local`.

## 4. shairport-sync (AirPlay)

Install: `sudo apt install shairport-sync`

Add session hooks to `/etc/shairport-sync.conf`:

```
sessioncontrol = {
    run_this_before_play_begins = "/usr/local/bin/airplay-start";
    run_this_after_play_ends    = "/usr/local/bin/airplay-stop";
};
```

Create the two scripts:

```bash
sudo tee /usr/local/bin/airplay-start <<'EOF'
#!/bin/sh
touch /run/airplay-active
EOF

sudo tee /usr/local/bin/airplay-stop <<'EOF'
#!/bin/sh
rm -f /run/airplay-active
EOF

sudo chmod +x /usr/local/bin/airplay-start /usr/local/bin/airplay-stop
sudo systemctl restart shairport-sync
```

## 5. MPD

Install: `sudo apt install mpd`

Ensure MPD uses PipeWire output. Edit `/etc/mpd.conf`:

```
audio_output {
    type    "pipewire"
    name    "PipeWire"
}
```

## 6. Python environment

```bash
cd /home/admin/Projects/radio
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 7. DAC HAT

Add the following to `/boot/firmware/config.txt` (before the `[pi4]` section if one exists):

```
dtoverlay=rpi-dacplus
```

Reboot, then verify the DAC appears:

```bash
aplay -l   # should show "RPi DAC+" as a card
```

Configure shairport-sync to output to the DAC — edit `/etc/shairport-sync.conf`:

```
alsa = {
    output_device = "hw:DAC";
};
```

Set PipeWire's default sink to the DAC so MPD also routes there:

```bash
pactl set-default-sink alsa_output.platform-soc_sound.stereo-fallback
```

To persist across reboots, add to `/etc/pipewire/pipewire.conf.d/default-sink.conf`:

```json
context.properties = {
    default.audio.sink = "alsa_output.platform-soc_sound.stereo-fallback"
}
```

Set DAC output levels to maximum (do once, then store):

```bash
amixer -c DAC sset Digital 100%
amixer -c DAC sset Analogue 100%
amixer -c DAC sset 'Analogue Playback Boost' 1
sudo alsactl store
```

## 8. I2C / SPI

Enable in `raspi-config` → Interface Options:
- I2C → Enable
- SPI → Enable (for SK6812 LED strip)

Add software I2C bus for splitflap modules in `/boot/firmware/config.txt`:

```
dtoverlay=i2c-gpio,bus=3,i2c_gpio_sda=23,i2c_gpio_scl=24
```

Bus layout:
- **Bus 1** (GPIO 2/3): DAC HAT (kernel-managed at 0x4C) only
- **Bus 3** (GPIO 23/24): PCF8575 × 4 (0x20–0x23), ADS1115 (0x49), MAX9744 (0x4B)

Bus 3 requires external 4.7 kΩ pull-up resistors from SDA and SCL to 3.3 V. All devices must be powered from the Pi's 3.3 V rail (pin 1 or 17).

Solder address pads on PCF8575 boards before connecting (all four default to 0x20 and will lock the bus):

| Module | A0 | A1 | Address |
|--------|----|----|---------|
| 0 | — | — | 0x20 |
| 1 | ● | — | 0x21 |
| 2 | — | ● | 0x22 |
| 3 | ● | ● | 0x23 |

Verify bus 3 devices after wiring (connect one at a time):

```bash
i2cdetect -y 3
```

Expected addresses: 0x20, 0x21, 0x22, 0x23 (PCF8575), 0x49 (ADS1115), 0x4B (MAX9744)
