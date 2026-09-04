"""Standard purchased parts — screws, nuts, washers, bearings, motors, extrusion.

Every real assembly needs screws, and until now there was no way to get one
except modelling it. This is the library, and the two decisions behind it are
worth stating before the tables.

**Where the geometry comes from.** Not from a supplier's CAD download. The
obvious source is McMaster-Carr, and it is not available to a catalog like this
one: their `robots.txt` disallows `/catalog/*` outright, and their Product
Information API is gated behind an approved account and a per-customer client
certificate issued by their eProcurement team, with subscription limits
explicitly there to protect the data. Scraping it would breach the first and
redistributing it would breach the second.

So the parts here are **generated from published dimensional standards** — ISO
4762, ISO 4032, ISO 7089, ISO 273, ISO 15, the NEMA frame sizes. A dimension in
a published standard is a fact, and a fact is not somebody's content. What each
part carries instead of a supplier's part number is its **designation**
(`ISO 4762 M4×16`, `608-2RS`, `NEMA 17`), which is the thing that actually gets
ordered, plus a search link. A part number we could not verify would be worse
than none: the designation is what a purchasing system reads, and inventing one
is inventing an order.

**Why a feature tree and not a mesh.** A catalog part arrives as an ordinary
editable tree, for the same reason a template does: an `ImportFeature` or a
`BrepBodyFeature` is an opaque body with no parameters, no history, and nothing
the decompiler can recover. A generated M4 screw can be measured, sectioned,
interfered against and re-exported like anything else in this system.

**Fidelity is declared, not implied.** A screw here has a plain cylindrical
shank at the nominal major diameter — no thread helix. That is what assembly
models normally carry and it is the right body for clearance, interference and
mass, but it is *not* the right body for a thread-engagement study, so every
part says which it is in `fidelity` and `notes` rather than leaving you to find
out by measuring.

**Where a domain has no standard, the number becomes a parameter.** The second
half of this file is the RC, drone and robotics catalog, and that domain has far
fewer published standards than the fastener one. The rule it runs on is that a
dimension is tabled only if it is standardised or a settled de-facto convention
— the flight-controller stack patterns, the four-digit brushless motor code, the
three RC servo classes — and everything that varies by manufacturer is a
*parameter* whose default is a typical figure and whose description says so.
Nothing is a number invented to fill a table: a LiPo pack, whose dimensions
genuinely differ between two packs of the same capacity, is parameterised
outright rather than tabled.

**Mass is a parameter wherever the envelope cannot know it.** A brushless motor
modelled as a solid cylinder of its stator's steel weighs three times the motor,
and a propeller modelled as its swept disc weighs ten times the propeller.
`typical_mass_g` carries the class figure — `bom` and `assembly_mass` read it
through `standard_parts.published_mass_kg` — and `mass_g` is a parameter
alongside it, because a mass that is typical for the class is not the mass of
the part in your hand.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, NamedTuple, Optional
from urllib.parse import quote_plus

MM = 0.001   # the kernel is metres; every table below is in millimetres


# ---------------------------------------------------------------- the tables --
#
# Values are the nominal dimensions from the named standard. Where a standard
# gives a range, the nominal (max material) figure is used, because these bodies
# are for fit and clearance rather than for tolerance analysis.

class Metric(NamedTuple):
    """One metric thread size, and everything the parts below need from it."""
    pitch: float          # coarse pitch, mm
    head_dia: float       # ISO 4762 socket head cap screw, dk
    head_height: float    # ISO 4762, k
    socket: float         # ISO 4762, hex socket across flats
    nut_flats: float      # ISO 4032 hex nut, s
    nut_height: float     # ISO 4032, m
    washer_od: float      # ISO 7089, d2
    washer_id: float      # ISO 7089, d1
    washer_thk: float     # ISO 7089, h
    clearance: float      # ISO 273 medium fit


#: Coarse-pitch metric sizes, M2 through M12. Beyond M12 the head proportions
#: change enough that the table would need its own rows rather than a formula.
METRIC: Dict[str, Metric] = {
    "M2":  Metric(0.40,  3.8,  2.0, 1.5,  4.0,  1.6,  5.0,  2.2, 0.3,  2.4),
    "M2.5": Metric(0.45, 4.5,  2.5, 2.0,  5.0,  2.0,  6.0,  2.7, 0.5,  2.9),
    "M3":  Metric(0.50,  5.5,  3.0, 2.5,  5.5,  2.4,  7.0,  3.2, 0.5,  3.4),
    "M4":  Metric(0.70,  7.0,  4.0, 3.0,  7.0,  3.2,  9.0,  4.3, 0.8,  4.5),
    "M5":  Metric(0.80,  8.5,  5.0, 4.0,  8.0,  4.7, 10.0,  5.3, 1.0,  5.5),
    "M6":  Metric(1.00, 10.0,  6.0, 5.0, 10.0,  5.2, 12.0,  6.4, 1.6,  6.6),
    "M8":  Metric(1.25, 13.0,  8.0, 6.0, 13.0,  6.8, 16.0,  8.4, 1.6,  9.0),
    "M10": Metric(1.50, 16.0, 10.0, 8.0, 16.0,  8.4, 20.0, 10.5, 2.0, 11.0),
    "M12": Metric(1.75, 18.0, 12.0, 10.0, 18.0, 10.8, 24.0, 13.0, 2.5, 13.5),
}

#: Preferred lengths (ISO 888) — what is actually stocked, so the picker offers
#: 16 mm and not 17 mm.
SCREW_LENGTHS = (4, 5, 6, 8, 10, 12, 16, 20, 25, 30, 35, 40, 45, 50, 60, 70, 80)


class Bearing(NamedTuple):
    bore: float      # d
    outer: float     # D
    width: float     # B


#: Deep-groove ball bearings, ISO 15 boundary dimensions. The envelope is what a
#: model needs — races and balls are inside it and nothing mates to them.
BEARINGS: Dict[str, Bearing] = {
    "623":  Bearing(3, 10, 4),
    "624":  Bearing(4, 13, 5),
    "625":  Bearing(5, 16, 5),
    "626":  Bearing(6, 19, 6),
    "608":  Bearing(8, 22, 7),
    "6000": Bearing(10, 26, 8),
    "6001": Bearing(12, 28, 8),
    "6002": Bearing(15, 32, 9),
    "6200": Bearing(10, 30, 9),
    "6201": Bearing(12, 32, 10),
    "6202": Bearing(15, 35, 11),
    "6203": Bearing(17, 40, 12),
    "6204": Bearing(20, 47, 14),
    "6802": Bearing(15, 24, 5),
    "6806": Bearing(30, 42, 7),
}


class Nema(NamedTuple):
    face: float          # frame face, square, mm
    bolt_circle: float   # mounting hole spacing (square pattern), mm
    bolt: str            # the screw those holes take
    pilot_dia: float     # locating boss diameter
    pilot_height: float
    shaft_dia: float
    shaft_length: float  # a typical single-shaft protrusion
    body: float          # a typical body length; varies by model, so a parameter
    mass_g: float        # published typical mass — see `typical_mass_g` below


#: NEMA stepper frame sizes. The frame face and the bolt pattern are the
#: standard's; body length is not standardised at all — it changes with the
#: winding stack — so the figure here is a common one and is a parameter.
NEMA: Dict[str, Nema] = {
    "NEMA 8":  Nema(20.3, 16.0, "M2",   15.0, 1.5, 4.0, 12.0, 30.0, 60),
    "NEMA 11": Nema(28.2, 23.0, "M2.5", 22.0, 2.0, 5.0, 20.0, 45.0, 140),
    "NEMA 14": Nema(35.2, 26.0, "M3",   22.0, 2.0, 5.0, 21.0, 34.0, 180),
    "NEMA 17": Nema(42.3, 31.0, "M3",   22.0, 2.0, 5.0, 24.0, 48.0, 350),
    "NEMA 23": Nema(56.4, 47.14, "M5",  38.1, 1.6, 6.35, 21.0, 56.0, 700),
    "NEMA 34": Nema(86.0, 69.6, "M5",   73.0, 2.0, 14.0, 32.0, 98.0, 2900),
}


class Extrusion(NamedTuple):
    width: float
    height: float
    slot: float          # slot opening width
    bore: float          # central bore diameter


#: T-slot aluminium extrusion, the sizes every maker project uses. The profile
#: here is the envelope plus the slot openings and the centre bore — enough to
#: mate to, to check clearance against and to cut to length. The internal
#: geometry of the slot is not modelled and the part says so.
EXTRUSIONS: Dict[str, Extrusion] = {
    "2020": Extrusion(20, 20, 6.2, 4.2),
    "2040": Extrusion(20, 40, 6.2, 4.2),
    "3030": Extrusion(30, 30, 8.2, 6.8),
    "4040": Extrusion(40, 40, 10.2, 10.0),
}


# ------------------------------------- the RC, drone and robotics families --
#
# Everything above comes from ISO or NEMA, where a dimension is published and a
# published dimension is a fact. This half is the domain the tool is actually
# used for — multirotors, RC vehicles, small robots — and it has far fewer
# formal standards. So every number below is sorted into one of three buckets,
# and the row says which bucket it is in:
#
# * **Standardised, or a settled de-facto convention** — tabled, with the
#   convention named. The stack patterns (30.5x30.5, 20x20, 16x16), the
#   four-digit motor code, the three servo classes. A part built to the wrong
#   one does not bolt on, which is what makes them conventions rather than
#   preferences.
# * **Varies by manufacturer** — a *parameter*, whose default is a typical
#   figure for the class and whose description says so. A motor's bell
#   diameter, a board's outline, a LiPo's envelope. The default is what lets
#   the part be placed without a datasheet in hand; the parameter is how it
#   becomes your part.
# * **Neither** — not here. Nothing below is a number invented to fill a table.
#
# **Mass is a parameter wherever the envelope cannot know it.** A brushless
# motor modelled as a solid cylinder of stator steel weighs three times the
# motor; a propeller modelled as its swept disc weighs ten times the prop. So
# `typical_mass_g` — which `bom` and `assembly_mass` read through
# `standard_parts.published_mass_kg` — carries the class figure, and `mass_g`
# is a parameter because a *typical* mass is not your part's: the pack in your
# hand has its weight printed on the side.


class MountPattern(NamedTuple):
    """A rectangular four-hole bolt pattern, and the screw it takes."""
    span_x: float
    span_y: float
    screw: str
    label: str


#: The bolt patterns this domain is built around. Each one is a convention
#: rather than a standard with a number, but it is a convention with teeth: a
#: 30.5 flight controller does not fit a 20 x 20 stack, and every board, ESC,
#: standoff and mount in the hobby is made to one of them. Tabling them is what
#: makes swapping a board a parameter change rather than four hand-placed holes.
MOUNT_PATTERNS: Dict[str, MountPattern] = {
    # The FC / ESC stack patterns, oldest first. 30.5 is the original full-size
    # stack, 20 the mini one, 16 the AIO / toothpick one.
    "fc-30.5": MountPattern(30.5, 30.5, "M3", "30.5 x 30.5 stack (M3)"),
    "fc-20": MountPattern(20.0, 20.0, "M2", "20 x 20 stack (M2)"),
    "fc-16": MountPattern(16.0, 16.0, "M2", "16 x 16 stack (M2)"),
    # Multirotor motor mounts, by size class. These follow the motor rather
    # than the frame — an arm is drilled for the motor it carries — and the
    # class boundaries are soft: a large motor is sold with 16 x 16 by one
    # maker and 19 x 19 by another, which is why every motor row names its
    # pattern and every motor's notes say to check it.
    "motor-12": MountPattern(12.0, 12.0, "M2", "12 x 12 motor mount (M2)"),
    "motor-16": MountPattern(16.0, 16.0, "M3", "16 x 16 motor mount (M3)"),
    "motor-19": MountPattern(19.0, 19.0, "M3", "19 x 19 motor mount (M3)"),
    "motor-25": MountPattern(25.0, 25.0, "M3", "25 x 25 motor mount (M3)"),
}


class Motor(NamedTuple):
    """An outrunner, by the four-digit code every one of them is sold under.

    **The code is the only thing here that is fixed**, and it is a convention
    rather than a standard: `2207` is a 22 mm stator diameter and a 7 mm stator
    height, and the stator is what sizes a motor — two 2207s from different
    makers are interchangeable in a way a "medium" motor never would be. The
    mount pattern is the de-facto one for the size class.

    Every other field is a **parameter default**: a typical figure for the
    class, not something anybody publishes for all of them. The bell diameter
    and the body height change with the maker's can design, and the mass
    changes with the winding — a high-KV 2207 and a low-KV one differ by more
    than a tenth. Set them from your motor's drawing when it matters.
    """
    pattern: str
    bell_dia: float      # parameter default
    height: float        # parameter default: base to the top of the bell
    shaft: float         # parameter default: the prop shaft
    mass_g: float        # parameter default


#: Six size classes spanning what this domain flies and drives: a 3-inch
#: motor, the three that carry a 5-inch quad, a 7-inch one and a heavy-lift /
#: gimbal frame size.
MOTORS: Dict[str, Motor] = {
    "1507": Motor("motor-12", 19.5, 16.0, 1.5, 16.0),
    "2207": Motor("motor-16", 27.8, 20.0, 5.0, 32.0),
    "2306": Motor("motor-16", 27.9, 19.5, 5.0, 30.0),
    "2807": Motor("motor-16", 34.5, 22.5, 5.0, 52.0),
    "3110": Motor("motor-19", 37.0, 26.0, 5.0, 80.0),
    "3508": Motor("motor-19", 42.0, 25.0, 4.0, 102.0),
}


class Board(NamedTuple):
    """A flight controller or a 4-in-1 ESC, which are the same body.

    The **pattern** is the tabled part and the only part that has to be right:
    it is what decides whether the board bolts into the stack. The outline, the
    stack height and the mass are parameter defaults — a 30.5 board is usually
    36 mm square and is sometimes 35 or 38, and none of that stops it fitting.
    """
    pattern: str
    width: float         # parameter default
    length: float        # parameter default
    height: float        # parameter default: PCB plus the tallest components
    mass_g: float        # parameter default


#: Flight controllers and 4-in-1 ESCs, one of each per stack pattern. An ESC
#: is the taller and heavier of the two — the capacitors and the FETs are on it.
BOARDS: Dict[str, Board] = {
    "fc-30.5": Board("fc-30.5", 36.0, 36.0, 8.0, 8.0),
    "fc-20": Board("fc-20", 26.0, 26.0, 7.0, 5.0),
    "fc-16": Board("fc-16", 21.0, 21.0, 6.0, 3.5),
    "esc-4in1-30.5": Board("fc-30.5", 38.0, 38.0, 9.0, 13.0),
    "esc-4in1-20": Board("fc-20", 28.0, 28.0, 8.0, 8.0),
    "esc-4in1-16": Board("fc-16", 22.0, 22.0, 7.0, 5.5),
}


class Servo(NamedTuple):
    """One of the three settled RC servo classes.

    Servos are the one part in this half of the catalog whose *body* is a
    convention rather than a parameter: a standard-size servo has fitted the
    same 40.5 x 20 pocket with the same 49.5 mm ear span since the 1970s, which
    is why a servo tray in any RC model takes any maker's servo. The figures
    here are the class's; an individual servo differs by a millimetre or two,
    so check a drawing before committing a mount you cannot re-cut.
    """
    body_l: float
    body_w: float
    body_h: float        # case, floor to the top of the gearbox
    overall_l: float     # across the mounting ears — the pocket it needs
    ear_span: float      # mounting hole centres along the length
    ear_cross: float     # hole centres across the width; 0 = one hole per ear
    ear_thk: float
    ear_z: float         # floor to the underside of the mounting ears
    hole_dia: float
    shaft_x: float       # output shaft centre, from the front face
    shaft_dia: float
    shaft_h: float       # output boss above the case
    screw: str           # what goes through the ears into your bracket
    mass_g: float


#: The three classes, as they are quoted: a standard servo is the 40.5 x 20 x
#: 36.5 body in a 54 mm envelope, a mini is two thirds of it and a micro is the
#: nine-gram one. `body_h` is the case alone and the output boss stands above
#: it, so the overall height is `body_h + shaft_h` — 36.5 mm for a standard
#: servo, which is the figure the class is quoted at.
SERVOS: Dict[str, Servo] = {
    "standard": Servo(40.5, 20.0, 32.0, 54.0, 49.5, 10.0, 2.5, 24.0, 4.1,
                      10.0, 5.8, 4.5, "M3", 40.0),
    "mini": Servo(32.5, 16.8, 26.5, 45.0, 41.0, 8.0, 2.3, 19.0, 3.0,
                  8.5, 4.6, 4.5, "M2.5", 22.0),
    "micro": Servo(22.8, 12.2, 22.7, 32.2, 28.0, 0.0, 2.5, 15.5, 2.2,
                   6.0, 4.6, 5.8, "M2", 9.0),
}


#: Hex standoff lengths, as stocked. The across-flats comes from `METRIC` —
#: an M3 standoff is a 5.5 mm hex because an M3 nut is, and there is no second
#: table to disagree with the first.
STANDOFF_SIZES = ("M2", "M2.5", "M3")
STANDOFF_LENGTHS = (5, 8, 10, 12, 15, 20, 25, 30, 35, 40)

#: Propeller diameters in inches, which is how every one of them is sold.
#: Nothing else about a propeller is tabled: pitch and blade count are part of
#: the designation rather than of this body, and the hub bore varies with what
#: it mounts to. The typical masses are for injection-moulded multirotor props
#: and are parameter defaults like every other mass here.
PROP_DIAMETERS_IN = (3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 10.0)
PROP_MASS_G = {3.0: 2.0, 4.0: 3.0, 5.0: 4.5, 6.0: 6.0,
               7.0: 8.0, 8.0: 10.0, 10.0: 14.0}


# ------------------------------------------------------------ bolt patterns --

class BoltPattern(NamedTuple):
    """A named set of hole placements, and the screw they take."""
    label: str
    screw: str
    holes: Tuple[Tuple[float, float], ...]   # mm, about the pattern's centre


_BOLT_PATTERNS: Optional[Dict[str, BoltPattern]] = None


def _corners(span_x: float, span_y: float) -> Tuple[Tuple[float, float], ...]:
    """A rectangular pattern's holes — two of them when one span is zero."""
    ys = (-span_y / 2, span_y / 2) if span_y else (0.0,)
    return tuple((sx * span_x / 2, y) for sx in (-1, 1) for y in ys)


