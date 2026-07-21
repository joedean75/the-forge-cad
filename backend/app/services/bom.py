"""Bill of Materials (roadmap feature 05).

An assembly's parts list — quantity, material, per-part and extended mass, and a
part number — rolled up from the placed components. Distinct parts are grouped
(four wheel instances → one line, qty 4). A CSV export is provided for handoff,
and an optional whole-assembly cost estimate ties into the quote material data.
"""

from __future__ import annotations

import csv
import io
from typing import Dict, List

from app.services import assembly_service, document_service, materials


def _part_number(doc_id: str, index: int) -> str:
    return f"P-{index:03d}-{doc_id[:6].upper()}"


def bill_of_materials(asm_id: str) -> Dict:
    """Grouped BOM lines for an assembly, with mass and material rollups."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)

    # Group component instances by the part document they reference.
    order: List[str] = []
    groups: Dict[str, Dict] = {}
    for c in asm.components:
        if c.part_id not in groups:
            order.append(c.part_id)
            groups[c.part_id] = {"qty": 0, "name": c.name, "missing": c.missing}
        groups[c.part_id]["qty"] += 1

    lines: List[Dict] = []
    total_mass = 0.0
    total_cost = 0.0
    for i, part_id in enumerate(order, start=1):
        g = groups[part_id]
        doc = document_service.get_document(part_id)
        unit_mass = 0.0
        material_name = "—"
        cost = 0.0
        if doc is not None:
            document_service.ensure_derived(doc)
            unit_mass = float(doc.derived.mass or 0.0)
            mat = materials.get_material(doc.material_id)
            material_name = mat.name
            cost = unit_mass * mat.cost_per_kg
        ext_mass = unit_mass * g["qty"]
        ext_cost = cost * g["qty"]
        total_mass += ext_mass
        total_cost += ext_cost
        lines.append({
            "item": i,
            "part_number": _part_number(part_id, i),
            "name": g["name"],
            "part_id": part_id,
            "quantity": g["qty"],
            "material": material_name,
            "unit_mass_g": round(unit_mass * 1000, 2),
            "ext_mass_g": round(ext_mass * 1000, 2),
            "ext_material_cost_usd": round(ext_cost, 2),
            "missing": g["missing"],
        })

    return {
        "assembly_id": asm_id,
        "assembly_name": asm.name,
        "line_count": len(lines),
        "component_count": len(asm.components),
        "total_mass_g": round(total_mass * 1000, 2),
        "total_material_cost_usd": round(total_cost, 2),
        "lines": lines,
    }


def bom_csv(asm_id: str) -> str:
    """The BOM as CSV (shop-floor / procurement handoff)."""
    data = bill_of_materials(asm_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Item", "Part Number", "Name", "Qty", "Material",
                "Unit Mass (g)", "Ext Mass (g)", "Ext Material Cost (USD)"])
    for ln in data["lines"]:
        w.writerow([ln["item"], ln["part_number"], ln["name"], ln["quantity"], ln["material"],
                    ln["unit_mass_g"], ln["ext_mass_g"], ln["ext_material_cost_usd"]])
    w.writerow([])
    w.writerow(["", "", "TOTAL", data["component_count"], "",
                "", data["total_mass_g"], data["total_material_cost_usd"]])
    return buf.getvalue()
