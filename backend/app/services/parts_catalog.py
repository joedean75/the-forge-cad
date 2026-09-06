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


class Servo(NamedTuple):
    """An RC servo case, by form factor rather than by manufacturer.

    Servo cases are a *de-facto* standard the way NEMA frames are a written one:
    the "9 g micro" and the "standard" case have had the same body and the same
    mounting-tab span across dozens of makers for decades, because a horn, a
    tray and a mounting cut-out all have to interchange. That is what makes it
    honest to ship these — the dimensions belong to the form factor, not to
    somebody's product, and the notes say so.

    `mass_g` is carried only where the form factor names it: a "9 g servo" is
    called that because of what it weighs. Where it does not, the field is
    `None` and the part reports `mass_source: unknown` until somebody records
    the real figure with `set_part_mass` — which is the right answer, because a
    metal-geared case of the same size is half again as heavy.
    """

    body_l: float        # case length (the long axis, through the tabs), mm
    body_w: float        # case width, mm
    body_h: float        # case height to the top of the case, mm
    tab_span: float      # hole centres across the mounting tabs, mm
    tab_thick: float     # tab thickness, mm
    output_dia: float    # output spline boss diameter, mm
    output_h: float      # boss height above the case, mm
    mass_g: Optional[float]
    note: str


#: Servo form factors. Sizes are the industry case standards; masses only where
#: the form factor's own name states one.
SERVOS: Dict[str, Servo] = {
    "sub-micro": Servo(20.0, 8.6, 20.0, 27.5, 2.0, 4.8, 3.0, None,
                       "sub-micro (3.7 g class) case; mass varies with gearing"),
    "micro-9g":  Servo(22.8, 12.2, 22.5, 32.5, 2.5, 4.8, 3.5, 9.0,
                       "the 9 g micro case, named for its mass; a metal-geared "
                       "one in the same case is nearer 13 g — record it"),
    "standard":  Servo(40.5, 20.0, 36.5, 49.5, 2.5, 5.9, 4.0, None,
                       "the standard (Futaba-derived) case; mass runs 40-60 g "
                       "by gearing and motor"),
}


class Bldc(NamedTuple):
    """A brushless outrunner, sized by its stator code.

    The code *is* the dimension: a 2205 has a 22 mm stator diameter and a 5 mm
    stator height, a 2807 a 28 mm stator. That is the one thing about these
    motors that is standardised, so it is what the envelope is built from — the
    can is the stator plus a conventional clearance, and the mounting pattern is
    the near-universal 16 mm (M3) or 12 mm (M2) cross.

    No mass. It is set by the winding, the KV and the bell material, and two
    2205s can differ by 40% — a published figure here would be a guess with a
    decimal point on it.
    """

    stator_dia: float
    stator_h: float
    can_dia: float
    can_h: float
    shaft_dia: float
    shaft_len: float
    bolt_span: float     # mounting cross, hole centres, mm
    bolt: str


#: Stator codes. `can_dia`/`can_h` are the stator plus the conventional bell and
#: bearing stack; `bolt_span` is the standard mounting cross for the class.
BLDC: Dict[str, Bldc] = {
    "1806": Bldc(18.0, 6.0, 23.0, 16.0, 3.0, 9.0, 12.0, "M2"),
    "2204": Bldc(22.0, 4.0, 27.5, 15.0, 3.0, 9.0, 16.0, "M3"),
    "2205": Bldc(22.0, 5.0, 27.5, 16.0, 3.0, 9.0, 16.0, "M3"),
    "2306": Bldc(23.0, 6.0, 28.5, 18.0, 4.0, 10.0, 16.0, "M3"),
    "2807": Bldc(28.0, 7.0, 34.0, 20.0, 4.0, 11.0, 16.0, "M3"),
    "2812": Bldc(28.0, 12.0, 34.0, 26.0, 5.0, 12.0, 16.0, "M3"),
}


class Board(NamedTuple):
    """A stack-mounted electronics board, sized by its mounting standard.

    Flight controllers and the ESCs that stack under them are standardised on
    the *mounting pattern* and nothing else: 30.5 x 30.5 mm, 20 x 20 mm, and the
    16 x 16 mm micro pattern. Board outline follows the pattern by convention
    and component height is what varies, so height is a parameter with a
    conventional default and the notes say it is an envelope for clearance.
    """

    pattern: float       # mounting hole pitch, square, mm
    outline: float       # board outline, square, mm
    height: float        # envelope height including the tallest components, mm
    bolt: str


