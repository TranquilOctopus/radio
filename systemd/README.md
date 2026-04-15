# System setup notes

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
cd /home/alex/Projects/radio
python3 -m venv venv
venv/bin/pip install -r requirements.txt
```

## 7. I2C / SPI

Enable in `raspi-config` → Interface Options:
- I2C → Enable
- SPI → Enable (for SK6812 LED strip)

Verify I2C devices are visible: `i2cdetect -y 1`
Expected addresses: 0x20, 0x21, 0x22, 0x23 (PCF8575), 0x48 (ADS1115)
