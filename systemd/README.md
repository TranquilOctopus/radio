# System setup notes

## Hardware: Raspberry Pi Zero 2 W

The clock runs on a **Raspberry Pi Zero 2 W** (quad-core 64-bit Cortex-A53,
512 MB RAM, 2.4 GHz-only WiFi) mounted on the splitflap speaker board:

- Solder the 2×20 header **from the top** so the pins point down out of the
  underside, then plug the Pi into the board's socket **component side up**.
  The socket is wired for this orientation only; mounted the other way up,
  5 V lands on GPIO pins and the Pi is damaged.
- The Pi is powered from the board's 5 V rail through header pins 2/4. Don't
  also plug in a micro-USB supply.
- WiFi is 2.4 GHz only, so the provisioning step needs a 2.4 GHz network.

## 0. microSD card

Flash **Raspberry Pi OS Lite 64-bit (Bookworm)**. Desktop is unnecessary for
this headless controller and would push past 8 GB on its own, and the Zero
2 W's 512 MB of RAM is better spent on the app.

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

## 7. I2C / SPI / I2S DAC

Enable in `raspi-config` → Interface Options:
- I2C → Enable
- SPI → Enable (LED strip data on GPIO 10 / SPI0 MOSI)

The PCM5122 DAC on the speaker board runs in hardware (pin-strapped) mode with
no I2C control, so it uses the plain I2S DAC overlay. In
`/boot/firmware/config.txt`:

```
dtparam=audio=off
dtoverlay=hifiberry-dac
```

Reboot, then check the card shows up: `aplay -l`

Verify I2C devices are visible: `i2cdetect -y 1`
Expected addresses: 0x20, 0x21, 0x22, 0x23 (PCF8575 splitflap modules). The
DAC and amp are not on I2C.
