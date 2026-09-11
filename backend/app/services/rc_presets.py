"""The numbers a 1/16 RC build starts from, in one place — and what they are.

Two kinds of number get called a "preset" and they are not the same thing, so
this module keeps them apart and labels every row with which it is:

- **A standard.** M2.5 is M2.5 in ISO 68-1 and a module-0.6 gear meshes with
  every other module-0.6 gear by ISO 54. These are not conventions and not
  opinions; `parts_catalog` already generates parts from them, and the rows here
  point at that catalog rather than restating its tables.
- **A band.** A 1/16 buggy's wheelbase is 225-275 mm because that is the range
  the platforms in that class sit in. Nobody published it. It is a starting
  point and the typical value in the middle of it is a *suggestion*, which is
  why every band states its low and high rather than a single figure dressed up
  as a specification.

`kind` on each row says which, and it is not decoration. A model reading this
catalog needs to know that changing `wheelbase` to 300 is a design decision and
changing `pinion_bore` to 3.2 is a mistake — the first leaves the band, the
second leaves the standard, and only one of those is recoverable by asking the
user what they meant.

Two of the entries here are computations rather than tables, because the number
somebody actually wants is derived and getting it by hand is where the mistakes
live:

- **Ackermann.** The guide construction is that each steering arm's outer ball
  lies on the line from its own kingpin to the *centre of the rear axle*. That
  single sentence fixes the arm angle, and from it the tie-rod length. Drawn by
  eye it is wrong by a few degrees and the car pushes in slow corners, which is
  diagnosed as everything except the steering geometry.
- **Spur and pinion.** Centre distance is `m(z1+z2)/2` and it decides where the
  motor mount goes. Mesh it by eye and the gears either bind or strip; get the
  arithmetic right and the mount is placed once.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional

#: The scale this toolkit was built for. One entry, deliberately: a band for a
#: class nobody here measured would read exactly like this one and mean nothing.
SCALE = "1/16"

STANDARD = "standard"
BAND = "band"


def _band(name: str, low: float, high: float, typical: float, unit: str,
          note: str) -> Dict[str, Any]:
    return {"name": name, "kind": BAND, "low": low, "high": high,
            "typical": typical, "unit": unit, "note": note}


def _std(name: str, value: Any, unit: str, standard: str,
         note: str) -> Dict[str, Any]:
    return {"name": name, "kind": STANDARD, "value": value, "unit": unit,
            "standard": standard, "note": note}


#: Groups, in the order somebody lays a car out: the box it lives in, then what
#: turns inside it, then what holds it together.
GROUPS: Dict[str, List[Dict[str, Any]]] = {
    "chassis": [
        _band("wheelbase", 225.0, 275.0, 250.0, "mm",
              "axle centre to axle centre; the band 1/16 short-course and buggy "
              "platforms sit in"),
        _band("track", 170.0, 200.0, 185.0, "mm",
              "wheel centreline to wheel centreline; wider is more stable and "
              "less likely to fit a stock body"),
        _band("ride_height", 12.0, 25.0, 18.0, "mm",
              "chassis underside to ground at static ride"),
        _band("chassis_thickness", 2.5, 4.0, 3.0, "mm",
              "a printed chassis plate; under 2.5 mm it flexes visibly in a "
              "0.4 mm-nozzle print"),
    ],
    "shaft": [
        _std("axle_shaft", 3.0, "mm", "h7 ground stock",
             "the 1/16 standard: 3 mm ground shaft runs in a 3 mm bore bearing"),
        _std("layshaft", 5.0, "mm", "h7 ground stock",
             "5 mm where a shaft carries the spur gear's load"),
        _std("kingpin", 3.0, "mm", "h7 ground stock",
             "3 mm kingpin through the knuckle; the same stock as the axle"),
    ],
    "bearing": [
        _std("axle_bearing", "MR105", "5x10x4 mm", "miniature metric (MR) series",
             "the usual 5 mm-bore RC bearing; press into a 10 mm pocket"),
        _std("axle_bearing_wide", "MR115", "5x11x4 mm", "miniature metric (MR) series",
             "the other 5 mm bore in common use — check which your hubs take, "
             "the outer diameter differs by a millimetre and they do not swap"),
        _std("small_bearing", "623", "3x10x4 mm", "ISO 15",
             "a 3 mm bore for a kingpin or an idler"),
    ],
    "hardware": [
        _std("chassis_screw", "M2.5", "ISO 4762", "ISO 4762 / ISO 68-1",
             "socket cap into a printed boss or a nut trap"),
        _std("link_screw", "M2", "ISO 4762", "ISO 4762 / ISO 68-1",
             "M2 through a ball end or a servo horn"),
        _std("nut", "M2.5", "ISO 4032", "ISO 4032",
             "trap it rather than tapping the print — a printed thread in "
             "PLA strips at the torque a chassis screw wants"),
    ],
    "gear": [
        _std("module_fine", 0.5, "mm", "ISO 54",
             "fine pitch; more teeth in the same centre distance and less "
             "tolerant of a printed tooth"),
        _std("module_common", 0.6, "mm", "ISO 54",
             "the usual 1/16 spur/pinion module"),
        _std("module_coarse", 0.8, "mm", "ISO 54",
             "coarse; the printable one, and what a 1/10 uses"),
        _band("pinion_teeth", 12.0, 24.0, 16.0, "count",
              "below 17 a standard 20-degree involute undercuts at the root — "
              "real, and it is what `gear_layout` warns about"),
        _band("spur_teeth", 48.0, 90.0, 64.0, "count",
              "with the pinion this sets the reduction; larger spurs need the "
              "centre distance checked against the chassis"),
    ],
    # The values are `parts_catalog.SERVOS` keys, and the test holds them to
    # it: a preset naming a class the catalog cannot build is a suggestion
    # nobody can place.
    "servo": [
        _std("micro", "micro", "servo class", "de-facto case standard",
             "the 9 g micro class: 22.8 x 12.2 mm case, ears 28 mm apart"),
        _std("mini", "mini", "servo class", "de-facto case standard",
             "32.5 x 16.8 mm case, ears 41 mm apart — for a 1/16 or a tight "
             "1/10 front end"),
        _std("standard_case", "standard", "servo class", "de-facto case standard",
             "the 40.5 x 20 mm Futaba-derived case, ears 49.5 mm apart; heavy "
             "for 1/16 but it is what a high-torque steering servo comes in"),
    ],
}


def groups() -> List[str]:
    return list(GROUPS)


def describe(group: Optional[str] = None) -> Dict[str, Any]:
    """The preset rows, optionally for one group."""
    if group:
        key = str(group).strip().lower()
        if key not in GROUPS:
            raise KeyError(key)
        chosen = {key: GROUPS[key]}
    else:
        chosen = GROUPS
    return {
        "scale": SCALE,
        "groups": {k: list(v) for k, v in chosen.items()},
        "note": ("`kind` says what a row is: `standard` numbers come from a "
                 "published standard and a different value is a mistake; `band` "
                 "numbers are the range this class of vehicle sits in and a "
                 "different value is a design decision."),
    }


def ackermann(wheelbase_mm: float, kingpin_track_mm: float,
              arm_mm: float) -> Dict[str, Any]:
    """The steering arm angle and tie-rod length for the guide construction.

    100% Ackermann in the form everyone actually draws: each arm's outer ball
    sits on the line from its own kingpin to the centre of the rear axle, so
    ``tan(theta) = (track/2) / wheelbase``. The inner wheel then turns further
    than the outer one, which is the whole point — both wheels want to trace
    circles about one centre and the inner one's is tighter.

    Returned in the frame the knuckle is modelled in: ``arm_back_mm`` is how far
    behind the kingpin the ball sits and ``arm_in_mm`` how far inboard, so the
    numbers drop straight into a feature's centre without anybody re-deriving
    the triangle.
    """
    wheelbase = float(wheelbase_mm)
    track = float(kingpin_track_mm)
    arm = float(arm_mm)
    if wheelbase <= 0 or track <= 0 or arm <= 0:
        raise ValueError("wheelbase, track and arm length must all be positive")

    theta = math.atan2(track / 2.0, wheelbase)
    back = arm * math.cos(theta)
    inward = arm * math.sin(theta)
    tie_rod = track - 2.0 * inward
    if tie_rod <= 0:
        raise ValueError(
            f"an arm of {arm:g} mm reaches past the centreline on a "
            f"{track:g} mm track; shorten the arm")
    return {
        "wheelbase_mm": round(wheelbase, 3),
        "kingpin_track_mm": round(track, 3),
        "arm_mm": round(arm, 3),
        "arm_angle_deg": round(math.degrees(theta), 3),
        "arm_back_mm": round(back, 3),
        "arm_in_mm": round(inward, 3),
        "tie_rod_mm": round(tie_rod, 3),
        "note": ("100% Ackermann: each arm points at the centre of the rear "
                 "axle. Racers often run less than 100% deliberately — shorten "
                 "the arm angle, not the wheelbase, to get there."),
    }


def spur_pinion(module_mm: float, pinion_teeth: int,
                spur_teeth: int) -> Dict[str, Any]:
    """Pitch diameters, centre distance and reduction for one gear pair.

    The centre distance is the number the motor mount is drilled to, so it is
    computed rather than measured off a layout. ``warnings`` carries the two
    facts that bite: a pinion under 17 teeth undercuts at the root on a standard
    20-degree involute, and two gears of different modules do not mesh at all,
    whatever the centre distance says.
    """
    module = float(module_mm)
    z1, z2 = int(pinion_teeth), int(spur_teeth)
    if module <= 0:
        raise ValueError("module must be positive")
    if z1 < 4 or z2 < 4:
        raise ValueError("a gear with fewer than four teeth is not a gear")

    pitch1, pitch2 = module * z1, module * z2
    warnings: List[str] = []
    if z1 < 17:
        warnings.append(
            f"a {z1}-tooth pinion undercuts at the root on a standard "
            f"20-degree involute (the limit is 17); it is common in RC and it "
            f"is weaker than the tooth count suggests")
    return {
        "module_mm": round(module, 4),
        "pinion_teeth": z1,
        "spur_teeth": z2,
        "pinion_pitch_dia_mm": round(pitch1, 4),
        "spur_pitch_dia_mm": round(pitch2, 4),
        "pinion_outside_dia_mm": round(module * (z1 + 2), 4),
        "spur_outside_dia_mm": round(module * (z2 + 2), 4),
        "center_distance_mm": round(module * (z1 + z2) / 2.0, 4),
        "reduction": round(z2 / z1, 4),
        "warnings": warnings,
        "note": "both gears must be the same module to mesh",
    }
