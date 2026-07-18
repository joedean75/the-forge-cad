"""Forge MCP server — lets any MCP agent (Claude, etc.) drive Forge CAD natively.

It wraps the running Forge backend's operation API over HTTP, exposing CAD as a
small set of tools: create/list/get parts and run any catalog operation
(add_primitive, set_feature_params, fillet, hole, measure, ai_edit, …).

Run (with the Forge backend already up on :8000):
    cd backend && .venv/bin/python mcp_server.py

Register with Claude Code / Desktop by pointing an MCP stdio server at that command.
Configure the backend URL with FORGE_API (default http://localhost:8000/api) and
authenticate with FORGE_API_KEY (create one under Account → API keys; required
when the deployment runs with FORGE_AUTH_REQUIRED=1).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

API = os.environ.get("FORGE_API", "http://localhost:8000/api")
API_KEY = os.environ.get("FORGE_API_KEY")

mcp = FastMCP("forge-cad")


def _client() -> httpx.Client:
    headers = {"X-Api-Key": API_KEY} if API_KEY else {}
    return httpx.Client(base_url=API, timeout=300.0, headers=headers)


def _size(doc: dict) -> Optional[dict]:
    d = doc.get("derived", {})
    b = d.get("bbox")
    if not b:
        return None
    return {
        "x": round((b["max"]["x"] - b["min"]["x"]) * 1000, 2),
        "y": round((b["max"]["y"] - b["min"]["y"]) * 1000, 2),
        "z": round((b["max"]["z"] - b["min"]["z"]) * 1000, 2),
    }


@mcp.tool()
def list_operations() -> str:
    """List the available CAD operations and their parameters (the tool catalog)."""
    with _client() as c:
        return json.dumps(c.get("/operations").json(), indent=2)


@mcp.tool()
def create_part(prompt: str = "", name: str = "") -> str:
    """Create a new part. With a prompt, text-to-CAD generates geometry; otherwise a blank box.

    Returns the new document id, size (mm) and mass (kg).
    """
    body: dict[str, Any] = {"resolution": 96}
    if prompt:
        body["prompt"] = prompt
    if name:
        body["name"] = name
    with _client() as c:
        d = c.post("/documents", json=body).json()
    return json.dumps({"id": d["id"], "name": d["name"], "size_mm": _size(d),
                       "mass_kg": d["derived"]["mass"], "error": d["derived"]["error"]})


@mcp.tool()
def list_parts() -> str:
    """List saved parts (id, name, provenance, updated)."""
    with _client() as c:
        return json.dumps(c.get("/documents").json(), indent=2)


@mcp.tool()
def get_part(doc_id: str) -> str:
    """Get a part's feature tree and size/mass (mesh data omitted)."""
    with _client() as c:
        r = c.get(f"/documents/{doc_id}")
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text}"
        d = r.json()
    return json.dumps({
        "id": d["id"], "name": d["name"], "provenance": d["provenance"],
        "size_mm": _size(d), "mass_kg": d["derived"]["mass"], "error": d["derived"]["error"],
        "feature_tree": d["feature_tree"], "dimensions": d["dimensions"],
    }, indent=2)


@mcp.tool()
def run_operation(doc_id: str, op: str, args: Optional[dict] = None) -> str:
    """Run one CAD operation on a part (see list_operations for names/params).

    Examples: op="add_primitive" args={"shape":"cylinder","operation":"subtract","params":{"radius":0.005,"height":0.05,"center":{"x":0,"y":0,"z":0}}};
    op="set_feature_params" args={"feature_id":"...","params":{"size":{"x":0.1,"y":0.06,"z":0.01}}};
    op="ai_edit" args={"instruction":"add a fillet to the top edges"}. Lengths are in metres.
    """
    with _client() as c:
        r = c.post(f"/documents/{doc_id}/op", json={"op": op, "args": args or {}})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    data = r.json()
    out: dict[str, Any] = {"ok": data["ok"], "op": data["op"], "result": data["result"]}
    if data.get("document"):
        out["size_mm"] = _size(data["document"])
        out["mass_kg"] = data["document"]["derived"]["mass"]
        out["error"] = data["document"]["derived"]["error"]
    return json.dumps(out)