def bolt_patterns() -> Dict[str, BoltPattern]:
    """Every named bolt pattern here, derived from the tables it already has.

    The stack and motor patterns come from `MOUNT_PATTERNS`, the stepper faces
    from `NEMA` and the servo ears from `SERVOS` — the same tables the parts
    themselves are built from. That is the whole reason this is a derivation
    rather than a fourth table: `pattern_holes` cuts these into the catalog's
    own parts and the `mounting_pattern` operation drills them into yours, so a
    bracket made for a NEMA 17 and a NEMA 17 have their holes in the same place
    by construction. A bolt circle transcribed twice is a bracket that does not
    fit, and it fits in CAD right up until it is made.

    A servo is why the placements are a *list* rather than two spans: its ears
    are 49.5 by 10 and the micro class has one hole per ear, which is an
    arrangement no pair of spans describes.
    """
    global _BOLT_PATTERNS
    if _BOLT_PATTERNS is not None:
        return _BOLT_PATTERNS
    out: Dict[str, BoltPattern] = {}
    for name, p in MOUNT_PATTERNS.items():
        out[name] = BoltPattern(p.label, p.screw, _corners(p.span_x, p.span_y))
    for frame, n in NEMA.items():
        out[frame.lower().replace(" ", "")] = BoltPattern(
            f"{frame} face ({n.bolt_circle:g} mm, {n.bolt})", n.bolt,
            _corners(n.bolt_circle, n.bolt_circle))
    for cls, s in SERVOS.items():
        out[f"servo-{cls}"] = BoltPattern(
            f"{cls} servo ears ({s.ear_span:g} mm)", s.screw,
            _corners(s.ear_span, s.ear_cross))
    _BOLT_PATTERNS = out
    return out


