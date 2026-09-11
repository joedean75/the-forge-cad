"""Bill of Materials (roadmap feature 05).

An assembly's parts list — quantity, material, per-part and extended mass, and a
part number — rolled up from the placed components. Distinct parts are grouped
(four wheel instances → one line, qty 4). A CSV export is provided for handoff,
and an optional whole-assembly cost estimate ties into the quote material data.

**A purchased part's mass is the catalog's, not its envelope's.** A line whose
document came from the standard-parts catalog takes that entry's published mass
where it has one, because a motor modelled as a solid steel block weighs nearly
twice what the motor weighs — `part_mass.effective_mass` ranks the four possible
sources and is the only place that decision is made. Every line says which
figure it used in `mass_source` with the reason in `mass_basis`, and carries the
geometric one beside it when the two differ, so a total can be checked rather
than taken on trust. **Cost does not follow.** It keys off the geometric mass:
that is the stock the part is cut or printed from, and a purchased component's
price has nothing to do with the material it is nominally assigned.
"""

from __future__ import annotations

import csv
import io
from typing import Dict, List

from app.services import assembly_service, document_service, materials, part_mass


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
    facts: List[part_mass.MassFact] = []
    total_mass = 0.0
    total_cost = 0.0
    for i, part_id in enumerate(order, start=1):
        g = groups[part_id]
        doc = document_service.get_document(part_id)
        material_name = "—"
        cost = 0.0
        fact = part_mass.MassFact(0.0, part_mass.UNKNOWN,
                                  "the part this line references is gone", None)
        if doc is not None:
            document_service.ensure_derived(doc)
            # Not `doc.derived.mass`. That is the *geometric* mass, which for a
            # purchased part is the mass of a simplified envelope in whatever
            # material happens to be assigned — a NEMA 17 modelled as solid
            # steel weighs 669 g against a real 350 g, and a BOM total built
            # from those is wrong in the place an RC or drone build cares most.
            fact = part_mass.effective_mass(doc)
            mat = materials.get_material(doc.material_id)
            material_name = mat.name
            # Material cost still keys off the *geometric* mass where there is
            # one: it is the cost of the stock the part is cut or printed from,
            # and a purchased component's price has nothing to do with its
            # material at all.
            billable = part_mass.geometric_mass(doc)
            cost = (billable or 0.0) * mat.cost_per_kg
        facts.append(fact)
        unit_mass = fact.kg
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
            # Where the unit mass came from, on every line. A total is only
            # worth reading next to this: one envelope-derived motor among
            # forty printed parts is otherwise invisible.
            "mass_source": fact.source,
            "mass_basis": fact.basis,
            **({"geometric_mass_g": round(fact.geometric_kg * 1000, 2)}
               if fact.geometric_kg is not None else {}),
        })

    return {
        "assembly_id": asm_id,
        "assembly_name": asm.name,
        "line_count": len(lines),
        "component_count": len(asm.components),
        "total_mass_g": round(total_mass * 1000, 2),
        "total_material_cost_usd": round(total_cost, 2),
        "mass": part_mass.summarize(facts),
        "lines": lines,
    }


def bom_csv(asm_id: str) -> str:
    """The BOM as CSV (shop-floor / procurement handoff)."""
    data = bill_of_materials(asm_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    # `Mass Source` travels with the numbers: this file is read by somebody who
    # cannot ask where a figure came from, and a published mass sitting in a
    # column headed the same as a measured one is exactly the ambiguity the
    # field exists to remove.
    w.writerow(["Item", "Part Number", "Name", "Qty", "Material",
                "Unit Mass (g)", "Ext Mass (g)", "Ext Material Cost (USD)",
                "Mass Source"])
    for ln in data["lines"]:
        w.writerow([ln["item"], ln["part_number"], ln["name"], ln["quantity"], ln["material"],
                    ln["unit_mass_g"], ln["ext_mass_g"], ln["ext_material_cost_usd"],
                    ln["mass_source"]])
    w.writerow([])
    w.writerow(["", "", "TOTAL", data["component_count"], "",
                "", data["total_mass_g"], data["total_material_cost_usd"], ""])
    return buf.getvalue()
