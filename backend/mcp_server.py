"""Forge MCP server — lets any MCP agent (Claude, etc.) drive Forge CAD natively.

It wraps the running Forge backend's operation API over HTTP, exposing CAD as a
small set of tools: create/list/get parts and run any catalog operation
(add_primitive, set_feature_params, fillet, hole, measure, ai_edit, …).

Run (with the Forge backend already up on :8000):
    cd backend && .venv/bin/python mcp_server.py

Register with Claude Code / Desktop by pointing an MCP stdio server at that command.
Configure the backend URL with FORGE_API (default http://localhost:8000/api).
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

import httpx
from mcp.server.fastmcp import FastMCP

API = os.environ.get("FORGE_API", "http://localhost:8000/api")

mcp = FastMCP("forge-cad")


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, timeout=120.0)


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


if __name__ == "__main__":
    mcp.run()