def _unit(v) -> Tuple[float, float, float]:
    length = math.sqrt(sum(c * c for c in v)) or 1.0
    return tuple(c / length for c in v)


def _cross(a, b) -> Tuple[float, float, float]:
    return (a[1] * b[2] - a[2] * b[1],
            a[2] * b[0] - a[0] * b[2],
            a[0] * b[1] - a[1] * b[0])


def pattern_placements(name: str, axis=(0.0, 0.0, 1.0)
                       ) -> List[Tuple[float, float, float]]:
    """Each hole of a named pattern as an x/y/z offset in millimetres.

    The offsets lie in the plane perpendicular to `axis`, and **which way round
    they lie has to be decided rather than left to whichever perpendicular came
    out first**: a servo's ears are 49.5 mm by 10, so a pattern that lands a
    quarter turn from where it did last time is a re-drill that moves the holes
    — worse than no re-drill at all. The reference is the world axis this one is
    least aligned with, which for +Z hands back x and y, and which changes
    continuously rather than flipping when a nearly-aligned axis is nudged.
    """
    n = _unit(axis)
    ref = [0.0, 0.0, 0.0]
    ref[min(range(3), key=lambda i: abs(n[i]))] = 1.0
    v = _unit(_cross(n, ref))
    u = _cross(v, n)
    return [tuple(u[i] * hx + v[i] * hy for i in range(3))
            for hx, hy in bolt_patterns()[name].holes]


# ------------------------------------------------------------- the geometry --

def _cyl(radius_mm: float, height_mm: float, z_mm: float,
         axis=(0.0, 0.0, 1.0), name: str = "Cylinder") -> dict:
    return {"type": "CylinderFeature", "name": name, "params": {
        "radius": radius_mm * MM,
        "height": height_mm * MM,
        "center": {"x": 0.0, "y": 0.0, "z": z_mm * MM},
        "axis": {"x": axis[0], "y": axis[1], "z": axis[2]},
    }}


def _box(x_mm: float, y_mm: float, z_mm: float, at=(0.0, 0.0, 0.0),
         name: str = "Box") -> dict:
    return {"type": "BoxFeature", "name": name, "params": {
        "size": {"x": x_mm * MM, "y": y_mm * MM, "z": z_mm * MM},
        "center": {"x": at[0] * MM, "y": at[1] * MM, "z": at[2] * MM},
    }}


def _union(children: List[dict], name: str = "Union") -> dict:
    return {"type": "BooleanFeature", "name": name,
            "params": {"operation": "union", "children": children}}


def _subtract(children: List[dict], name: str = "Cut") -> dict:
    return {"type": "BooleanFeature", "name": name,
            "params": {"operation": "subtract", "children": children}}


def _tree(root: dict) -> dict:
    return {"version": "brep-v1", "root": root}


def _hex_prism(across_flats_mm: float, height_mm: float, z_mm: float,
               name: str = "Hex") -> dict:
    """A hexagon, as the intersection of three crossed slabs.

    There is no hexagonal primitive and adding one for two parts would be a
    feature type to maintain forever. Three boxes at 60° intersect to exactly a
    regular hexagon of that width across flats — no approximation, and it stays
    an editable tree.
    """
    long_side = across_flats_mm * 2.0
    slabs = []
    for i, angle in enumerate((0.0, 60.0, 120.0)):
        slabs.append({"type": "TransformFeature",
                      "name": f"{name} slab {i + 1}",
                      "params": {"rotate": {"x": 0.0, "y": 0.0, "z": angle},
                                 "children": [_box(long_side, across_flats_mm,
                                                   height_mm, (0, 0, z_mm))]}})
    # The AABB of an intersection cannot be recovered from the children's own
    # AABBs once they are rotated — three 14x7 slabs at 0/60/120 degrees
    # intersect to a hexagon 7 across flats, and no combination of their boxes
    # says 7. So the node states its own extent for the listing to read. The
    # geometry is unaffected; this is a label, and `_extent` treats it as one.
    across_corners = across_flats_mm * 2.0 / math.sqrt(3.0)
    return {"type": "BooleanFeature", "name": name,
            "params": {"operation": "intersect", "children": slabs,
                       "envelope_mm": [across_corners, across_flats_mm, height_mm]}}


def socket_head_screw(size: str, length_mm: float) -> dict:
    """ISO 4762 socket head cap screw. Shank is plain — see `fidelity`."""
    m = METRIC[size]
    shank = float(size[1:])
    return _tree(_subtract([
        _union([
            _cyl(m.head_dia / 2, m.head_height, m.head_height / 2, name="Head"),
            _cyl(shank / 2, length_mm, -length_mm / 2, name="Shank"),
        ], name="Screw"),
        # The hex socket, sunk to 60% of the head — the standard's key
        # engagement depth, and the feature that makes it recognisable.
        _hex_prism(m.socket, m.head_height * 0.6,
                   m.head_height - m.head_height * 0.3, name="Socket"),
    ], name="Socket head cap screw"))


