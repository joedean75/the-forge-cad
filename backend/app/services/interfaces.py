"""What a part offers to the parts around it: its shaft, its mount and its can.

A purchased part imported from STEP arrives as one exact solid with no feature
history — the A2212 motor that stopped a live truck build is 5,362 faces, and
the copilot asked the user for "a dimensioned drawing" because nothing would
read the drawing out of the body it already had. This does, for the three
interfaces a rotating part is mounted by:

* **The main axis** is the cylinder direction carrying the most surface. A
  motor's bell, shaft, bearing seats and mounting holes all point along it;
  cooling-fin fillets and fastener threads do not add up to it.
* **The shaft** is the thinnest coaxial *boss* (material inside the cylinder)
  that reaches an end of the body, and its projection is measured from the tip
  to the nearest body material that is *not* the shaft — the dimension a
  coupler or a pinion is chosen by.
* **Mounting holes** are *bores* parallel to the axis but off it, grouped by
  diameter and by the circle they sit on, with every metric screw they could
  be: tapped, clearance, or modelled at nominal size. Every candidate is
  returned rather than the likeliest, the rule `printability` follows — a
  2.5 mm hole is an M3 tap drill and an M2 clearance at once.

What it will not do is guess what the part *is*. A KV rating, a turn count or a
manufacturer's name is not in the geometry, and a field that looked like one
would be an invention. Faces under `MIN_DIAMETER_MM` are ignored: a real STEP
motor carries thousands of 0.2 mm cylinders — winding strands, thread flanks —
that are noise for this question.

Coordinates are the kernel's metres in, millimetres out, like every reading an
agent is handed.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

import numpy as np

MM = 1000.0

#: Cylinders thinner than this are ignored — winding strands and thread flanks.
MIN_DIAMETER_MM = 1.0
#: Bores in this band, parallel to the axis and off it, are mounting-hole
#: candidates: M1.6 clearance up to M6 clearance.
HOLE_BAND_MM = (1.5, 6.6)
#: Two cylinder axes closer than this are the same line.
_COAXIAL_M = 0.1e-3
_PARALLEL = 0.999
#: Diameters within this are the same size (split faces, tolerance noise).
_SAME_DIAMETER_MM = 0.05


def _cylinders(shape) -> List[Dict[str, Any]]:
    """Every cylindrical face above the noise floor, measured exactly.

    Axis, radius and axial extent come from the surface and its UV bounds, not
    the mesh; which side the material is on comes from the face's own outward
    normal at its UV centre (orientation included), the question `printability`
    showed has to be asked explicitly — a 10 mm cylinder is a post or a pocket
    and nothing else about it says which.
    """
    from OCP.BRepAdaptor import BRepAdaptor_Surface
    from OCP.BRepGProp import BRepGProp_Face
    from OCP.BRepTools import BRepTools
    from OCP.GeomAbs import GeomAbs_SurfaceType
    from OCP.gp import gp_Pnt, gp_Vec

    from app.cad_kernel.core import naming

    rows = []
    for face in naming.faces(shape):
        surface = BRepAdaptor_Surface(face)
        if surface.GetType() != GeomAbs_SurfaceType.GeomAbs_Cylinder:
            continue
        cylinder = surface.Cylinder()
        radius = float(cylinder.Radius())
        if radius * 2 * MM < MIN_DIAMETER_MM:
            continue
        axis = cylinder.Axis()
        loc = np.array([axis.Location().X(), axis.Location().Y(), axis.Location().Z()])
        direction = np.array([axis.Direction().X(), axis.Direction().Y(), axis.Direction().Z()])
        umin, umax, vmin, vmax = BRepTools.UVBounds_s(face)
        u, v = (umin + umax) / 2.0, (vmin + vmax) / 2.0
        point, normal = gp_Pnt(), gp_Vec()
        BRepGProp_Face(face).Normal(u, v, point, normal)
        p = np.array([point.X(), point.Y(), point.Z()])
        n = np.array([normal.X(), normal.Y(), normal.Z()])
        radial = p - loc - direction * float(np.dot(p - loc, direction))
        length = float(np.linalg.norm(radial))
        if length < 1e-12 or float(np.linalg.norm(n)) < 1e-12:
            continue
        side = "bore" if float(np.dot(n, radial)) < 0 else "boss"
        rows.append({
            "radius": radius, "loc": loc, "dir": direction,
            "v": (float(vmin), float(vmax)), "side": side,
            "area": radius * abs(umax - umin) * abs(vmax - vmin),
        })
    return rows


def _dominant_axis(rows) -> Optional[np.ndarray]:
    """The direction carrying the most cylindrical surface, sign-normalised."""
    groups: List[list] = []
    for row in rows:
        d = row["dir"]
        for group in groups:
            if abs(float(np.dot(group[0], d))) >= _PARALLEL:
                group[1] += row["area"]
                break
        else:
            groups.append([d, row["area"]])
    if not groups:
        return None
    d = max(groups, key=lambda g: g[1])[0]
    # Deterministic sign: the largest component positive.
    return d if d[int(np.argmax(np.abs(d)))] > 0 else -d


def _line_distance(point, origin, axis) -> float:
    offset = point - origin
    return float(np.linalg.norm(offset - axis * float(np.dot(offset, axis))))


def _axial_range(row, origin, axis):
    """Where a coaxial/parallel face sits along the main axis, as (lo, hi) in m."""
    base = float(np.dot(row["loc"] - origin, axis))
    sign = 1.0 if float(np.dot(row["dir"], axis)) > 0 else -1.0
    a, b = base + sign * row["v"][0], base + sign * row["v"][1]
    return (min(a, b), max(a, b))


def _merge_by_diameter(rows, origin, axis) -> List[Dict[str, Any]]:
    groups: List[Dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: r["radius"]):
        dia = row["radius"] * 2 * MM
        lo, hi = _axial_range(row, origin, axis)
        for group in groups:
            if abs(group["diameter_mm"] - dia) <= _SAME_DIAMETER_MM:
                group["lo"], group["hi"] = min(group["lo"], lo), max(group["hi"], hi)
                group["area"] += row["area"]
                break
        else:
            groups.append({"diameter_mm": dia, "lo": lo, "hi": hi, "area": row["area"]})
    return groups


def screw_candidates(diameter_mm: float) -> List[Dict[str, Any]]:
    """Every metric screw a hole of this diameter could be for, and how.

    Read from `parts_catalog.METRIC` rather than restated: `tapped` is the ISO
    2306 tap drill (D - P), `clearance` the ISO 273 medium fit, `nominal` a
    thread modelled at its major diameter — which supplier STEP files often do.
    """
    from app.services import parts_catalog

    found = []
    for name, row in parts_catalog.METRIC.items():
        nominal = float(name[1:])
        for kind, size, tol in (("nominal", nominal, 0.12),
                                ("tapped", nominal - row.pitch, 0.12),
                                ("clearance", row.clearance, 0.15)):
            if abs(diameter_mm - size) <= tol:
                found.append({"screw": name, "as": kind, "expected_mm": round(size, 3),
                              "off_mm": round(diameter_mm - size, 3)})
    return sorted(found, key=lambda c: abs(c["off_mm"]))


def _in_plane_basis(axis):
    """Two axes across `axis`, chosen deterministically so angles do not flip."""
    seed = np.eye(3)[int(np.argmin(np.abs(axis)))]
    e1 = seed - axis * float(np.dot(seed, axis))
    e1 /= np.linalg.norm(e1)
    return e1, np.cross(axis, e1)


def _mount_holes(bores, origin, axis, body_lo, body_hi) -> List[Dict[str, Any]]:
    """Off-axis bores parallel to the axis, grouped into patterns."""
    e1, e2 = _in_plane_basis(axis)
    holes: List[Dict[str, Any]] = []
    for row in bores:
        dia = row["radius"] * 2 * MM
        if not (HOLE_BAND_MM[0] <= dia <= HOLE_BAND_MM[1]):
            continue
        offset = row["loc"] - origin
        radial = offset - axis * float(np.dot(offset, axis))
        if float(np.linalg.norm(radial)) < _COAXIAL_M:
            continue          # on the axis: a shaft bore, not a mount
        lo, hi = _axial_range(row, origin, axis)
        # One hole is every bore on one line. A supplier model draws a tapped
        # hole as its drill, its modelled thread and an entry chamfer — three
        # diameters in one place — and reporting those as three bolt patterns
        # is what the first reading of a real motor did.
        for hole in holes:
            if float(np.linalg.norm(hole["radial"] - radial)) < _COAXIAL_M:
                hole["lo"], hole["hi"] = min(hole["lo"], lo), max(hole["hi"], hi)
                if all(abs(d - dia) > _SAME_DIAMETER_MM for d in hole["diameters"]):
                    hole["diameters"].append(dia)
                break
        else:
            holes.append({"diameters": [dia], "radial": radial, "lo": lo, "hi": hi})
    for hole in holes:
        hole["diameters"].sort()
        hole["diameter_mm"] = hole["diameters"][0]

    middle = (body_lo + body_hi) / 2.0
    patterns: Dict[tuple, Dict[str, Any]] = {}
    for hole in holes:
        end = "+axis" if (hole["lo"] + hole["hi"]) / 2.0 > middle else "-axis"
        key = (round(hole["diameter_mm"] / 0.1), end)
        patterns.setdefault(key, {"end": end, "holes": []})["holes"].append(hole)

    out = []
    for (_, end), pattern in sorted(patterns.items()):
        members = pattern["holes"]
        dia = float(np.mean([h["diameter_mm"] for h in members]))
        circles: List[Dict[str, Any]] = []
        for hole in members:
            r = float(np.linalg.norm(hole["radial"]))
            angle = math.degrees(math.atan2(float(np.dot(hole["radial"], e2)),
                                            float(np.dot(hole["radial"], e1)))) % 360.0
            for circle in circles:
                if abs(circle["r"] - r) < 0.1e-3:
                    circle["angles"].append(angle)
                    break
            else:
                circles.append({"r": r, "angles": [angle]})
        out.append({
            "hole_diameter_mm": round(dia, 3),
            "steps_mm": sorted({round(d, 3) for h in members for d in h["diameters"][1:]}),
            "count": len(members),
            "end": end,
            "depth_mm": round(max(h["hi"] - h["lo"] for h in members) * MM, 3),
            "circles": [{"diameter_mm": round(c["r"] * 2 * MM, 3), "holes": len(c["angles"]),
                         "angles_deg": sorted(round(a, 1) for a in c["angles"])}
                        for c in sorted(circles, key=lambda c: c["r"])],
            "screw_candidates": screw_candidates(dia)[:4],
        })
    out.sort(key=lambda p: -p["count"])
    return out


def read_interfaces(shape, vertices: np.ndarray) -> Dict[str, Any]:
    """Shaft, mounting pattern and can of an exact body, in its own frame.

    `vertices` is the body's tessellation (metres): the axial extent of a body
    is not a property of its cylinders — a domed bell's top is a spline — so
    the ends are read off the mesh, which is exact to its deflection.
    """
    rows = _cylinders(shape)
    axis = _dominant_axis(rows)
    if axis is None:
        return {"axis": None,
                "note": f"no cylindrical faces of {MIN_DIAMETER_MM} mm or more — nothing "
                        "here is turned or bored, so there is no shaft or bolt circle to read"}

    parallel = [r for r in rows if abs(float(np.dot(r["dir"], axis))) >= _PARALLEL]
    # The reference line: the axis of the most surface in that direction.
    anchor = max(parallel, key=lambda r: r["area"])
    origin = anchor["loc"]
    coaxial = [r for r in parallel if _line_distance(r["loc"], origin, axis) < _COAXIAL_M]

    along = (vertices - origin) @ axis
    radial = np.linalg.norm((vertices - origin) - np.outer(along, axis), axis=1)
    body_lo, body_hi = float(along.min()), float(along.max())
    reach = 0.3e-3

    bosses = _merge_by_diameter([r for r in coaxial if r["side"] == "boss"], origin, axis)
    length = body_hi - body_lo
    shaft = None
    for group in bosses:                        # thinnest first
        rest = along[radial > group["diameter_mm"] / 2 / MM + reach]
        if not len(rest):
            continue
        # Signed: positive stands proud of the material around it, negative is
        # recessed below it — which is still where a coupler or pinion goes,
        # but it changes what fits.
        ends = [("+axis", group["hi"] - float(rest.max()), group["hi"]),
                ("-axis", float(rest.min()) - group["lo"], group["lo"])]
        ends.sort(key=lambda e: -e[1])
        # A shaft either stands proud of the body or runs through it. Standing
        # proud alone would miss the A2212 that motivated this, whose shaft
        # ends 0.5 mm below the bell top and 1.5 mm up inside the base recess;
        # running through alone would miss a catalog outrunner's 8 mm stub on
        # a 28 mm body. A short thin boss that does neither (a circlip land) is
        # not a shaft.
        if ends[0][1] <= reach and group["hi"] - group["lo"] < 0.4 * length:
            continue
        shaft = {
            "diameter_mm": round(group["diameter_mm"], 3),
            "fits": _shaft_fits(group["diameter_mm"]),
            "length_mm": round((group["hi"] - group["lo"]) * MM, 3),
            "ends": [{"end": name,
                      "projection_mm": round(proj * MM, 3),
                      "tip_mm": [round(float(x) * MM, 3) for x in origin + axis * at]}
                     for name, proj, at in ends],
            "note": ("projection is measured from the tip to the nearest material "
                     "around the shaft; negative means the tip sits below it"),
        }
        break

    can = max(bosses, key=lambda g: g["diameter_mm"]) if bosses else None
    holes = _mount_holes([r for r in parallel if r["side"] == "bore"],
                         origin, axis, body_lo, body_hi)

    return {
        "axis": {"direction": [round(float(x), 6) for x in axis],
                 "point_mm": [round(float(x) * MM, 3) for x in origin]},
        "length_along_axis_mm": round((body_hi - body_lo) * MM, 3),
        "shaft": shaft,
        "can": ({"diameter_mm": round(can["diameter_mm"], 3),
                 "length_mm": round((can["hi"] - can["lo"]) * MM, 3)}
                if can and (not shaft or can["diameter_mm"] > shaft["diameter_mm"]) else None),
        "mount_patterns": holes,
        "cylinders_read": len(rows),
        "not_in_geometry": ["KV rating", "winding / turns", "rated current",
                            "manufacturer part number"],
    }


def _shaft_fits(diameter_mm: float) -> List[str]:
    """Common bore sizes this shaft is one of, including the inch ones."""
    named = {3.0: "3 mm", 3.175: "1/8 in (3.175 mm)", 4.0: "4 mm", 5.0: "5 mm",
             6.0: "6 mm", 6.35: "1/4 in (6.35 mm)", 8.0: "8 mm", 2.0: "2 mm",
             2.3: "2.3 mm", 1.5: "1.5 mm"}
    return [label for size, label in sorted(named.items())
            if abs(size - diameter_mm) <= 0.06]


def placed(reading: Dict[str, Any], rotation: np.ndarray, scale: float,
           translation: np.ndarray) -> Dict[str, Any]:
    """The same reading carried into assembly space: p' = R (s p) + t."""
    def point(mm: Sequence[float]) -> List[float]:
        p = rotation @ (scale * np.asarray(mm, dtype=float) / MM) + translation
        return [round(float(x) * MM, 3) for x in p]

    out: Dict[str, Any] = {}
    if reading.get("axis"):
        d = rotation @ np.asarray(reading["axis"]["direction"], dtype=float)
        out["axis"] = {"direction": [round(float(x), 6) for x in d],
                       "point_mm": point(reading["axis"]["point_mm"])}
    if reading.get("shaft"):
        out["shaft_tips_mm"] = {e["end"]: point(e["tip_mm"]) for e in reading["shaft"]["ends"]}
    return out