@mcp.tool()
def copilot(doc_id: str, goal: str, max_steps: int = 8) -> str:
    """Run Forge Copilot: an agentic loop that plans and executes multiple CAD
    operations toward a natural-language goal, verifying as it goes.

    Example goals: "turn this into an 80x40x10 mm plate with 4 corner holes",
    "shell the body to 2 mm and fillet the top edges".
    """
    import time

    with _client() as c:
        r = c.post(f"/documents/{doc_id}/copilot", json={"goal": goal, "max_steps": max_steps})
        if r.status_code >= 400:
            return f"Error {r.status_code}: {r.text}"
        data = r.json()
        # The copilot runs as a background job; poll until it resolves.
        deadline = time.time() + 20 * 60
        while data.get("status") == "running":
            if time.time() > deadline:
                return "Error: copilot timed out"
            time.sleep(2.0)
            r = c.get(f"/documents/{doc_id}/copilot/{data['job_id']}")
            if r.status_code >= 400:
                return f"Error {r.status_code}: {r.text}"
            data = r.json()
    steps = [
        {k: s.get(k) for k in ("op", "ok", "rationale", "error")}
        for s in data["steps"] if s.get("kind") == "op"
    ]
    out = {"summary": data["summary"], "steps": steps}
    if data.get("document"):
        out["size_mm"] = _size(data["document"])
        out["mass_kg"] = data["document"]["derived"]["mass"]
    return json.dumps(out, indent=2)


@mcp.tool()
def import_part(file_path: str, name: str = "", units: str = "auto") -> str:
    """Import a local STL/OBJ/PLY/GLB/OFF or faceted STEP file as an editable part.

    units: auto (default, detects mm vs m), or explicit m/mm/cm/inch.
    Returns the new document id and size.
    """
    p = Path(file_path).expanduser()
    if not p.is_file():
        return f"Error: file not found: {file_path}"
    form: dict[str, Any] = {"units": units}
    if name:
        form["name"] = name
    with _client() as c:
        r = c.post(
            "/documents/import",
            files={"file": (p.name, p.read_bytes(), "application/octet-stream")},
            data=form,
        )
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    d = r.json()
    return json.dumps({"id": d["id"], "name": d["name"], "size_mm": _size(d),
                       "mass_kg": d["derived"]["mass"], "error": d["derived"]["error"]})


@mcp.tool()
def quote_part(doc_id: str, quantity: int = 1) -> str:
    """Get instant manufacturing quotes (CNC / FDM / SLA / metal printing) for a part."""
    with _client() as c:
        r = c.post(f"/documents/{doc_id}/quotes", json={"quantity": quantity})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    return json.dumps(r.json(), indent=2)


@mcp.tool()
def order_part(doc_id: str, process: str, quantity: int = 1, notes: str = "") -> str:
    """Place a manufacturing order (RFQ) for a quoted process (see quote_part)."""
    with _client() as c:
        r = c.post(f"/documents/{doc_id}/orders",
                   json={"process": process, "quantity": quantity, "notes": notes})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    o = r.json()
    return json.dumps({"order_id": o["id"], "status": o["status"], "process": o["process"],
                       "quantity": o["quantity"], "total_usd": o["quote"]["total"],
                       "lead_time_days": o["quote"].get("lead_time_days")})


@mcp.tool()
def slice_part(doc_id: str, layer_height: float = 0.2, infill: float = 0.15,
               wall_count: int = 2, temp_nozzle: int = 210, temp_bed: int = 60) -> str:
    """Slice the part for FDM 3D printing. Returns layer count, print time and
    filament estimates; the G-code is stored on the document, ready to print."""
    with _client() as c:
        r = c.post(f"/documents/{doc_id}/op", json={"op": "slice_part", "args": {"settings": {
            "layer_height": layer_height, "infill": infill, "wall_count": wall_count,
            "temp_nozzle": temp_nozzle, "temp_bed": temp_bed}}})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    return json.dumps(r.json()["result"], indent=2)


@mcp.tool()
def print_part(doc_id: str, printer_url: str, start: bool = False) -> str:
    """Send the part's sliced G-code to a Moonraker/Klipper 3D printer
    (slices with default settings first if needed). start=True begins the print."""
    with _client() as c:
        r = c.post(f"/documents/{doc_id}/op",
                   json={"op": "print_part", "args": {"printer_url": printer_url, "start": start}})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    return json.dumps(r.json()["result"], indent=2)


@mcp.tool()
def export_part_step(doc_id: str, out_path: str) -> str:
    """Export a part as STEP to a local file path."""
    with _client() as c:
        r = c.get(f"/documents/{doc_id}/step", params={"resolution": 128})
    if r.status_code >= 400:
        return f"Error {r.status_code}: {r.text}"
    p = Path(out_path).expanduser()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(r.text)
    return f"Wrote {len(r.text)} chars of STEP to {p}"


if __name__ == "__main__":
    mcp.run()
