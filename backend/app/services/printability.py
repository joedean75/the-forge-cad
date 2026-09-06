"""Whether a printed part will come off the bed as the part you drew.

`dfm_topology` already measures the body — wall thickness, bores, trapped
voids — against a *process*. What it cannot know is the machine: a 0.4 mm
nozzle cannot lay a 0.6 mm wall as two perimeters, a 260 mm chassis does not fit
a 220 mm bed, and a bearing pocket cut at the bearing's nominal diameter is a
press fit that will not press once the printer has shrunk it.

So this is the machine half, and it is deliberately separate from the process
half rather than bolted onto it: `dfm` answers "is this manufacturable", this
answers "on *that* printer, with *that* nozzle, using *those* screws".

**Every row here is a measurement of the body.** That constraint is what keeps
the report worth reading, and it is the same rule `design_spec` follows: a check
that cannot be measured fails loudly instead of passing quietly. Two things
people expect to find here are therefore *not* checks:

- **Nut traps and head clearances.** A nut trap is a hexagonal pocket, and
  finding one reliably in a B-rep means recognising six coplanar-ish planar
  faces in a ring — a heuristic that would be wrong on exactly the parts where
  it matters. What is real is the *dimension*: an M2.5 nut is 5 mm across flats,
  so the trap is 5 mm plus the fit allowance. That is arithmetic from a
  published table, so it is reported as a callout under `hardware`, clearly not
  as a pass or a fail on the body.
- **Print orientation.** Choosing it is a trade between strength, support and
  surface, and the tool cannot know which the user is optimising. What is
  measurable is whether the part *fits* in a given orientation, so that is what
  `bed_fit` answers, for every 90-degree rotation, and it names the ones that
  work rather than picking one.

**A bore is only identified, never renamed.** `bores` groups the body's
cylindrical faces by diameter and says what each *matches* — a clearance hole
for a declared screw, a catalogue bearing's outer diameter, a stock shaft — with
the tolerance it matched inside. It never asserts the designer meant that; two
holes at 5.0 mm might be a bearing bore and a cable pass-through, and the report
that guessed would be the one nobody trusted the second time.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from app.models.schemas import DfmCheck

MM = 1000.0

#: Published build volumes, as a convenience for the common machines. Any
#: three numbers work; these exist so a caller does not have to look one up, and
#: they are the *nominal* volume — a printer with a skirt or a purge tower has
#: less usable bed than this, which is what `margin_mm` is for.
BEDS: Dict[str, Tuple[float, float, float]] = {
    "ender-3": (220.0, 220.0, 250.0),
    "prusa-mk4": (250.0, 210.0, 220.0),
    "bambu-a1": (256.0, 256.0, 256.0),
    "bambu-x1c": (256.0, 256.0, 256.0),
    "voron-v0": (120.0, 120.0, 120.0),
}

#: Nut, head and socket dimensions are **not** restated here. `parts_catalog.
#: METRIC` already carries ISO 4762's head diameter and height, its socket
#: across flats and ISO 4032's nut across flats, because it builds the parts
#: from them — and two transcriptions of one table agree on the day they are
#: written. `hardware_callouts` reads that one.

#: How close a measured bore has to be to a named diameter before the report
#: says it "matches". Deliberately tight: a millimetre of slack would match an
#: MR105's 10 mm outer to a 623's, which are different parts.
_MATCH_TOL_MM = 0.15


class PrintabilityError(ValueError):
    """The body cannot be measured, so nothing here can be answered."""


#: What a manufacturing intent may say. Named here because the release
#: package's manifest and `check_printability` both read it, and two readers of
#: an unvalidated free-form dict is how a manifest comes to state a nozzle
#: nobody typed.
PROCESSES = ("additive", "milling", "molding", "casting")


def intent(*, process: str = "additive", printer: Optional[str] = None,
           nozzle_mm: Optional[float] = None, layer_mm: Optional[float] = None,
           bed_mm: Optional[dict] = None, infill_percent: Optional[float] = None,
           screws: Sequence[str] = (), clearance_mm: Optional[float] = None,
           notes: str = "") -> Dict[str, Any]:
    """Validate and normalise a manufacturing intent.

    Everything is optional except the process, because a part designed for
    milling has no nozzle and stating one would be worse than saying nothing.
    What is *given* is checked: a named printer must be one whose bed is known,
    or the bed has to be spelled out, because a manifest that names a machine it
    cannot size is a manifest whose fit claim nobody can check.
    """
    process = str(process or "").strip().lower()
    if process not in PROCESSES:
        raise PrintabilityError(
            f"unknown process {process!r}; one of {', '.join(PROCESSES)}")

    bed: Optional[Dict[str, float]] = None
    if bed_mm:
        try:
            bed = {a: float(bed_mm[a]) for a in "xyz"}
        except (KeyError, TypeError, ValueError):
            raise PrintabilityError("bed_mm needs x, y and z in millimetres") from None
        if min(bed.values()) <= 0:
            raise PrintabilityError("a bed dimension must be positive")
    elif printer:
        known = BEDS.get(str(printer).strip().lower())
        if known is None:
            raise PrintabilityError(
                f"no published bed here for {printer!r} — give bed_mm, or use "
                f"one of: {', '.join(sorted(BEDS))}")
        bed = dict(zip("xyz", (float(v) for v in known)))

    for name, value in (("nozzle_mm", nozzle_mm), ("layer_mm", layer_mm),
                        ("clearance_mm", clearance_mm)):
        if value is not None and float(value) <= 0:
            raise PrintabilityError(f"{name} must be positive")

    return {
        "process": process,
        "printer": str(printer).strip() if printer else None,
        "nozzle_mm": None if nozzle_mm is None else float(nozzle_mm),
        "layer_mm": None if layer_mm is None else float(layer_mm),
        "bed_mm": bed,
        "infill_percent": None if infill_percent is None else float(infill_percent),
        "screws": [str(s).strip().upper() for s in screws if str(s).strip()],
        "clearance_mm": None if clearance_mm is None else float(clearance_mm),
        "notes": str(notes or "").strip(),
    }


def settings_from(doc) -> Dict[str, Any]:
    """The printer settings to measure against: the document's, then defaults.

    A caller may still pass its own — `check_printability` takes the same
    arguments — but the point of recording an intent is that the check and the
    package agree without anybody re-typing the nozzle.
    """
    stored = getattr(doc, "manufacturing", None) or {}
    bed = stored.get("bed_mm") or {}
    return {
        "bed_mm": (float(bed.get("x", 220.0)), float(bed.get("y", 220.0)),
                   float(bed.get("z", 250.0))),
        "nozzle_mm": float(stored.get("nozzle_mm") or 0.4),
        "layer_mm": float(stored.get("layer_mm") or 0.2),
        "screws": tuple(stored.get("screws") or ()),
        "clearance_mm": float(stored.get("clearance_mm") or 0.2),
    }


def _bbox_mm(doc) -> Optional[Tuple[float, float, float]]:
    bbox = getattr(getattr(doc, "derived", None), "bbox", None)
    if bbox is None:
        return None
    return ((bbox.max.x - bbox.min.x) * MM,
            (bbox.max.y - bbox.min.y) * MM,
            (bbox.max.z - bbox.min.z) * MM)


def _orientations(size: Tuple[float, float, float]) -> List[Tuple[str, Tuple[float, float, float]]]:
    """The six axis-aligned ways a part sits on a bed, named by what points up.

    Only 90-degree rotations: an arbitrary rotation would fit more parts and
    would also be a print nobody can slice the same way twice, because the
    layer lines then run across the part at an angle the report never stated.
    """
    x, y, z = size
    return [
        ("+Z", (x, y, z)), ("-Z", (x, y, z)),
        ("+Y", (x, z, y)), ("-Y", (x, z, y)),
        ("+X", (z, y, x)), ("-X", (z, y, x)),
    ]


def bed_fit(size_mm, bed_mm, margin_mm: float = 5.0) -> Dict[str, Any]:
    """Which 90-degree orientations fit, and what to do when none does."""
    usable = tuple(max(0.0, b - 2 * margin_mm) for b in bed_mm[:2]) + (bed_mm[2],)
    fits: List[str] = []
    seen: set = set()
    for axis, (dx, dy, dz) in _orientations(size_mm):
        if (dx, dy, dz) in seen:
            continue
        seen.add((dx, dy, dz))
        if dx <= usable[0] and dy <= usable[1] and dz <= usable[2]:
            fits.append(axis)
        elif dy <= usable[0] and dx <= usable[1] and dz <= usable[2]:
            fits.append(f"{axis} (rotated 90° on the bed)")

    out: Dict[str, Any] = {"fits": fits, "usable_mm": {"x": round(usable[0], 2),
                                                       "y": round(usable[1], 2),
                                                       "z": round(usable[2], 2)},
                           "size_mm": {"x": round(size_mm[0], 2),
                                       "y": round(size_mm[1], 2),
                                       "z": round(size_mm[2], 2)}}
    if fits:
        return out

    # Splitting: the axis that overruns by the most is the one to cut across,
    # and the piece count is what it takes to bring that axis inside the bed.
    # The plane is named, not applied — where a part *should* split is a
    # decision about strength and joint design, and this only says how many
    # pieces the bed demands.
    overrun = [(size_mm[i] / usable[i] if usable[i] else math.inf, i)
               for i in range(3)]
    ratio, index = max(overrun)
    axis_name = "xyz"[index]
    pieces = max(2, math.ceil(ratio))
    out["split"] = {
        "axis": axis_name,
        "pieces": pieces,
        "piece_mm": round(size_mm[index] / pieces, 2),
        "note": (f"{size_mm[index]:.1f} mm along {axis_name} against "
                 f"{usable[index]:.1f} mm usable; {pieces} pieces cut across "
                 f"{axis_name} each fit. Where to cut is a joint-design "
                 f"decision, so it is named rather than made."),
    }
    return out


def _cylinder_faces(shape, samples) -> List[Dict[str, Any]]:
    """The body's cylindrical faces, grouped by diameter and split bore/boss.

    **Which side the material is on is the whole question.** A 10 mm cylinder is
    a bearing pocket if the metal is outside it and a 10 mm post if the metal is
    inside, and the first report written here called the outer surface of a tube
    an MR148 pocket. The face's own outward normal settles it: on a bore it
    points *at* the axis, because outward-of-material is inward-of-cylinder.

    That is read off the same surface normal `dfm_topology` samples — evaluated
    on the real surface rather than on the triangulation, which is the
    distinction that made those measurements trustworthy in the first place.
    """
    from app.cad_kernel.core import naming

    faces = naming.faces(shape)
    bounds = naming.body_bounds(shape)
    by_face: Dict[int, list] = {}
    for sample in samples:
        by_face.setdefault(sample.face_index, []).append(sample)

    grouped: Dict[tuple, Dict[str, Any]] = {}
    for index, face in enumerate(faces):
        descriptor = naming.face_descriptor(face, bounds)
        if descriptor.get("kind") != "cylinder" or not descriptor.get("radius_mm"):
            continue
        side = _side(face, by_face.get(index, ()))
        if side is None:
            continue
        diameter = round(float(descriptor["radius_mm"]) * 2.0, 3)
        row = grouped.setdefault((diameter, side),
                                 {"diameter_mm": diameter, "side": side,
                                  "faces": 0, "face_ids": []})
        row["faces"] += 1
        if len(row["face_ids"]) < 8:
            row["face_ids"].append(descriptor["id"])
    return [grouped[k] for k in sorted(grouped)]


def _side(face, samples) -> Optional[str]:
    """`bore` when the material is outside the cylinder, `boss` when inside."""
    import numpy as np
    from OCP.BRepAdaptor import BRepAdaptor_Surface

    if not samples:
        return None
    cylinder = BRepAdaptor_Surface(face).Cylinder()
    origin = cylinder.Axis().Location()
    direction = cylinder.Axis().Direction()
    axis_point = np.array([origin.X(), origin.Y(), origin.Z()])
    axis_dir = np.array([direction.X(), direction.Y(), direction.Z()])

    votes = 0
    for sample in samples:
        offset = sample.point - axis_point
        radial = offset - axis_dir * float(np.dot(offset, axis_dir))
        length = float(np.linalg.norm(radial))
        if length < 1e-9:
            continue
        votes += 1 if float(np.dot(sample.normal, radial / length)) < 0 else -1
    if votes == 0:
        return None
    return "bore" if votes > 0 else "boss"


def _matches(diameter: float, side: str, screws: Sequence[str]) -> List[Dict[str, Any]]:
    """What a measured cylinder could be, with the tolerance it matched inside.

    Every candidate is returned rather than the closest one: a report that
    picked would be asserting intent it cannot know, and two candidates at one
    diameter is itself worth seeing. A `boss` is matched only against shaft
    stock — a post is not a bearing pocket, whatever it measures.
    """
    from app.services import parts_catalog

    found: List[Dict[str, Any]] = []
    if side == "bore":
        for screw in screws:
            try:
                hole = parts_catalog.clearance_hole(screw)
            except Exception:  # noqa: BLE001 — an unknown screw is not a bore failure
                continue
            if abs(hole["diameter_mm"] - diameter) <= _MATCH_TOL_MM:
                found.append({"kind": "clearance_hole", "for": screw, "fit": "medium",
                              "nominal_mm": hole["diameter_mm"]})
            if abs(hole["counterbore_dia_mm"] - diameter) <= _MATCH_TOL_MM:
                found.append({"kind": "counterbore", "for": screw,
                              "nominal_mm": hole["counterbore_dia_mm"]})

    for table, label in ((parts_catalog.BEARINGS, "ISO 15"),
                         (parts_catalog.MINIATURE, "miniature metric (MR)")):
        for designation, bearing in table.items():
            if side == "bore" and abs(bearing.outer - diameter) <= _MATCH_TOL_MM:
                found.append({"kind": "bearing_pocket", "for": designation,
                              "standard": label, "nominal_mm": float(bearing.outer),
                              "bore_mm": float(bearing.bore),
                              "width_mm": float(bearing.width)})
            if abs(bearing.bore - diameter) <= _MATCH_TOL_MM:
                found.append({"kind": "shaft" if side == "boss" else "shaft_bore",
                              "for": designation, "standard": label,
                              "nominal_mm": float(bearing.bore)})
    return found


def hardware_callouts(screws: Sequence[str], clearance_mm: float) -> List[Dict[str, Any]]:
    """Trap and counterbore dimensions for the declared screws.

    Arithmetic from `parts_catalog.METRIC` — the same ISO 4762 and ISO 4032
    figures the catalog builds the screws and nuts from — not a measurement of
    the body, which is why these are callouts rather than checks. `clearance` is
    the caller's number because it is a property of their machine and their
    filament, not of the screw.
    """
    from app.services import parts_catalog

    rows: List[Dict[str, Any]] = []
    for screw in screws:
        size = str(screw).strip().upper()
        metric = parts_catalog.METRIC.get(size)
        if metric is None:
            rows.append({"screw": screw,
                         "unavailable": (f"no metric table here for {screw!r}; "
                                         f"known: "
                                         f"{', '.join(parts_catalog.METRIC)}")})
            continue
        hole = parts_catalog.clearance_hole(size)
        rows.append({
            "screw": size,
            "clearance_hole_mm": hole["diameter_mm"],
            "nut_across_flats_mm": metric.nut_flats,
            "nut_trap_across_flats_mm": round(metric.nut_flats + clearance_mm, 3),
            "nut_trap_depth_mm": round(metric.nut_height + clearance_mm, 3),
            "head_dia_mm": metric.head_dia,
            "head_counterbore_mm": round(metric.head_dia + clearance_mm, 3),
            "head_height_mm": metric.head_height,
            # An across-corners figure: a hex key has to *turn* in the recess,
            # so the across-flats dimension is the one that does not fit.
            "driver_clearance_mm": round(metric.socket * 2.0 / math.sqrt(3.0)
                                         + clearance_mm, 3),
            "note": (f"trap, depth and counterbore include your declared "
                     f"{clearance_mm:g} mm fit allowance; the driver clearance is "
                     f"the {metric.socket:g} mm key across corners"),
        })
    return rows


def check(doc_id: str, *, bed_mm: Sequence[float] = (220.0, 220.0, 250.0),
          nozzle_mm: float = 0.4, layer_mm: float = 0.2,
          screws: Sequence[str] = (), clearance_mm: float = 0.2,
          margin_mm: float = 5.0) -> Dict[str, Any]:
    """Measure a body against one printer's constraints."""
    from app.cad_kernel.evaluator import brep
    from app.services import document_service as docs, dfm_topology
    from app.services.op_errors import NoSuchDocument, PreconditionFailed

    doc = docs.get_document(doc_id)
    if doc is None:
        raise NoSuchDocument(f"no document {doc_id!r}")
    docs.ensure_derived(doc)

    size = _bbox_mm(doc)
    if size is None or min(size) <= 0:
        raise PreconditionFailed(
            "this document has no solid to measure; add a feature first")
    if nozzle_mm <= 0 or layer_mm <= 0:
        raise ValueError("nozzle and layer height must both be positive")

    rows: List[DfmCheck] = []

    fit = bed_fit(size, tuple(float(b) for b in bed_mm), margin_mm)
    if fit["fits"]:
        rows.append(DfmCheck(
            id="bed_fit", label="Fits the bed", status="pass",
            detail=(f"{size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f} mm; fits with "
                    f"{', '.join(fit['fits'])} up")))
    else:
        split = fit["split"]
        rows.append(DfmCheck(
            id="bed_fit", label="Fits the bed", status="fail",
            detail=(f"{size[0]:.1f}×{size[1]:.1f}×{size[2]:.1f} mm does not fit in "
                    f"any 90° orientation — {split['note']}"),
            value=float(split["pieces"])))

    # Two perimeters is the thinnest wall a slicer will lay as solid extrusions;
    # below it the wall becomes gap fill, which is neither the thickness drawn
    # nor a predictable strength.
    wall_limit_mm = 2.0 * nozzle_mm
    bores: List[Dict[str, Any]] = []
    if brep.AVAILABLE and (shape := docs.get_exact_shape(doc_id)) is not None:
        faces = dfm_topology._faces(shape)
        samples = dfm_topology._samples(shape, faces, 0.0005)
        rays = dfm_topology._Rays(shape)

        wall = dfm_topology.wall_thickness(shape, samples, rays)
        if wall["min"] is None:
            rows.append(DfmCheck(
                id="wall_vs_nozzle", label="Wall against the nozzle", status="pass",
                detail="no enclosed wall found — the body is solid"))
        else:
            measured = wall["min"] * MM
            rows.append(DfmCheck(
                id="wall_vs_nozzle", label="Wall against the nozzle",
                status="pass" if measured >= wall_limit_mm else "fail",
                detail=(f"{measured:.2f} mm thinnest wall against {wall_limit_mm:.2f} mm "
                        f"(two {nozzle_mm:g} mm perimeters)"),
                value=round(measured, 4)))

        small = dfm_topology.small_features(shape, faces)
        bore = small["min_bore_diameter_mm"]
        rows.append(DfmCheck(
            id="bore_vs_nozzle", label="Smallest bore against the nozzle",
            status="pass" if bore is None or bore >= wall_limit_mm else "fail",
            detail=("no cylindrical faces" if bore is None
                    else f"Ø{bore:.2f} mm against Ø{wall_limit_mm:.2f} mm"),
            value=None if bore is None else round(bore, 4)))

        voids = dfm_topology.trapped_voids(shape)
        rows.append(DfmCheck(
            id="trapped_void", label="Trapped internal void",
            status="pass" if voids == 0 else "fail",
            detail=("none" if voids == 0
                    else f"{voids} sealed cavity/cavities — support material "
                         f"cannot be removed from a cavity with no opening"),
            value=float(voids)))

        for row in _cylinder_faces(shape, samples):
            row["matches"] = _matches(row["diameter_mm"], row["side"], screws)
            bores.append(row)
        pockets = [b for b in bores
                   if any(m["kind"] == "bearing_pocket" for m in b["matches"])]
        if pockets:
            # By diameter, not by candidate: one 10 mm pocket matches three
            # MR bearings, and listing all three says nothing the diameter has
            # not already said while burying the sentence that matters.
            named = "; ".join(
                "Ø{:g} mm ({})".format(
                    b["diameter_mm"],
                    ", ".join(m["for"] for m in b["matches"]
                              if m["kind"] == "bearing_pocket"))
                for b in pockets)
            rows.append(DfmCheck(
                id="press_fit", label="Bearing pocket cut at nominal", status="warn",
                detail=(f"{named} — a pocket at the bearing's nominal outer "
                        f"diameter is a press fit before the printer's own "
                        f"shrinkage, which is machine- and filament-specific. "
                        f"Print a test pocket rather than trusting the nominal."),
                value=float(len(pockets))))
    else:
        # The rule the rest of this codebase follows: an unmeasurable check
        # fails loudly. A body with no exact form has no faces to measure a
        # wall or a bore on, and reporting "pass" for a question nobody asked
        # is the failure mode a printability report must not have.
        rows.append(DfmCheck(
            id="wall_vs_nozzle", label="Wall against the nozzle", status="fail",
            detail=("needs the exact kernel: without it the body has no faces "
                    "to measure a wall or a bore on (pip install cadquery-ocp)")))

    passed = sum(1 for r in rows if r.status == "pass")
    return {
        "printer": {"bed_mm": {"x": float(bed_mm[0]), "y": float(bed_mm[1]),
                               "z": float(bed_mm[2])},
                    "nozzle_mm": float(nozzle_mm), "layer_mm": float(layer_mm),
                    "margin_mm": float(margin_mm)},
        "size_mm": fit["size_mm"],
        "bed_fit": fit,
        "checks": [r.model_dump() for r in rows],
        "bores": bores,
        "hardware": hardware_callouts(screws, float(clearance_mm)),
        "score": round(100.0 * passed / len(rows)) if rows else 0,
        "note": ("every check is a measurement of the body; the hardware rows "
                 "are dimensions to design to, not findings about this part"),
    }