def hex_nut(size: str) -> dict:
    """ISO 4032 hex nut, style 1. The bore is the tapping drill, not a thread."""
    m = METRIC[size]
    return _tree(_subtract([
        _hex_prism(m.nut_flats, m.nut_height, m.nut_height / 2, name="Body"),
        _cyl((float(size[1:]) - m.pitch) / 2, m.nut_height * 3,
             m.nut_height / 2, name="Bore"),
    ], name=f"Hex nut {size}"))


def washer(size: str) -> dict:
    """ISO 7089 plain washer. Fully described by the standard — nothing omitted."""
    m = METRIC[size]
    return _tree(_subtract([
        _cyl(m.washer_od / 2, m.washer_thk, 0, name="Body"),
        _cyl(m.washer_id / 2, m.washer_thk * 3, 0, name="Bore"),
    ], name=f"Washer {size}"))


def dowel_pin(diameter_mm: float, length_mm: float) -> dict:
    """ISO 2338 parallel pin — a cylinder, and genuinely nothing more."""
    return _tree(_cyl(diameter_mm / 2, length_mm, 0, name="Dowel pin"))


def ball_bearing(designation: str) -> dict:
    """A deep-groove ball bearing as its ISO 15 boundary envelope.

    An annulus, not races and balls: nothing mates to the internals, the mass is
    within a few percent, and modelling the rolling elements would be a
    decorative body that slows every boolean it takes part in.
    """
    b = BEARINGS[designation]
    return _tree(_subtract([
        _cyl(b.outer / 2, b.width, 0, name="Outer race"),
        _cyl(b.bore / 2, b.width * 3, 0, name="Bore"),
    ], name=f"Bearing {designation}"))


def stepper_motor(frame: str, body_mm: Optional[float] = None) -> dict:
    """A NEMA stepper: frame body, pilot boss, output shaft, mounting holes.

    Everything a mount needs — the face, the bolt pattern, the pilot and the
    shaft — and nothing it does not. The connector and the wiring boss are
    omitted; they vary by manufacturer and are not part of the frame standard.
    """
    n = NEMA[frame]
    body = float(body_mm if body_mm else n.body)
    # Through `pattern_holes`, which reads the same `NEMA` row the frame does —
    # so a bracket drilled `nema17` and this motor put their holes in the same
    # place because it is one bolt circle, not two people typing 31 mm.
    holes = pattern_holes(frame.lower().replace(" ", ""), body * 1.2, -body / 2)

    return _tree(_subtract([
        _union([
            _box(n.face, n.face, body, (0, 0, -body / 2), name="Frame"),
            _cyl(n.pilot_dia / 2, n.pilot_height, n.pilot_height / 2, name="Pilot boss"),
            _cyl(n.shaft_dia / 2, n.shaft_length,
                 n.pilot_height + n.shaft_length / 2, name="Shaft"),
        ], name=f"{frame} stepper"),
        *holes,
    ], name=f"{frame} stepper"))


def tslot_extrusion(profile: str, length_mm: float) -> dict:
    """T-slot extrusion: the envelope, the four slot openings and the centre bore.

    Deliberately the *outside* of the profile rather than its full section. What
    an assembly does with extrusion is bolt things to the slots and check that
    the frame fits, and both of those need the envelope and the slot positions.
    The internal T of the slot is omitted and `fidelity` says so.
    """
    e = EXTRUSIONS[profile]
    cuts: List[dict] = [
        {"type": "CylinderFeature", "name": "Centre bore", "params": {
            "radius": (e.bore / 2) * MM, "height": length_mm * 3 * MM,
            "center": {"x": 0.0, "y": 0.0, "z": 0.0},
            "axis": {"x": 0.0, "y": 1.0, "z": 0.0}}},
    ]
    # A slot down the middle of each face. On a 2040 the long faces carry two,
    # which is what the second bay of the profile is.
    depth = e.slot
    for axis, span, other in (("x", e.width, e.height), ("z", e.height, e.width)):
        for sign in (-1, 1):
            bays = max(1, int(round(other / min(e.width, e.height))))
            for bay in range(bays):
                centre = (bay - (bays - 1) / 2) * min(e.width, e.height)
                at = {"x": 0.0, "y": 0.0, "z": 0.0}
                at[axis] = sign * (span / 2)
                at["z" if axis == "x" else "x"] = centre
                cuts.append(_box(
                    depth if axis == "x" else e.slot,
                    length_mm * 1.01,
                    e.slot if axis == "x" else depth,
                    (at["x"], 0.0, at["z"]), name="Slot"))
    return _tree(_subtract(
        [_box(e.width, length_mm, e.height, name="Profile"), *cuts],
        name=f"{profile} extrusion"))


def _cyl_at(radius_mm: float, height_mm: float, at=(0.0, 0.0, 0.0),
            axis=(0.0, 0.0, 1.0), name: str = "Cylinder") -> dict:
    """`_cyl` with an x and a y as well as a z.

    Everything above this point sits on the axis, so `_cyl` takes only a z. The
    stepper wrote its four mounting holes out as raw nodes for want of this;
    every part below has a bolt pattern.
    """
    node = _cyl(radius_mm, height_mm, at[2], axis=axis, name=name)
    node["params"]["center"]["x"] = at[0] * MM
    node["params"]["center"]["y"] = at[1] * MM
    return node


def pattern_holes(pattern: str, depth_mm: float, z_mm: float = 0.0,
                  at=(0.0, 0.0), *, diameter_mm: Optional[float] = None,
                  fit: str = "medium") -> List[dict]:
    """The holes of a named bolt pattern, as cutting tools down +Z.

    Placements from `bolt_patterns`, which is the same derivation the
    `mounting_pattern` operation drills from — so the holes in a part and the
    holes in the bracket somebody makes for it come from one list.

    The diameter comes from `clearance_hole`, the ISO 273 table `add_hole` and
    `clearance_hole` already read, unless `diameter_mm` overrides it. A servo's
    ears are the case for the override: 4.1 mm is the hole in the servo, and it
    is not the clearance for anything.
    """
    p = bolt_patterns()[pattern]
    radius = (float(diameter_mm) / 2 if diameter_mm is not None
              else clearance_hole(p.screw, fit)["diameter_mm"] / 2)
    return [_cyl_at(radius, depth_mm, (at[0] + hx, at[1] + hy, z_mm),
                    name="Mount hole")
            for hx, hy in p.holes]


#: How deep a motor's mounting screws reach into its base, and how far its prop
#: shaft stands above the bell. Both are part of the envelope rather than of
#: any table: a prop nut and a washer need about eight millimetres of shaft on
#: every motor in this catalog, and a mount screw that reaches further than
#: three has hit the windings.
_MOTOR_MOUNT_DEPTH = 3.0
_MOTOR_SHAFT_STICKOUT = 8.0


def brushless_motor(code: str, bell_dia_mm: Optional[float] = None,
                    height_mm: Optional[float] = None,
                    shaft_dia_mm: Optional[float] = None) -> dict:
    """An outrunner as its bell, its mounting face and its prop shaft.

    What a frame needs and nothing else: the can to check arm clearance
    against, the four mounting holes an arm is drilled for, and the shaft the
    propeller sits on. The stator, the magnets and the windings are inside the
    bell and nothing bolts to them — which is also why the body's own mass is
    meaningless and `mass_g` exists.
    """
    m = MOTORS[code]
    # `x if x is None else` rather than `x or`: a caller's explicit zero has to
    # reach the range check and be refused, not be silently replaced.
    bell = m.bell_dia if bell_dia_mm is None else float(bell_dia_mm)
    height = m.height if height_mm is None else float(height_mm)
    shaft = m.shaft if shaft_dia_mm is None else float(shaft_dia_mm)
    stickout = _MOTOR_SHAFT_STICKOUT

    body = _union([
        _cyl(bell / 2, height, height / 2, name="Bell"),
        _cyl(shaft / 2, stickout, height + stickout / 2, name="Prop shaft"),
    ], name=f"{code} motor")
    return _tree(_subtract(
        [body, *pattern_holes(m.pattern, _MOTOR_MOUNT_DEPTH * 1.2,
                              _MOTOR_MOUNT_DEPTH / 2)],
        name=f"{code} brushless motor"))


#: FR4 is 1.6 mm. It is the one dimension of a circuit board that really is
#: standard, and it is what a board sits on in a stack.
_PCB_THICKNESS = 1.6


