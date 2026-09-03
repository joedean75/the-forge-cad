"""The whole vehicle's mass, centre of gravity and inertia.

A part's mass properties have been here for a long time (`mass_properties.py`).
What was missing is the *assembly's*, and for an RC airframe, a drone or a robot
that is the number that matters: all-up weight, where the CG sits relative to
the thrust line or the wheelbase, and the inertia tensor a flight controller or
a dynamics model is tuned against.

Three pieces of arithmetic, none of them optional:

* **Mass adds.** Trivially, and it is the only part the BOM already did.
* **The CG is the mass-weighted mean** of the component CGs *in assembly space*
  — so each part's own centre of mass has to be rotated and translated by its
  placement first. Using the component's bounding-box centre instead is the
  tempting shortcut and it is wrong for anything that is not symmetric, which
  is every bracket and every motor.
* **The inertia tensor rotates and shifts.** `R I Rᵀ` carries a part's tensor
  into assembly axes, and the parallel-axis theorem moves it from the part's own
  CG to the assembly's. Skipping the rotation gives a tensor that is right only
  when nothing is turned, which is exactly the case the mate connectors were
  added to stop being true.

**A purchased part weighs what the catalog says, not what its envelope does.**
A catalog part is a solid envelope, and for a motor — laminations, windings and
air — that envelope weighs nearly twice the motor. `mass_budget` is the number
an airframe is checked against, so it takes the published figure where there is
one and says so in each row's `mass_source`. What it does **not** do is scale
the inertia tensor by that ratio: a motor's mass sits in its stator and magnets
rather than being spread evenly through the bell, so the ratio is right for the
mass and wrong for the second moment. The tensors are reported as modelled and
a note says so — the honest correction needs a mass distribution the catalog
does not carry, and inventing one would be a plausible wrong tensor in place of
a known-approximate one.

**Scale is refused rather than approximated.** A component scaled by `s` has
`s³` the mass and `s⁵` the inertia, and the material density is a property of
the material rather than of the instance — so a scaled instance is a different
part that happens to look similar, and quietly weighing it as if it were the
original is worse than saying so.
"""

from __future__ import annotations

from typing import Any, Dict, List

import numpy as np

from app.services import (assembly_service, document_service, mass_properties,
                          standard_parts)

MM = 1000.0
G = 1000.0


def _rotation(component) -> np.ndarray:
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    r = component.transform.rotate_deg
    return _euler_matrix(r.x, r.y, r.z)


def _parallel_axis(mass: float, offset: np.ndarray) -> np.ndarray:
    """The tensor a point mass at `offset` contributes about the origin."""
    x, y, z = offset
    return mass * np.array([
        [y * y + z * z, -x * y, -x * z],
        [-x * y, x * x + z * z, -y * z],
        [-x * z, -y * z, x * x + y * y],
    ])


