"""Machine control over Moonraker (Mainsail/Klipper backend).

Outbound calls target user-configured Moonraker endpoints. Status is resilient
(returns an offline marker rather than erroring); control actions surface a 502
when the printer is unreachable.
"""

from __future__ import annotations

import base64
import os

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.security import current_account, require_entitlement
from app.services import accounts, document_service as docs
from app.services.machine import moonraker_client as mr
from app.services.machine.url_guard import UrlNotAllowed, validate_url
from app.services.plans import ENT_MACHINE

router = APIRouter(prefix="/api/machine", tags=["machine"])


def _safe_url(url: str) -> str:
    """Validate an outbound machine URL against the SSRF policy, or 400."""
    try:
        return validate_url(url)
    except UrlNotAllowed as exc:
        raise HTTPException(status_code=400, detail=str(exc))


def _configured_printers() -> list[dict]:
    """Read printers from env MOONRAKER_URLS ("Name|url,Name|url"); default localhost."""
    raw = os.environ.get("MOONRAKER_URLS", "").strip()
    if not raw:
        return [{"name": "Local Klipper", "url": "http://localhost:7125"}]
    out = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        if "|" in entry:
            name, url = entry.split("|", 1)
        else:
            name, url = entry, entry
        out.append({"name": name.strip(), "url": url.strip()})
    return out


class UrlBody(BaseModel):
    url: str


class GcodeBody(BaseModel):
    url: str
    script: str


class JogBody(BaseModel):
    url: str
    axis: str
    distance: float
    feed: float = 1500.0


class HomeBody(BaseModel):
    url: str
    axes: str = ""


class PrintBody(BaseModel):
    url: str
    action: str
    filename: str | None = None


class SendDocumentBody(BaseModel):
    url: str
    doc_id: str
    start: bool = False


@router.get("/printers")
def list_printers(acc: accounts.Account = Depends(current_account)):
    return _configured_printers()


@router.get("/machines")
def list_machines(acc: accounts.Account = Depends(current_account)):
    """The shop's machines — process, envelope, dialect and how a job reaches them."""
    from app.services import machines as reg

    return {"machines": [m.model_dump() for m in reg.list_machines()]}


@router.post("/status")
def status(body: UrlBody, acc: accounts.Account = Depends(current_account)):
    return mr.get_status(_safe_url(body.url))


@router.post("/gcode")
def gcode(body: GcodeBody, acc: accounts.Account = Depends(require_entitlement(ENT_MACHINE))):
    try:
        return mr.send_gcode(_safe_url(body.url), body.script)
    except mr.MoonrakerError as exc:
        raise HTTPException(status_code=502, detail="printer request failed")


@router.post("/jog")
def jog(body: JogBody, acc: accounts.Account = Depends(require_entitlement(ENT_MACHINE))):
    try:
        return mr.jog(_safe_url(body.url), body.axis, body.distance, body.feed)
    except mr.MoonrakerError as exc:
        raise HTTPException(status_code=502, detail="printer request failed")


@router.post("/home")
def home(body: HomeBody, acc: accounts.Account = Depends(require_entitlement(ENT_MACHINE))):
    try:
        return mr.home(_safe_url(body.url), body.axes)
    except mr.MoonrakerError as exc:
        raise HTTPException(status_code=502, detail="printer request failed")


@router.post("/print")
def print_action(body: PrintBody, acc: accounts.Account = Depends(require_entitlement(ENT_MACHINE))):
    try:
        return mr.print_action(_safe_url(body.url), body.action, body.filename)
    except mr.MoonrakerError as exc:
        raise HTTPException(status_code=502, detail="printer request failed")


@router.post("/send-document")
def send_document(body: SendDocumentBody, acc: accounts.Account = Depends(require_entitlement(ENT_MACHINE))):
    doc = docs.get_document(body.doc_id)
    if doc is None or not docs.can_access(doc, acc.id):
        raise HTTPException(status_code=404, detail="Document not found")
    cam = doc.derived.cam
    if cam is None or not cam.gcode_url:
        raise HTTPException(status_code=400, detail="No G-code — run the CAM step first")

    try:
        _, b64 = cam.gcode_url.split(",", 1)
        gcode_text = base64.b64decode(b64).decode("utf-8")
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Bad G-code payload: {exc}")

    filename = f"{doc.name.replace(' ', '_')[:40] or 'part'}.nc"
    try:
        return mr.upload_gcode(_safe_url(body.url), filename, gcode_text, start=body.start)
    except mr.MoonrakerError as exc:
        raise HTTPException(status_code=502, detail="printer request failed")