def circuit_board(pattern: str, width_mm: float, length_mm: float,
                  height_mm: float) -> dict:
    """A flight controller or a 4-in-1 ESC: the PCB, its components, its holes.

    The components are one block rather than individual parts, inset clear of
    the mounting holes. That is not laziness about detail — the question a
    stack asks is how tall the board is, and a board that fits in CAD and not
    between the standoffs is the failure this envelope exists to catch.
    """
    p = MOUNT_PATTERNS[pattern]
    stack = max(0.1, float(height_mm) - _PCB_THICKNESS)
    body = _union([
        _box(width_mm, length_mm, _PCB_THICKNESS, (0, 0, _PCB_THICKNESS / 2),
             name="PCB"),
        _box(max(1.0, p.span_x - 8.0), max(1.0, p.span_y - 8.0), stack,
             (0, 0, _PCB_THICKNESS + stack / 2), name="Components"),
    ], name="Board")
    return _tree(_subtract(
        [body, *pattern_holes(pattern, _PCB_THICKNESS * 4, _PCB_THICKNESS / 2)],
        name="Board"))


def rc_servo(servo_class: str) -> dict:
    """An RC servo: the case, the mounting ears and the output boss.

    The output shaft is deliberately off-centre — it is what a linkage is laid
    out from, and a servo modelled with a centred output is one whose horn
    sweeps the wrong arc. The case is a plain box: the gearbox, the motor and
    the potentiometer are inside it and nothing mounts to them.
    """
    s = SERVOS[servo_class]
    # The ears run past the case at both ends, and how far is the class's
    # `overall_l` rather than an offset from the hole centres — the overall
    # length is what a servo tray is cut to and it is the figure the class is
    # quoted at, so it is the one that has to come out right.
    ear_len = s.overall_l
    body = _union([
        _box(s.body_l, s.body_w, s.body_h, (0, 0, s.body_h / 2), name="Case"),
        _box(ear_len, s.body_w, s.ear_thk, (0, 0, s.ear_z + s.ear_thk / 2),
             name="Mounting ears"),
        _cyl_at(s.shaft_dia / 2, s.shaft_h,
                (s.body_l / 2 - s.shaft_x, 0.0, s.body_h + s.shaft_h / 2),
                name="Output boss"),
    ], name=f"{servo_class} servo")

    # Four holes on the classes that have four, two on the class that has one
    # per ear — and from `bolt_patterns`, so the ears and a bracket drilled
    # `servo-standard` are the same four placements rather than two
    # transcriptions of them. The diameter is the servo's own: 4.1 mm is the
    # hole in the ear and it is not the clearance for anything.
    holes = pattern_holes(f"servo-{servo_class}", s.ear_thk * 4,
                          s.ear_z + s.ear_thk / 2, diameter_mm=s.hole_dia)
    return _tree(_subtract([body, *holes], name=f"{servo_class} servo"))


def hex_standoff(size: str, length_mm: float) -> dict:
    """A female-female hex standoff, bored through at the tapping drill.

    Across flats is the hex-nut figure from `METRIC` rather than a second
    table: an M3 standoff is a 5.5 mm hex because an M3 nut is, and two tables
    of the same number are two chances to be wrong about it. Threads are a
    callout, not geometry, exactly as they are for the nut.
    """
    m = METRIC[size]
    return _tree(_subtract([
        _hex_prism(m.nut_flats, length_mm, length_mm / 2, name="Body"),
        _cyl((float(size[1:]) - m.pitch) / 2, length_mm * 3, length_mm / 2,
             name="Bore"),
    ], name=f"{size} standoff {length_mm:g} mm"))


def propeller(diameter_in: float, bore_mm: float, hub_dia_mm: float,
              disc_thickness_mm: float) -> dict:
    """A propeller as the disc it sweeps, plus its hub.

    **Not two blades.** The question anyone asks of a propeller in an assembly
    is whether anything is inside its arc — an arm, a camera, the next
    propeller — and two blades frozen at one azimuth answer that only for the
    azimuth they happen to be at. `check_interference` against a blade is a
    clearance report that changes when nothing changed. The disc is the swept
    volume and it is the honest body; it also makes the mass geometrically
    absurd, which is why `mass_g` is a parameter.
    """
    diameter = float(diameter_in) * 25.4
    if bore_mm >= hub_dia_mm:
        raise ValueError(
            f"bore_mm ({bore_mm:g}) must be smaller than hub_dia_mm "
            f"({hub_dia_mm:g}) — a hub bored larger than itself is not a hub")
    hub_h = disc_thickness_mm * 2
    return _tree(_subtract([
        _union([
            _cyl(diameter / 2, disc_thickness_mm, 0, name="Swept disc"),
            _cyl(hub_dia_mm / 2, hub_h, 0, name="Hub"),
        ], name="Propeller"),
        _cyl(bore_mm / 2, hub_h * 3, 0, name="Bore"),
    ], name=f"{diameter_in:g} in propeller"))


def flat_plate(width_mm: float, length_mm: float, thickness_mm: float) -> dict:
    """Sheet stock, cut to size. A box, and genuinely nothing more.

    It is here rather than left to `add_primitive` for one reason: it arrives
    with the material already set to the laminate, which is what makes the mass
    on the BOM line the plate's real mass rather than aluminium's.
    """
    return _tree(_box(width_mm, length_mm, thickness_mm, name="Plate"))


def round_tube(od_mm: float, id_mm: float, length_mm: float) -> dict:
    """Tube stock — an arm, a boom, a leg."""
    if id_mm >= od_mm:
        raise ValueError(
            f"id_mm ({id_mm:g}) must be smaller than od_mm ({od_mm:g})")
    return _tree(_subtract([
        _cyl(od_mm / 2, length_mm, 0, axis=(0.0, 1.0, 0.0), name="Tube"),
        _cyl(id_mm / 2, length_mm * 3, 0, axis=(0.0, 1.0, 0.0), name="Bore"),
    ], name=f"Tube {od_mm:g}x{id_mm:g}"))


def battery_pack(length_mm: float, width_mm: float, height_mm: float) -> dict:
    """A LiPo pack as its envelope, which is all a pack ever is in a model.

    **Nothing about a pack is tabled and that is deliberate.** Two 4S 1300 mAh
    packs from different makers differ by ten millimetres and forty grams, so a
    table keyed on cell count and capacity would be a table of invented
    numbers — and the one place a wrong number here really bites is the mass
    budget, where the pack is the single heaviest thing in the aircraft. The
    envelope and the weight are both on the label; type them in.
    """
    return _tree(_box(length_mm, width_mm, height_mm, name="LiPo pack"))


# ------------------------------------------------------------- the listing --

class Entry(NamedTuple):
    key: str                     # stable id: "iso4762-m4-16"
    category: str
    designation: str             # what gets ordered
    standard: str
    material: str                # a catalog material id
    fidelity: str                # "as-standard" | "envelope"
    notes: str
    # (**params) -> feature tree. Reached through `build_tree`, never called
    # directly: a parameterised row's builder takes its dimensions as keyword
    # arguments and has no defaults of its own — those live on the descriptors
    # below, which is the only place they can also be published and checked.
    build: Any
    # Published mass, where the body's own geometry is known to mislead. A
    # stepper is laminations, windings and air, so a solid steel envelope of a
    # NEMA 17 weighs 669 g against a real 350 g. The *document* keeps reporting
    # its geometric mass, because that is what its geometry is; this says what
    # the real thing weighs so a BOM total is not quietly doubled.
    typical_mass_g: Optional[float] = None
    # What varies by manufacturer, where tabling it would be inventing it. Each
    # name maps to a descriptor built by `_p`; `build` takes them as keyword
    # arguments and `resolve_params` fills, validates and range-checks them.
    # `None` means the row has one configuration, which is true of everything
    # generated from an ISO table.
    params: Optional[Dict[str, Dict[str, Any]]] = None


def _search_url(designation: str) -> str:
    """A search for the designation, not a claimed part number.

    A supplier's part number is the thing an order is placed against, and none
    of these were verified against a supplier's data — inventing one would be
    inventing an order. A search on the designation is honest and lands in the
    right place.
    """
    return f"https://www.mcmaster.com/products/?q={quote_plus(designation)}"


_UNTHREADED = ("modelled with a plain shank at the nominal major diameter — "
               "correct for clearance, interference and mass, not for a thread "
               "engagement study")