def assembly_mass_properties(asm_id: str) -> Dict[str, Any]:
    """All-up weight, CG and inertia for a whole assembly, in assembly space."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)

    rows: List[Dict[str, Any]] = []
    notes: List[str] = []
    total_mass = 0.0
    weighted_cg = np.zeros(3)
    inertia_origin = np.zeros((3, 3))

    for component in asm.components:
        if component.missing:
            notes.append(f"{component.id}: the part it references is gone — "
                         f"excluded from every figure below")
            continue
        scale = float(component.transform.scale or 1.0)
        if abs(scale - 1.0) > 1e-9:
            notes.append(
                f"{component.name or component.id}: placed at scale {scale:g} and "
                f"excluded. Mass goes as the cube of scale and inertia as the "
                f"fifth power, and density belongs to the material rather than "
                f"to the instance — a scaled copy is a different part.")
            continue
        try:
            part = mass_properties.mass_properties(component.part_id)
        except (KeyError, ValueError) as exc:
            notes.append(f"{component.name or component.id}: {exc}")
            continue

        mass = float(part["mass_kg"])
        modelled_mass = None
        mass_source = "geometry"
        doc = document_service.get_document(component.part_id)
        published = standard_parts.published_mass_kg(
            doc.catalog_ref if doc is not None else None)
        if published is not None:
            modelled_mass, mass = mass, published
            mass_source = "published"
        if mass <= 0:
            notes.append(f"{component.name or component.id}: zero mass — is a "
                         f"material assigned?")
            continue

        rotation = _rotation(component)
        translate = np.array([component.transform.translate.x,
                              component.transform.translate.y,
                              component.transform.translate.z])
        # The part's own CG, carried into assembly space by its placement.
        local_cg = np.asarray(part["center_of_mass_mm"], dtype=float) / MM
        cg = rotation @ local_cg + translate

        # kg·mm² back to SI, rotated into assembly axes, then shifted from the
        # part's CG to the assembly origin. Both steps, in that order.
        #
        # The part's own tensor is as modelled and is deliberately *not* scaled
        # by published/geometric — see the module docstring. The parallel-axis
        # term does take the reported mass, because it has to: the CG above and
        # the back-shift below are both weighted by it, and a shift computed
        # against one total and undone against another is not a tensor at all.
        local_inertia = np.asarray(part["inertia_kg_mm2"], dtype=float) / (MM ** 2)
        inertia = rotation @ local_inertia @ rotation.T
        inertia_origin += inertia + _parallel_axis(mass, cg)

        total_mass += mass
        weighted_cg += mass * cg
        rows.append({
            "component_id": component.id,
            "name": component.name,
            "mass_g": round(mass * G, 3),
            "center_of_mass_mm": [round(float(v) * MM, 4) for v in cg],
            "mass_source": mass_source,
            **({"modelled_mass_g": round(modelled_mass * G, 3)}
               if modelled_mass is not None else {}),
            "exact": bool(part.get("exact")),
        })

    substituted = sum(1 for r in rows if r["mass_source"] == "published")
    if substituted:
        notes.append(
            f"{substituted} of {len(rows)} components weigh what the catalog "
            f"publishes rather than what their envelope measures — each says "
            f"which in mass_source, with the modelled figure beside it. Their "
            f"own inertia tensors are as modelled and are not scaled by that "
            f"ratio: a motor's mass is in its stator and magnets rather than "
            f"spread through the bell, so the ratio is right for the mass and "
            f"wrong for the second moment. Correcting it needs a mass "
            f"distribution this catalog does not carry.")

    if total_mass <= 0:
        return {
            "mass_g": 0.0, "center_of_mass_mm": [0.0, 0.0, 0.0],
            "inertia_kg_mm2": [[0.0] * 3 for _ in range(3)],
            "components": rows, "counted": 0, "notes": notes or [
                "nothing in this assembly has mass — assign a material to the parts"],
        }

    cg = weighted_cg / total_mass
    # Parallel-axis, the other way: from the origin to the assembly's own CG.
    inertia_cg = inertia_origin - _parallel_axis(total_mass, cg)

    # What each part does to the balance. The heaviest part is not always the
    # one moving the CG — a light part on a long arm outvotes it.
    for row in rows:
        offset = np.asarray(row["center_of_mass_mm"], dtype=float) / MM - cg
        row["moment_g_mm"] = round(
            float(np.linalg.norm(offset)) * MM * row["mass_g"], 2)
    rows.sort(key=lambda r: r["moment_g_mm"], reverse=True)

    return {
        "mass_g": round(total_mass * G, 3),
        "center_of_mass_mm": [round(float(v) * MM, 4) for v in cg],
        # About the assembly CG, in kg·mm² — the same unit and reference the
        # part-level figure uses, so the two can be compared without a
        # conversion nobody remembers to do.
        "inertia_kg_mm2": [[round(float(inertia_cg[i, j]) * (MM ** 2), 4)
                            for j in range(3)] for i in range(3)],
        "components": rows,
        "counted": len(rows),
        "notes": notes,
    }
