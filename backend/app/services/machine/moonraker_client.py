"""Moonraker client — the HTTP API behind Mainsail/Fluidd (Klipper).

Talks to a user-configured Moonraker endpoint to query status, upload G-code,
control jobs, and jog. Everything degrades gracefully: if the printer is
unreachable, calls return a clear offline status rather than raising, so the UI
can render a "not connected" state instead of erroring.
"""

from __future__ import annotations

from typing import Optional

import httpx

_TIMEOUT = httpx.Timeout(4.0, connect=2.0)

_STATUS_OBJECTS = "extruder&heater_bed&print_stats&display_status&toolhead"


class MoonrakerError(Exception):
    pass


def _base(url: str) -> str:
    return url.rstrip("/")


def get_status(url: str) -> dict:
    """Return a normalized status dict. `connected` is False if unreachable."""
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.get(f"{_base(url)}/printer/objects/query?{_STATUS_OBJECTS}")
            r.raise_for_status()
            status = r.json()["result"]["status"]
    except Exception as exc:  # offline / bad URL / Klipper down
        return {"connected": False, "error": str(exc)}

    extruder = status.get("extruder", {})
    bed = status.get("heater_bed", {})
    print_stats = status.get("print_stats", {})
    display = status.get("display_status", {})
    toolhead = status.get("toolhead", {})

    return {
        "connected": True,
        "state": print_stats.get("state", "unknown"),
        "filename": print_stats.get("filename", ""),
        "progress": round(float(display.get("progress", 0.0)), 4),
        "print_duration": print_stats.get("print_duration", 0.0),
        "extruder_temp": round(float(extruder.get("temperature", 0.0)), 1),
        "extruder_target": round(float(extruder.get("target", 0.0)), 1),
        "bed_temp": round(float(bed.get("temperature", 0.0)), 1),
        "bed_target": round(float(bed.get("target", 0.0)), 1),
        "position": toolhead.get("position", [0, 0, 0, 0])[:3],
    }


def send_gcode(url: str, script: str) -> dict:
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_base(url)}/printer/gcode/script", params={"script": script})
            r.raise_for_status()
        return {"ok": True}
    except Exception as exc:
        raise MoonrakerError(str(exc))


def jog(url: str, axis: str, distance: float, feed: float = 1500.0) -> dict:
    axis = axis.upper()
    if axis not in ("X", "Y", "Z"):
        raise MoonrakerError(f"Invalid jog axis: {axis}")
    # Relative move then restore absolute mode.
    script = f"G91\nG1 {axis}{distance} F{feed}\nG90"
    return send_gcode(url, script)


def home(url: str, axes: str = "") -> dict:
    return send_gcode(url, f"G28 {axes}".strip())


def upload_gcode(url: str, filename: str, content: str, start: bool = False) -> dict:
    try:
        with httpx.Client(timeout=httpx.Timeout(15.0, connect=2.0)) as c:
            files = {"file": (filename, content.encode("utf-8"), "text/plain")}
            data = {"print": "true"} if start else {}
            r = c.post(f"{_base(url)}/server/files/upload", files=files, data=data)
            r.raise_for_status()
            return {"ok": True, "result": r.json()}
    except Exception as exc:
        raise MoonrakerError(str(exc))


def print_action(url: str, action: str, filename: Optional[str] = None) -> dict:
    endpoints = {
        "start": ("/printer/print/start", {"filename": filename}),
        "pause": ("/printer/print/pause", {}),
        "resume": ("/printer/print/resume", {}),
        "cancel": ("/printer/print/cancel", {}),
    }
    if action not in endpoints:
        raise MoonrakerError(f"Unknown print action: {action}")
    path, params = endpoints[action]
    try:
        with httpx.Client(timeout=_TIMEOUT) as c:
            r = c.post(f"{_base(url)}{path}", params={k: v for k, v in params.items() if v is not None})
            r.raise_for_status()
        return {"ok": True}
    except Exception as exc:
        raise MoonrakerError(str(exc))
