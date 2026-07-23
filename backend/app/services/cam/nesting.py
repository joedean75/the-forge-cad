"""Sheet nesting — pack copies of a cut profile onto a machine's sheet.

Sheet processes (fiber laser, plasma, waterjet) cut many parts from one plate,
so the job is only real once the parts are *nested*: laid out inside the
machine's sheet with edge margin and part-to-part spacing, then emitted as one
combined cut program.

This is a shelf/grid nest — parts are placed on rows of the part's own bounding
box. It is deterministic, respects the sheet envelope, and reports utilisation,
which is what the shop actually needs to know before cutting.
"""

from __future__ import annotations

from typing import Dict, List, Tuple

from app.services import machines
from app.services.cam import profile_cut as pc


def nest(doc_id: str, machine_id: str, quantity: int = 1, kerf_mm: float = 0.2,
         spacing_mm: float = 5.0, margin_mm: float = 10.0) -> Dict:
    """Nest ``quantity`` copies of the part's cut profile onto the machine sheet."""
    machine = machines.get_machine(machine_id)
    sheet_x, sheet_y = float(machine.bed[0]), float(machine.bed[1])
    if sheet_x <= 0 or sheet_y <= 0:
        raise ValueError(f"{machine.name} has no sheet area to nest on.")

    cut = pc.profile_cut(doc_id, kerf_mm=kerf_mm)
    paths: List[List[List[float]]] = cut["paths"]
    if not paths:
        raise ValueError("No cut profile to nest.")

    xs = [p[0] for path in paths for p in path]
    ys = [p[1] for path in paths for p in path]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    part_w, part_h = maxx - minx, maxy - miny
    pitch_x, pitch_y = part_w + spacing_mm, part_h + spacing_mm

    usable_x = sheet_x - 2 * margin_mm
    usable_y = sheet_y - 2 * margin_mm
    if part_w > usable_x or part_h > usable_y:
        raise ValueError(
            f"Part ({part_w:.0f}×{part_h:.0f} mm) does not fit {machine.name}'s "
            f"sheet ({sheet_x:.0f}×{sheet_y:.0f} mm) with {margin_mm:.0f} mm margin."
        )

    cols = max(int((usable_x + spacing_mm) // pitch_x), 1)
    rows = max(int((usable_y + spacing_mm) // pitch_y), 1)
    per_sheet = cols * rows
    qty = max(1, int(quantity))
    placed_this_sheet = min(qty, per_sheet)

    placements: List[Dict] = []
    for i in range(placed_this_sheet):
        r, c = divmod(i, cols)
        placements.append({
            "index": i + 1,
            "x": round(margin_mm + c * pitch_x - minx, 3),
            "y": round(margin_mm + r * pitch_y - miny, 3),
        })

    # One combined program: every placed instance, translated.
    moved: List[List[Tuple[float, float]]] = []
    for pl in placements:
        for path in paths:
            moved.append([(p[0] + pl["x"], p[1] + pl["y"]) for p in path])
    gcode, cut_len = pc._emit_gcode(moved, feed=1200.0, power=100.0, pierce_s=0.3, lead_in=2.0)

    part_area = part_w * part_h
    utilisation = (part_area * placed_this_sheet) / (sheet_x * sheet_y) * 100.0
    return {
        "document_id": doc_id,
        "machine": {"id": machine.id, "name": machine.name, "sheet_mm": [sheet_x, sheet_y]},
        "requested": qty,
        "per_sheet": per_sheet,
        "placed": placed_this_sheet,
        "sheets_needed": (qty + per_sheet - 1) // per_sheet,
        "grid": {"cols": cols, "rows": rows},
        "part_mm": [round(part_w, 2), round(part_h, 2)],
        "utilisation_pct": round(utilisation, 1),
        "placements": placements,
        "paths": [[[round(x, 2), round(y, 2)] for x, y in p] for p in moved],
        "cut_length_mm": round(cut_len, 1),
        "gcode": gcode,
    }
