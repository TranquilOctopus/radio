"""
WiFi provisioning via NetworkManager.

On first boot (no saved WiFi credentials), the Pi creates a hotspot named
"SplitflapClock-Setup".  The user connects to that network and visits
http://192.168.4.1 to pick their network and enter a password.
NetworkManager handles the AP and client transition — no manual hostapd/dnsmasq
config required on modern Pi OS (Bookworm+).
"""
import logging
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse

logger = logging.getLogger(__name__)

_AP_SSID = "SplitflapClock-Setup"
_AP_IP = "192.168.4.1"
_CONNECT_TIMEOUT = 30   # seconds to wait for WiFi connection after credentials submitted
_SETUP_HTML = Path(__file__).parent / "wifi_setup.html"


def wifi_connected() -> bool:
    """Return True if wlan0 has an IP address (i.e. is connected to a network)."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "DEVICE,STATE", "device"],
            capture_output=True, text=True, timeout=5,
        )
        for line in result.stdout.splitlines():
            if line.startswith("wlan0:connected"):
                return True
        return False
    except Exception:
        return False


def start_hotspot() -> None:
    """Create a NetworkManager hotspot on wlan0."""
    logger.info("Starting provisioning hotspot: %s", _AP_SSID)
    subprocess.run([
        "nmcli", "device", "wifi", "hotspot",
        "ifname", "wlan0",
        "ssid", _AP_SSID,
        "password", "",         # open network — no password
    ], check=True, timeout=15)


def stop_hotspot() -> None:
    """Bring down the hotspot and let NetworkManager manage wlan0 normally."""
    try:
        subprocess.run(
            ["nmcli", "connection", "delete", _AP_SSID],
            capture_output=True, timeout=10,
        )
    except Exception:
        pass


def scan_networks() -> list[dict]:
    """Return a list of visible SSIDs with signal strength."""
    try:
        result = subprocess.run(
            ["nmcli", "-t", "-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list"],
            capture_output=True, text=True, timeout=10,
        )
        networks = []
        seen: set[str] = set()
        for line in result.stdout.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                ssid = parts[0].strip()
                if ssid and ssid not in seen and ssid != _AP_SSID:
                    seen.add(ssid)
                    networks.append({
                        "ssid": ssid,
                        "signal": parts[1] if len(parts) > 1 else "?",
                        "security": parts[2] if len(parts) > 2 else "",
                    })
        return sorted(networks, key=lambda n: -int(n["signal"] or 0))
    except Exception:
        logger.exception("Network scan failed")
        return []


def connect_to_network(ssid: str, password: str) -> bool:
    """Attempt to connect to a WiFi network; return True on success."""
    stop_hotspot()
    try:
        cmd = ["nmcli", "device", "wifi", "connect", ssid]
        if password:
            cmd += ["password", password]
        subprocess.run(cmd, check=True, timeout=_CONNECT_TIMEOUT)
        return wifi_connected()
    except subprocess.CalledProcessError:
        logger.error("nmcli failed to connect to %s", ssid)
        return False
    except subprocess.TimeoutExpired:
        logger.error("Connection to %s timed out", ssid)
        return False


# ---------------------------------------------------------------------------
# Provisioning FastAPI sub-application (mounted at / during AP mode)
# ---------------------------------------------------------------------------

provision_app = FastAPI()


@provision_app.get("/", response_class=HTMLResponse)
async def setup_page(request: Request):
    networks = scan_networks()
    html = _SETUP_HTML.read_text()
    options = "\n".join(
        f'<option value="{n["ssid"]}">{n["ssid"]} ({n["signal"]}%)</option>'
        for n in networks
    )
    html = html.replace("<!-- NETWORK_OPTIONS -->", options)
    return HTMLResponse(html)


@provision_app.post("/connect")
async def do_connect(ssid: str = Form(...), password: str = Form("")):
    def _attempt():
        success = connect_to_network(ssid, password)
        if not success:
            logger.warning("WiFi connection failed — restarting hotspot")
            start_hotspot()

    threading.Thread(target=_attempt, daemon=True).start()
    # Return immediately with a "connecting…" page; the client will poll /status
    return HTMLResponse(
        "<html><body style='font-family:sans-serif;padding:40px'>"
        f"<h2>Connecting to {ssid}…</h2>"
        "<p>This may take up to 30 seconds. Once connected, the hotspot will "
        "disappear and the clock will be reachable at "
        "<a href='http://splitflapclock.local'>http://splitflapclock.local</a>.</p>"
        "</body></html>"
    )


@provision_app.get("/status")
async def provision_status():
    return {"connected": wifi_connected()}