def catalog() -> List[Entry]:
    """Every part this library can build, as a flat list."""
    out: List[Entry] = []
    for size, m in METRIC.items():
        for length in SCREW_LENGTHS:
            # Below one diameter of length a cap screw is a set screw, and above
            # ~10 diameters it is not a stocked cap screw either.
            shank = float(size[1:])
            if not (shank <= length <= shank * 12):
                continue
            out.append(Entry(
                key=f"iso4762-{size.lower()}-{length:g}",
                category="fastener",
                designation=f"ISO 4762 {size}×{length:g}",
                standard="ISO 4762", material="steel-4140", fidelity="envelope",
                notes=_UNTHREADED,
                build=(lambda s=size, l=length: socket_head_screw(s, l))))
        out.append(Entry(
            key=f"iso4032-{size.lower()}", category="fastener",
            designation=f"ISO 4032 hex nut {size}", standard="ISO 4032",
            material="steel-4140", fidelity="envelope", notes=_UNTHREADED,
            build=(lambda s=size: hex_nut(s))))
        out.append(Entry(
            key=f"iso7089-{size.lower()}", category="fastener",
            designation=f"ISO 7089 washer {size}", standard="ISO 7089",
            material="steel-4140", fidelity="as-standard",
            notes="every dimension the standard gives",
            build=(lambda s=size: washer(s))))

    for designation, b in BEARINGS.items():
        out.append(Entry(
            key=f"bearing-{designation}", category="bearing",
            designation=f"{designation} deep-groove ball bearing",
            standard="ISO 15", material="steel-4140", fidelity="envelope",
            notes=(f"boundary envelope {b.bore:g}×{b.outer:g}×{b.width:g} mm; "
                   "races and rolling elements are not modelled"),
            build=(lambda d=designation: ball_bearing(d))))

    for frame, n in NEMA.items():
        out.append(Entry(
            key=f"stepper-{frame.lower().replace(' ', '')}", category="motor",
            designation=f"{frame} stepper motor",
            standard="NEMA ICS 16 frame", material="steel-1018",
            fidelity="envelope",
            notes=(f"{n.face:g} mm face, {n.bolt_circle:g} mm bolt pattern "
                   f"({n.bolt}), {n.shaft_dia:g} mm shaft. Body length is not "
                   "standardised — it varies with the winding stack — so it is "
                   "a parameter. Connector and wiring boss omitted. Modelled as "
                   "a solid envelope, which over-states mass: a real motor is "
                   "laminations, windings and air."),
            build=(lambda f=frame: stepper_motor(f)),
            typical_mass_g=n.mass_g))

    for profile in EXTRUSIONS:
        for length in (100, 200, 300, 500):
            out.append(Entry(
                key=f"extrusion-{profile}-{length}", category="extrusion",
                designation=f"{profile} T-slot extrusion, {length} mm",
                standard="T-slot (20/30/40 series)", material="al-6061",
                fidelity="envelope",
                notes="envelope, slot openings and centre bore; the internal T "
                      "of each slot is not modelled",
                build=(lambda p=profile, l=length: tslot_extrusion(p, l))))

    for diameter in (2, 3, 4, 5, 6, 8, 10):
        for length in (6, 8, 10, 12, 16, 20, 25, 30):
            if length < diameter * 1.5:
                continue
            out.append(Entry(
                key=f"iso2338-{diameter}-{length}", category="fastener",
                designation=f"ISO 2338 dowel pin ⌀{diameter}×{length}",
                standard="ISO 2338", material="steel-4140",
                fidelity="as-standard", notes="m6 tolerance class; a plain cylinder",
                build=(lambda d=diameter, l=length: dowel_pin(d, l))))

    # ---- the RC, drone and robotics half ----------------------------------
    #
    # Same shape as the loops above; what differs is where the numbers come
    # from, which is why each `notes` says. Nothing below claims a standard it
    # does not have.

    for code, m in MOTORS.items():
        p = MOUNT_PATTERNS[m.pattern]
        out.append(Entry(
            key=f"bldc-{code}", category="motor",
            designation=f"{code} brushless motor",
            standard="stator code (4-digit convention)",
            # A can, a stator stack and magnets. The envelope is modelled as
            # the stator's steel, which is what makes its geometric mass three
            # times the motor's and `mass_g` mandatory.
            material="steel-1018", fidelity="envelope",
            notes=(f"{int(code[:2])} mm stator diameter x {int(code[2:])} mm "
                   f"stator height — the four-digit code is a convention every "
                   f"maker builds to and it is the one fixed dimension here. "
                   f"Mounts on {p.label}, which is the de-facto pattern for "
                   f"this size class; large motors are sold with more than one, "
                   f"so check yours. Bell diameter, body height, shaft and mass "
                   f"are parameters with typical defaults — the can design and "
                   f"the winding vary by maker. Modelled as a solid envelope: "
                   f"its geometric mass means nothing, which is what mass_g is "
                   f"for."),
            build=(lambda c=code, **kw: brushless_motor(c, **kw)),
            typical_mass_g=m.mass_g,
            params={
                "bell_dia_mm": _p(m.bell_dia, "outside diameter of the can",
                                  4.0, 200.0),
                "height_mm": _p(m.height, "body height, mounting face to the "
                                          "top of the can, excluding the shaft",
                                3.0, 200.0),
                "shaft_dia_mm": _p(m.shaft, "prop or output shaft diameter",
                                   0.5, 25.0),
                "mass_g": _p(m.mass_g, "what the motor weighs; the default is "
                                       "typical for the class and varies with "
                                       "the winding", 0.1, 20000.0),
            }))

    for key, b in BOARDS.items():
        p = MOUNT_PATTERNS[b.pattern]
        esc = key.startswith("esc")
        what = "4-in-1 ESC" if esc else "flight controller"
        out.append(Entry(
            key=key, category="electronics",
            designation=f"{what}, {p.label.split(' (')[0]}",
            standard=p.label,
            # FR4 is a glass laminate, which is what `gfrp` is.
            material="gfrp", fidelity="envelope",
            notes=(f"mounts on {p.label} — the pattern is the tabled part and "
                   f"the part that has to be right, because it decides whether "
                   f"the board bolts into the stack. Board outline, stack "
                   f"height and mass are parameters with typical defaults; "
                   f"they vary board to board and none of it stops one "
                   f"fitting. Components are one block, so the height is the "
                   f"stack height rather than a parts list."),
            build=(lambda k=key, **kw: circuit_board(BOARDS[k].pattern, **kw)),
            typical_mass_g=b.mass_g,
            params={
                "width_mm": _p(b.width, "board outline across", 5.0, 200.0),
                "length_mm": _p(b.length, "board outline along", 5.0, 200.0),
                "height_mm": _p(b.height, "PCB plus the tallest components — "
                                          "what has to fit between the "
                                          "standoffs", 1.7, 60.0),
                "mass_g": _p(b.mass_g, "what the board weighs", 0.1, 2000.0),
            }))

    for cls, s in SERVOS.items():
        out.append(Entry(
            key=f"servo-{cls}", category="motor",
            designation=f"{cls} RC servo",
            standard=f"{cls} servo class (de-facto)",
            material="abs", fidelity="envelope",
            notes=(f"{s.body_l:g} x {s.body_w:g} x "
                   f"{s.body_h + s.shaft_h:g} mm, {s.overall_l:g} mm across "
                   f"the ears, {s.ear_span:g} mm between the holes, output "
                   f"{s.shaft_x:g} mm from the front. The class is a de-facto "
                   f"convention rather than a standard — it is why any maker's "
                   f"standard servo drops into any servo tray — and an "
                   f"individual servo differs by a millimetre or two, so check "
                   f"a drawing before cutting a pocket you cannot re-cut. "
                   f"Gearbox, motor and pot are inside the case and not "
                   f"modelled, so the mass is the class's published figure."),
            build=(lambda c=cls: rc_servo(c)),
            typical_mass_g=s.mass_g))

    for size in STANDOFF_SIZES:
        m = METRIC[size]
        for length in STANDOFF_LENGTHS:
            out.append(Entry(
                key=f"standoff-{size.lower()}-{length}", category="hardware",
                designation=f"{size} hex standoff, {length} mm",
                standard=f"ISO 4032 hex across flats ({m.nut_flats:g} mm)",
                material="al-6061", fidelity="envelope",
                notes=(f"female-female, {m.nut_flats:g} mm across flats — the "
                       f"same figure the hex nut takes, from one table rather "
                       f"than two. Bored at the tapping drill; the thread is a "
                       f"callout, not geometry. Aluminium, so the body's own "
                       f"mass is the part's mass."),
                build=(lambda s=size, l=length: hex_standoff(s, l))))

    for diameter in PROP_DIAMETERS_IN:
        # Below 5 inches a propeller is shaft- or T-mounted on a 1.5 mm shaft;
        # at 5 and above it is an M5 prop shaft. Both are parameters; this is
        # only which default the row is listed with.
        bore = 5.0 if diameter >= 5.0 else 1.5
        mass = PROP_MASS_G[diameter]
        out.append(Entry(
            key=f"prop-{diameter:g}in", category="propeller",
            designation=f"{diameter:g} in propeller (swept disc)",
            standard="diameter x pitch designation",
            material="nylon-pa12", fidelity="envelope",
            notes=("modelled as the disc it sweeps, not as blades. The "
                   "question anyone asks of a propeller in an assembly is "
                   "whether anything is inside its arc, and two blades frozen "
                   "at one azimuth answer that only for that azimuth. Pitch "
                   "and blade count are part of the designation rather than of "
                   "this body. A solid disc weighs ten times the propeller, so "
                   "the mass is a parameter and its default is typical for an "
                   "injection-moulded multirotor prop of this size."),
            build=(lambda **kw: propeller(**kw)),
            typical_mass_g=mass,
            params={
                "diameter_in": _p(diameter, "swept diameter", 0.5, 60.0),
                "bore_mm": _p(bore, "hub bore — the motor shaft it goes on",
                              0.5, 30.0),
                "hub_dia_mm": _p(bore + 5.0, "hub outside diameter",
                                 1.0, 60.0),
                "disc_thickness_mm": _p(4.0, "axial band the blades sweep",
                                        0.2, 40.0),
                "mass_g": _p(mass, "what the propeller weighs", 0.05, 2000.0),
            }))

    out.append(Entry(
        key="carbon-plate", category="stock",
        designation="carbon fibre plate",
        standard="stock sheet", material="cfrp", fidelity="envelope",
        notes=("plain woven laminate, cut to size. Entirely parameterised — "
               "sheet stock has no dimensions to table — and it is here rather "
               "than left to a box primitive because it arrives with the "
               "laminate's density already set, which is what makes its BOM "
               "mass the plate's rather than aluminium's."),
        build=(lambda **kw: flat_plate(**kw)),
        params={
            "width_mm": _p(100.0, "across", 1.0, 3000.0),
            "length_mm": _p(200.0, "along", 1.0, 3000.0),
            "thickness_mm": _p(2.0, "sheet thickness", 0.1, 100.0),
        }))

    out.append(Entry(
        key="carbon-tube", category="stock",
        designation="carbon fibre tube",
        standard="stock tube", material="cfrp", fidelity="envelope",
        notes=("pultruded round tube — an arm, a boom, a leg. Entirely "
               "parameterised for the same reason the plate is."),
        build=(lambda **kw: round_tube(**kw)),
        params={
            "od_mm": _p(16.0, "outside diameter", 1.0, 500.0),
            "id_mm": _p(14.0, "bore", 0.5, 500.0),
            "length_mm": _p(300.0, "cut length", 1.0, 6000.0),
        }))

    out.append(Entry(
        key="lipo", category="battery",
        designation="LiPo pack",
        standard="none — packs are not standardised",
        # The envelope's material is a placeholder: the mass that reaches the
        # BOM and the mass budget is `mass_g`, because a pack's weight is on
        # its label and no density recovers it from a box.
        material="abs", fidelity="envelope",
        notes=("the envelope and the weight, both of which are printed on the "
               "pack. Nothing about a LiPo is tabled and that is deliberate: "
               "two 4S 1300 mAh packs from different makers differ by ten "
               "millimetres and forty grams, so a table keyed on cell count "
               "and capacity would be a table of invented numbers — and the "
               "pack is the single heaviest thing in most of these aircraft, "
               "so a wrong number here lands squarely on the mass budget. The "
               "defaults are one common 4S 1300 mAh pack; measure yours."),
        build=(lambda **kw: battery_pack(**kw)),
        typical_mass_g=165.0,
        params={
            "length_mm": _p(72.0, "pack length", 5.0, 600.0),
            "width_mm": _p(35.0, "pack width", 5.0, 400.0),
            "height_mm": _p(30.0, "pack height", 3.0, 300.0),
            "mass_g": _p(165.0, "what the pack weighs — it is on the label",
                         1.0, 50000.0),
        }))

    return out