def read_document(doc_id: str) -> Dict[str, Any]:
    """`read_interfaces` for a stored part; raises ValueError when it has no exact body."""
    from app.cad_kernel.kernel import Kernel
    from app.services import document_service

    body = document_service.get_exact_body(doc_id)
    if body is None:
        raise ValueError(
            "this part has no exact body, so its faces cannot be measured — interfaces "
            "are read from B-rep cylinders, which a voxelised mesh import does not have")
    mesh = Kernel.tessellate(document_service.get_solid(doc_id))
    reading = read_interfaces(body.shape, np.asarray(mesh.vertices, dtype=float))
    return with_facts(reading, document_service.get_document(doc_id))


def with_facts(reading: Dict[str, Any], doc) -> Dict[str, Any]:
    """Attach what has been recorded about the real part, and stop listing as
    unknown what a recorded fact already answers."""
    from app.services import part_facts

    facts = part_facts.compact(doc)
    if facts:
        reading["recorded_facts"] = facts
    if "not_in_geometry" in reading:
        reading["not_in_geometry"] = part_facts.unanswered(doc, reading["not_in_geometry"])
        if reading["not_in_geometry"]:
            reading["how_to_record"] = ("confirmed values go in record_part_facts "
                                        "(record_component_facts in an assembly)")
    return reading