BOARDS: Dict[str, Board] = {
    "30x30": Board(30.5, 36.0, 8.0, "M3"),
    "20x20": Board(20.0, 27.0, 7.0, "M2"),
    "16x16": Board(16.0, 20.0, 6.0, "M2"),
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
    hole = METRIC[n.bolt].clearance / 2
    offset = n.bolt_circle / 2

    holes = []
    for sx in (-1, 1):
        for sy in (-1, 1):
            holes.append({"type": "CylinderFeature", "name": "Mount hole", "params": {
                "radius": hole * MM, "height": (body * 1.2) * MM,
                "center": {"x": sx * offset * MM, "y": sy * offset * MM,
                           "z": (-body / 2) * MM},
                "axis": {"x": 0.0, "y": 0.0, "z": 1.0}}})

    return _tree(_subtract([
        _union([
            _box(n.face, n.face, body, (0, 0, -body / 2), name="Frame"),
            _cyl(n.pilot_dia / 2, n.pilot_height, n.pilot_height / 2, name="Pilot boss"),
            _cyl(n.shaft_dia / 2, n.shaft_length,
                 n.pilot_height + n.shaft_length / 2, name="Shaft"),
        ], name=f"{frame} stepper"),
        *holes,
    ], name=f"{frame} stepper"))


def rc_servo(form: str) -> dict:
    """A servo case: body, mounting tabs, output boss.

    Three things a design needs from a servo and nothing else: the volume it
    occupies, where the two mounting screws go, and where the horn turns. The
    gearbox, the case screws and the wire exit are omitted — they vary by maker
    and none of them is what a tray or a cut-out is dimensioned from.
    """
    v = SERVOS[form]
    hole = METRIC["M2"].clearance / 2
    tab_z = v.body_h - v.tab_thick - 4.0     # tabs sit below the case top

    holes = [
        {"type": "CylinderFeature", "name": "Tab hole", "params": {
            "radius": hole * MM, "height": (v.tab_thick * 4) * MM,
            "center": {"x": sx * (v.tab_span / 2) * MM, "y": 0.0, "z": tab_z * MM},
            "axis": {"x": 0.0, "y": 0.0, "z": 1.0}}}
        for sx in (-1, 1)
    ]
    return _tree(_subtract([
        _union([
            _box(v.body_l, v.body_w, v.body_h, (0, 0, v.body_h / 2), name="Case"),
            _box(v.tab_span + 8.0, v.body_w, v.tab_thick,
                 (0, 0, tab_z + v.tab_thick / 2), name="Mounting tabs"),
            _cyl(v.output_dia / 2, v.output_h,
                 v.body_h + v.output_h / 2, name="Output boss"),
        ], name=f"{form} servo"),
        *holes,
    ], name=f"{form} servo"))


def bldc_motor(code: str) -> dict:
    """A brushless outrunner: can, shaft, and the mounting cross underneath.

    The bell and the base are one cylinder rather than two: the gap between them
    is where the windings are, and modelling it would suggest a clearance a
    mount can use, which it cannot — the bell rotates.
    """
    m = BLDC[code]
    hole = METRIC[m.bolt].clearance / 2
    holes = []
    # The standard cross: four holes on the axes, not on the diagonals.
    for axis in ("x", "y"):
        for sign in (-1, 1):
            at = {"x": 0.0, "y": 0.0, "z": 0.0}
            at[axis] = sign * (m.bolt_span / 2)
            holes.append({"type": "CylinderFeature", "name": "Mount hole", "params": {
                "radius": hole * MM, "height": (m.can_h * 2) * MM,
                "center": {"x": at["x"] * MM, "y": at["y"] * MM, "z": 0.0},
                "axis": {"x": 0.0, "y": 0.0, "z": 1.0}}})
    return _tree(_subtract([
        _union([
            _cyl(m.can_dia / 2, m.can_h, m.can_h / 2, name="Can"),
            _cyl(m.shaft_dia / 2, m.shaft_len,
                 m.can_h + m.shaft_len / 2, name="Shaft"),
        ], name=f"{code} motor"),
        *holes,
    ], name=f"{code} motor"))


def stack_board(pattern: str, height_mm: Optional[float] = None) -> dict:
    """A stack-mounted board: outline, component envelope, mounting holes.

    One box, not a board plus components, because the thing a frame needs is the
    clearance volume. Height is a parameter for the same reason a stepper's body
    length is: the pattern is standardised and what sits on the board is not.
    """
    b = BOARDS[pattern]
    height = float(height_mm if height_mm else b.height)
    hole = METRIC[b.bolt].clearance / 2
    holes = [
        {"type": "CylinderFeature", "name": "Mount hole", "params": {
            "radius": hole * MM, "height": (height * 3) * MM,
            "center": {"x": sx * (b.pattern / 2) * MM,
                       "y": sy * (b.pattern / 2) * MM, "z": 0.0},
            "axis": {"x": 0.0, "y": 0.0, "z": 1.0}}}
        for sx in (-1, 1) for sy in (-1, 1)
    ]
    return _tree(_subtract([
        _box(b.outline, b.outline, height, (0, 0, height / 2), name="Board envelope"),
        *holes,
    ], name=f"{pattern} board"))


def ground_shaft(diameter_mm: float, length_mm: float) -> dict:
    """A length of ground shaft. A cylinder, and that is the whole part."""
    return _tree(_cyl(diameter_mm / 2, length_mm, 0.0, name="Shaft"))


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


# ------------------------------------------------------------- the listing --

class Entry(NamedTuple):
    key: str                     # stable id: "iso4762-m4-16"
    category: str
    designation: str             # what gets ordered
    standard: str
    material: str                # a catalog material id
    fidelity: str                # "as-standard" | "envelope"
    notes: str
    build: Any                   # () -> feature tree
    # Published mass, where the body's own geometry is known to mislead. A
    # stepper is laminations, windings and air, so a solid steel envelope of a
    # NEMA 17 weighs 669 g against a real 350 g. The *document* keeps reporting
    # its geometric mass, because that is what its geometry is; this says what
    # the real thing weighs so a BOM total is not quietly doubled.
    typical_mass_g: Optional[float] = None


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

    for form, v in SERVOS.items():
        out.append(Entry(
            key=f"servo-{form}", category="motor",
            designation=f"RC servo, {form} case",
            standard="industry case form factor", material="abs",
            fidelity="envelope",
            notes=(f"{v.body_l:g}x{v.body_w:g}x{v.body_h:g} mm case, "
                   f"{v.tab_span:g} mm between tab holes. {v.note}. Gearbox, "
                   "case screws and wire exit omitted — a tray is dimensioned "
                   "from the case and the tabs."),
            build=(lambda f=form: rc_servo(f)),
            typical_mass_g=v.mass_g))

    for code, m in BLDC.items():
        out.append(Entry(
            key=f"bldc-{code}", category="motor",
            designation=f"{code} brushless outrunner",
            standard="stator code (diameter x height, mm)",
            material="al-6061", fidelity="envelope",
            notes=(f"{m.stator_dia:g} mm stator, {m.can_dia:g} mm can, "
                   f"{m.shaft_dia:g} mm shaft on a {m.bolt_span:g} mm "
                   f"{m.bolt} cross. No published mass: it is set by the "
                   "winding, the KV and the bell, and two motors of one stator "
                   "code differ by 40% — record yours with set_part_mass."),
            build=(lambda c=code: bldc_motor(c))))

    for pattern, b in BOARDS.items():
        for role in ("flight controller", "ESC", "receiver"):
            slug = role.split()[0].lower()
            out.append(Entry(
                key=f"board-{pattern}-{slug}", category="electronics",
                designation=f"{role}, {pattern} mm stack",
                standard=f"{b.pattern:g} mm mounting pattern",
                # G10 rather than a new "fr4" id: FR-4 is G10 with a flame
                # retardant and the same glass-epoxy laminate, so the density
                # that matters here is already in the catalog. Material ids are
                # persisted, and the rule is that new grades are added only when
                # an existing one is genuinely wrong.
                material="gfrp", fidelity="envelope",
                notes=(f"clearance envelope {b.outline:g}x{b.outline:g}x"
                       f"{b.height:g} mm on the {b.pattern:g} mm {b.bolt} "
                       "pattern. The pattern is the standard; the outline and "
                       "the component height are conventions, so treat the "
                       "height as a clearance to check rather than a fact. "
                       "No published mass — record yours."),
                build=(lambda p=pattern: stack_board(p))))

    for diameter in (2.0, 3.0, 4.0, 5.0, 6.0):
        for length in (30, 50, 80, 120):
            out.append(Entry(
                key=f"shaft-{diameter:g}-{length}", category="shaft",
                designation=f"ground shaft ⌀{diameter:g}x{length}",
                standard="h7 stock shafting", material="steel-4140",
                fidelity="as-standard",
                notes="plain ground shaft; cut to length. The mating bearing "
                      "bore is the same nominal — see the bearing entries.",
                build=(lambda d=diameter, l=length: ground_shaft(d, l))))

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
    return out


_BY_KEY: Optional[Dict[str, Entry]] = None


def by_key(key: str) -> Optional[Entry]:
    global _BY_KEY
    if _BY_KEY is None:
        _BY_KEY = {e.key: e for e in catalog()}
    return _BY_KEY.get(key)


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
    _extent(entry.build()["root"], lo, hi)
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