_BY_KEY: Optional[Dict[str, Entry]] = None


def by_key(key: str) -> Optional[Entry]:
    global _BY_KEY
    if _BY_KEY is None:
        _BY_KEY = {e.key: e for e in catalog()}
    return _BY_KEY.get(key)


# ------------------------------------------------------------- parameters --
#
# Half of this domain has no standard to table, and the rule the catalog runs
# on is that a number nobody published does not get tabled — it becomes a
# parameter with a typical default and a description that says so. This is the
# machinery that makes a parameterised row placeable.
#
# **The one parameter that is not geometry is `mass_g`.** Every other parameter
# is a dimension handed to the builder. Mass is what the thing weighs, which no
# envelope can know, so it is resolved, recorded on the document's
# `catalog_ref`, and read back by `bom` and `assembly_mass` — never passed to a
# builder.

MASS_PARAM = "mass_g"

#: A parameter's unit lives in its *name*, the way `feed_mm_min` and `kerf_mm`
#: already do elsewhere here. Reading it back out is what lets one generic
#: function label every parameterised part without a per-family format string.
_UNIT_BY_SUFFIX = (("_mm", "mm"), ("_in", "in"), ("_g", "g"))


def _p(default: float, description: str, lo: float = 0.0,
       hi: Optional[float] = None) -> Dict[str, Any]:
    """One parameter descriptor. The unit is read off the name."""
    return {"default": float(default), "description": description,
            "min": float(lo), "max": None if hi is None else float(hi)}


def _split_unit(name: str):
    for suffix, unit in _UNIT_BY_SUFFIX:
        if name.endswith(suffix):
            return name[:-len(suffix)].replace("_", " "), unit
    return name.replace("_", " "), ""


def param_unit(name: str) -> str:
    """The unit a parameter name declares, or `""` for a pure count.

    Named apart from `services.units.unit_of`, which answers the same-shaped
    question about an *operation's* parameters from a central map. These names
    are the catalog's own and carry their unit in the name, the way `kerf_mm`
    and `feed_mm_min` already do on the operation surface.
    """
    return _split_unit(name)[1]


def resolve_params(entry: "Entry", given: Optional[Dict[str, Any]] = None
                   ) -> Dict[str, float]:
    """Validate a caller's parameters against the entry's, and fill the rest.

    Raises `ValueError` — the operation layer turns that into a typed
    `BAD_ARG`. Refusing an unknown name rather than dropping it is the whole
    point: a parameter silently ignored is a part built to a size nobody asked
    for and reported as a success, which is the exact failure `op_schema`'s
    `additionalProperties: false` exists to prevent everywhere else.
    """
    declared = entry.params or {}
    given = dict(given or {})
    unknown = sorted(set(given) - set(declared))
    if unknown:
        known = ", ".join(sorted(declared)) or "no parameters"
        raise ValueError(
            f"{entry.key} takes {known}; {', '.join(unknown)} "
            f"{'is' if len(unknown) == 1 else 'are'} not among them")

    out: Dict[str, float] = {}
    for name, spec in declared.items():
        # `given.get(name, default)` and not `given.get(name) or default`: an
        # explicit zero has to reach the range check below and be refused by
        # name, not be quietly replaced by the default.
        raw = given.get(name, spec["default"])
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{name} must be a number, not {raw!r}") from None
        lo, hi = float(spec.get("min") or 0.0), spec.get("max")
        if not (value > 0 and value >= lo and (hi is None or value <= hi)):
            bound = f"over {lo:g}" if hi is None else f"between {lo:g} and {hi:g}"
            raise ValueError(
                f"{name} must be {bound} {param_unit(name)}".rstrip()
                + f" — got {value:g}")
        out[name] = value
    return out


def build_tree(entry: "Entry", params: Optional[Dict[str, Any]] = None) -> dict:
    """The feature tree for one configuration of an entry."""
    resolved = resolve_params(entry, params)
    return entry.build(**{name: value for name, value in resolved.items()
                          if name != MASS_PARAM})


def designation_of(entry: "Entry", params: Optional[Dict[str, Any]] = None) -> str:
    """What this configuration is called, on the document and on the BOM line.

    The row's own designation when the configuration is the default one, and
    the same with the changed parameters appended when it is not — because two
    LiPo packs that are different parts must not print the same name on two
    BOM lines. Generic rather than a format string per family: a per-family
    label is one more thing to forget when a family is added.
    """
    resolved = resolve_params(entry, params)
    declared = entry.params or {}
    changed = []
    for name, value in resolved.items():
        if abs(value - float(declared[name]["default"])) < 1e-9:
            continue
        label, unit = _split_unit(name)
        changed.append(f"{label} {value:g}{' ' + unit if unit else ''}")
    return f"{entry.designation} ({', '.join(changed)})" if changed \
        else entry.designation


