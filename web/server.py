"""
FastAPI web server — exposes all clock controls as a REST API and serves
the mobile-optimised web UI.

The `app_state` dict is populated by main.py before the server starts and
holds references to every subsystem (player, display, led, scheduler, etc.).
"""
import logging
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

logger = logging.getLogger(__name__)

CONFIG_PATH = Path(__file__).parent.parent / "config.yaml"
TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

app = FastAPI(title="Splitflap Clock", version="1.0.0")
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# Injected by main.py; state["config"] is the single shared config dict
# that all subsystems (alarm_ctrl, player, etc.) hold references to.
# Mutating state["config"] propagates changes everywhere without re-reading disk.
state: dict[str, Any] = {}


def _cfg() -> dict:
    return state["config"]


def _save_cfg() -> None:
    with open(CONFIG_PATH, "w") as f:
        yaml.safe_dump(state["config"], f, default_flow_style=False, allow_unicode=True)


# ---------------------------------------------------------------------------
# UI
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    cfg = _cfg()
    return templates.TemplateResponse("index.html", {"request": request, "config": cfg})


# ---------------------------------------------------------------------------
# Status
# ---------------------------------------------------------------------------

@app.get("/status")
async def get_status():
    player = state.get("player")
    led = state.get("led")
    alarm_ctrl = state.get("alarm_ctrl")
    scheduler = state.get("scheduler")
    airplay = state.get("airplay")
    cfg = _cfg()

    return {
        "alarm": {
            "enabled": scheduler.alarm_enabled if scheduler else False,
            "time": scheduler.alarm_time if scheduler else None,
            "active": alarm_ctrl.is_active if alarm_ctrl else False,
            "snooze_active": scheduler.snooze_active if scheduler else False,
        },
        "radio": player.status() if player else {"state": "unavailable"},
        "airplay_active": airplay.is_active if airplay else False,
        "led": {
            "on": led.is_on if led else False,
            "brightness": led.brightness if led else 0,
        },
        "snooze_duration_minutes": cfg["snooze"]["duration_minutes"],
    }


# ---------------------------------------------------------------------------
# Alarm
# ---------------------------------------------------------------------------

class AlarmPayload(BaseModel):
    time: str   # "HH:MM"
    enabled: bool = True


@app.post("/alarm")
async def set_alarm(payload: AlarmPayload):
    scheduler = state.get("scheduler")
    if not scheduler:
        raise HTTPException(503, "Scheduler unavailable")
    try:
        scheduler.set_alarm(payload.time)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    cfg = _cfg()
    cfg["alarm"]["time"] = payload.time
    cfg["alarm"]["enabled"] = True
    _save_cfg()
    return {"status": "ok", "alarm_time": payload.time}


@app.delete("/alarm")
async def clear_alarm():
    scheduler = state.get("scheduler")
    if scheduler:
        scheduler.clear_alarm()
    cfg = _cfg()
    cfg["alarm"]["enabled"] = False
    _save_cfg()
    return {"status": "ok"}


@app.post("/alarm/snooze")
async def snooze_alarm():
    alarm_ctrl = state.get("alarm_ctrl")
    if not alarm_ctrl:
        raise HTTPException(503, "Alarm controller unavailable")
    alarm_ctrl.snooze()
    return {"status": "ok"}


@app.post("/alarm/dismiss")
async def dismiss_alarm():
    alarm_ctrl = state.get("alarm_ctrl")
    if not alarm_ctrl:
        raise HTTPException(503, "Alarm controller unavailable")
    alarm_ctrl.dismiss()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Radio stations
# ---------------------------------------------------------------------------

class StationPayload(BaseModel):
    name: str
    url: str


@app.get("/stations")
async def list_stations():
    cfg = _cfg()
    return {"stations": cfg["radio"]["stations"], "default": cfg["radio"]["default_station"]}


@app.post("/stations")
async def add_station(payload: StationPayload):
    cfg = _cfg()
    cfg["radio"]["stations"].append({"name": payload.name, "url": payload.url})
    _save_cfg()
    player = state.get("player")
    if player:
        player.add_station(payload.name, payload.url)
    return {"status": "ok", "stations": cfg["radio"]["stations"]}


# ---------------------------------------------------------------------------
# Radio playback
# ---------------------------------------------------------------------------

class PlayPayload(BaseModel):
    station_index: int | None = None


@app.post("/radio/play")
async def play_radio(payload: PlayPayload = PlayPayload()):
    player = state.get("player")
    if not player:
        raise HTTPException(503, "Player unavailable")
    player.play_station(payload.station_index)

    cfg = _cfg()
    if payload.station_index is not None:
        cfg["radio"]["default_station"] = payload.station_index
        _save_cfg()
    return {"status": "ok"}


@app.post("/radio/stop")
async def stop_radio():
    player = state.get("player")
    if not player:
        raise HTTPException(503, "Player unavailable")
    player.stop()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Volume
# ---------------------------------------------------------------------------

class VolumePayload(BaseModel):
    level: int  # 0–100


@app.post("/volume")
async def set_volume(payload: VolumePayload):
    if not (0 <= payload.level <= 100):
        raise HTTPException(400, "Volume must be 0–100")
    player = state.get("player")
    if player:
        player.set_volume(payload.level)
    cfg = _cfg()
    cfg["radio"]["volume"] = payload.level
    _save_cfg()
    return {"status": "ok", "volume": payload.level}


# ---------------------------------------------------------------------------
# LED
# ---------------------------------------------------------------------------

class LEDPayload(BaseModel):
    on: bool
    brightness: int | None = None   # 0–255


@app.post("/led")
async def set_led(payload: LEDPayload):
    led = state.get("led")
    if not led:
        raise HTTPException(503, "LED strip unavailable")
    if payload.on:
        led.turn_on(payload.brightness)
    else:
        led.turn_off()

    cfg = _cfg()
    if payload.brightness is not None:
        cfg["led"]["brightness"] = payload.brightness
    _save_cfg()
    return {"status": "ok"}


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

class SettingsPayload(BaseModel):
    snooze_duration_minutes: int | None = None
    default_station: int | None = None


@app.get("/settings")
async def get_settings():
    return _cfg()


@app.post("/settings")
async def update_settings(payload: SettingsPayload):
    cfg = _cfg()
    if payload.snooze_duration_minutes is not None:
        if not (1 <= payload.snooze_duration_minutes <= 30):
            raise HTTPException(400, "Snooze duration must be 1–30 minutes")
        cfg["snooze"]["duration_minutes"] = payload.snooze_duration_minutes
    if payload.default_station is not None:
        cfg["radio"]["default_station"] = payload.default_station
    _save_cfg()
    return {"status": "ok"}