def make_ref(key: str, params: Optional[Dict[str, Any]] = None) -> str:
    """The `catalog_ref` a document built from this configuration carries.

    **The bare key when the configuration is the default one.** That is what
    keeps every document stored before parameters existed resolvable, and what
    keeps the common case — eight identical screws, four identical motors —
    one document and therefore one BOM line of qty 8.

    A non-default configuration appends its parameters, sorted and formatted
    canonically, so that asking twice for the same pack returns the same
    document and asking for a different one returns a different document.
    """
    entry = by_key(key)
    if entry is None or not entry.params:
        return key
    resolved = resolve_params(entry, params)
    changed = {name: value for name, value in resolved.items()
               if abs(value - float(entry.params[name]["default"])) >= 1e-9}
    if not changed:
        return key
    return key + "?" + "&".join(f"{n}={changed[n]:g}" for n in sorted(changed))


def parse_ref(ref: Optional[str]):
    """A `catalog_ref` split back into its key and its parameters."""
    key, _, query = str(ref or "").partition("?")
    params: Dict[str, float] = {}
    for bit in query.split("&"):
        name, _, value = bit.partition("=")
        if not name:
            continue
        try:
            params[name] = float(value)
        except ValueError:
            # A reference nobody here wrote. Dropping the parameter falls back
            # to the default, which is a listed configuration of a real part
            # rather than a refusal on a document somebody has open.
            continue
    return key, params


def published_mass_g(ref: Optional[str]) -> Optional[float]:
    """The mass this catalog publishes for a reference, or `None`.

    A part whose mass is a parameter answers with the configured value — the
    weight on the label beats a typical for the class, which is the whole
    reason the parameter is there. Everything else answers with its entry's
    `typical_mass_g`, and a part whose geometry tells the truth answers
    `None` so the roll-ups keep measuring it.
    """
    key, given = parse_ref(ref)
    entry = by_key(key)
    if entry is None:
        return None
    if entry.params and MASS_PARAM in entry.params:
        try:
            return float(resolve_params(entry, given)[MASS_PARAM])
        except ValueError:
            return float(entry.params[MASS_PARAM]["default"])
    return entry.typical_mass_g


def describe(entry: Entry) -> Dict[str, Any]:
    """One catalog row, as the API returns it."""
    return {
        "key": entry.key,
        "category": entry.category,
        "designation": entry.designation,
        "standard": entry.standard,
        "material_id": entry.material,
        "fidelity": entry.fidelity,
        # Always present, on every row of the listing, alongside the supplier
        # parts that share it. Everything here is a tree of primitives, so it
        # builds exactly on the B-rep kernel — an imported STL does not, and a
        # field that appears on only some rows is one a caller reads wrong.
        "geometry": "exact",
        "notes": entry.notes,
        "size_mm": envelope_mm(entry),
        **({"typical_mass_g": entry.typical_mass_g}
           if entry.typical_mass_g is not None else {}),
        # What this row can be configured with, published so a caller knows
        # before it calls — the same reason `GET /api/operations` publishes the
        # subject an operation takes. A row without this key has exactly one
        # configuration; `insert_standard_part` refuses a parameter a row does
        # not declare rather than dropping it.
        **({"params": {name: {"default": spec["default"],
                              "unit": param_unit(name),
                              "min": spec.get("min"), "max": spec.get("max"),
                              "description": spec["description"]}
                       for name, spec in entry.params.items()}}
           if entry.params else {}),
        # A search, not a part number — see `_search_url`.
        "supplier_search": _search_url(entry.designation),
    }


def categories() -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for entry in catalog():
        counts[entry.category] = counts.get(entry.category, 0) + 1
    return counts


def clearance_hole(size: str, fit: str = "medium") -> Dict[str, float]:
    """The hole a screw of this size goes through, per ISO 273.

    Fine/medium/coarse are the standard's three series. Medium is the default
    because it is what a general assembly uses and what every drill index has.
    """
    m = METRIC[size]
    shank = float(size[1:])
    # ISO 273 fine is roughly the shank plus a tenth of the step to medium;
    # coarse is roughly twice the medium clearance. Both derived from the
    # tabulated medium figure rather than a second table nobody would check.
    gap = m.clearance - shank
    table = {"fine": shank + gap * 0.5, "medium": m.clearance,
             "coarse": shank + gap * 2.0}
    if fit not in table:
        raise KeyError(fit)
    return {"diameter_mm": round(table[fit], 2),
            "radius_m": round(table[fit] * MM / 2, 8),
            "screw": size, "fit": fit,
            "counterbore_dia_mm": round(m.head_dia + 0.5, 2),
            "counterbore_depth_mm": m.head_height}


def envelope_mm(entry: Entry) -> Dict[str, float]:
    """The part's overall size, in millimetres.

    Deliberately the size and not a mass. A mass would have to come either from
    evaluating 210 trees to render one listing, or from a crude volume sum — and
    the crude sum is wrong in a way that looks right: intersecting three slabs to
    make a hexagon has no cheap closed form, so an M4 nut came out 50% heavy.
    The size is exact, it is what someone picking a part is checking, and the
    real mass arrives with the document the moment it is inserted.
    """
    lo = [float("inf")] * 3
    hi = [float("-inf")] * 3
    # Through `build_tree`, not `entry.build()`: a parameterised row's builder
    # takes its dimensions as arguments and has no defaults of its own to fall
    # back on — the defaults live on the descriptors, which is the only place
    # they can also be published and range-checked.
    _extent(build_tree(entry)["root"], lo, hi)
    if lo[0] > hi[0]:
        return {"x": 0.0, "y": 0.0, "z": 0.0}
    return {axis: round((hi[i] - lo[i]) / MM, 3)
            for i, axis in enumerate("xyz")}


def _extent(node: dict, lo: List[float], hi: List[float], additive: bool = True) -> None:
    """Grow `lo`/`hi` over the additive leaves. Subtracted tools remove material
    and so can only shrink the envelope — including them would report the reach
    of a cutting cylinder as part of the part."""
    kind = node.get("type")
    params = node.get("params") or {}
    declared = params.get("envelope_mm")
    if declared and additive:
        # A node that knows its own extent — see `_hex_prism`.
        centre = params.get("center") or {}
        for i, a in enumerate("xyz"):
            half = float(declared[i]) * MM / 2
            lo[i] = min(lo[i], centre.get(a, 0.0) - half)
            hi[i] = max(hi[i], centre.get(a, 0.0) + half)
        return
    if kind in ("BoxFeature", "CylinderFeature") and additive:
        centre = params.get("center") or {}
        if kind == "BoxFeature":
            size = params.get("size") or {}
            half = [size.get(a, 0.0) / 2 for a in "xyz"]
        else:
            radius, height = params.get("radius", 0.0), abs(params.get("height", 0.0))
            axis = params.get("axis") or {"x": 0, "y": 0, "z": 1}
            half = [(height / 2 if abs(axis.get(a, 0)) > 0.5 else radius)
                    for a in "xyz"]
        for i, a in enumerate("xyz"):
            lo[i] = min(lo[i], centre.get(a, 0.0) - half[i])
            hi[i] = max(hi[i], centre.get(a, 0.0) + half[i])
        return
    children = params.get("children") or []
    operation = params.get("operation", "union")
    if operation == "intersect" and additive:
        # An intersection is at most its *smallest* child, so unioning the
        # children's extents reports a hexagon made from three crossed slabs as
        # being as wide as one slab. Each child measured on its own, then
        # narrowed. Rotations are not tracked, which is conservative in the
        # right direction: the answer can only come out too generous.
        boxes = []
        for child in children:
            child_lo, child_hi = [float("inf")] * 3, [float("-inf")] * 3
            _extent(child, child_lo, child_hi, True)
            if child_lo[0] <= child_hi[0]:
                boxes.append((child_lo, child_hi))
        if boxes:
            for i in range(3):
                lo[i] = min(lo[i], max(b[0][i] for b in boxes))
                hi[i] = max(hi[i], min(b[1][i] for b in boxes))
        return
    for index, child in enumerate(children):
        # Only the first child of a subtract contributes material.
        _extent(child, lo, hi, additive and (operation != "subtract" or index == 0))
