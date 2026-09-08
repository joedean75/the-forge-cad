"""Starter parts — a feature tree with its driving parameters already named.

A new document is three datum planes and no body, which is correct and leaves a
beginner with nothing to edit. A template fills that: not a file to import, but
the tree somebody would have built, with the numbers that drive it already
lifted out into named parameters. The first thing a user does is change
``length`` from 100 to 140, not construct a box.

**A template is an operation sequence, not a serialized tree.** Each one is a
list of ``{op, args}`` run through :func:`decompiler.replay`, which is the same
path a recorded trajectory replays through, with the same ``{"$step": i}``
references. That is not a stylistic choice — three properties fall out of it and
none of them survive a hand-written tree:

- **It can only build what the catalog can build.** A template that reached past
  the operation surface would be content a model could not reproduce, and it
  would show up in ``corpus_report`` as an undecompilable document dragging
  every feature type in it to 0%.
- **It is a demonstration.** Applied inside a recording session it writes a real
  trajectory, because the operations genuinely run.
- **There is one implementation.** The gallery button and an agent calling
  ``apply_template`` execute the identical sequence.

**Parameters are bound in a second step, deliberately.** ``add_primitive`` takes
``params.size.x`` as a *number* and ``op_schema`` enforces it, so the expression
string ``"=length"`` cannot be smuggled in there — nor should it be, since the
schema is what a constrained decoder reads. The literal is built first and
``set_feature_params`` — whose ``params`` is an opaque object the feature owns —
rebinds it to the expression. That is exactly the sequence a person performs.

The literals and the expressions must agree: the template is applied by running
the steps, so a mismatch is a body that jumps the first time a parameter is
touched. ``test_templates`` evaluates each one and checks the bound tree against
the declared parameter values.
"""

from __future__ import annotations

from typing import Dict, List, NamedTuple, Optional


class Template(NamedTuple):
    key: str
    name: str
    summary: str
    #: What the finished part is for, in the words someone searching would use.
    tags: tuple
    #: ``[{name, expr, unit}]`` — the driving parameters, in dependency order.
    parameters: List[dict]
    #: ``[{op, args}]``, ``{"$step": i}`` referring to the feature step *i* made.
    steps: List[dict]


def _mm(**kw) -> List[dict]:
    return [{"name": n, "expr": str(v), "unit": "mm"} for n, v in kw.items()]


def _box(size, center, operation: str = "union") -> dict:
    return {"op": "add_primitive",
            "args": {"shape": "box", "operation": operation,
                     "params": {"size": dict(zip("xyz", size)),
                                "center": dict(zip("xyz", center))}}}


def _cyl(radius, height, center, axis=(0.0, 0.0, 1.0), operation: str = "union") -> dict:
    return {"op": "add_primitive",
            "args": {"shape": "cylinder", "operation": operation,
                     "params": {"radius": radius, "height": height,
                                "center": dict(zip("xyz", center)),
                                "axis": dict(zip("xyz", axis))}}}


def _bind(step: int, params: dict) -> dict:
    return {"op": "set_feature_params",
            "args": {"feature_id": {"$step": step}, "params": params}}


def _hole(screw: str, center, axis=(0.0, 0.0, 1.0)) -> dict:
    return {"op": "clearance_hole",
            "args": {"screw": screw, "center": dict(zip("xyz", center)),
                     "axis": dict(zip("xyz", axis))}}


def _sphere(radius, center, operation: str = "union") -> dict:
    return {"op": "add_primitive",
            "args": {"shape": "sphere", "operation": operation,
                     "params": {"radius": radius,
                                "center": dict(zip("xyz", center))}}}


def _sweep(path, radius, operation: str = "union") -> dict:
    return {"op": "add_sweep",
            "args": {"path": [list(p) for p in path], "radius": radius,
                     "operation": operation}}


def _mech(kind: str, label: str, **kw) -> dict:
    """A mechanism note as a template step.

    A template is an operation sequence, so "this shaft turns clockwise about X"
    is a step like any other — which means the drivetrain templates arrive with
    their own shaft axes and gear directions already recorded, and a drawing of
    one comes out with the rotation arrows on it without anybody typing a note.
    """
    return {"op": "annotate_mechanism", "args": {"kind": kind, "label": label, **kw}}


def _counts(**kw) -> List[dict]:
    return [{"name": n, "expr": str(v), "unit": "count"} for n, v in kw.items()]


#: The Ackermann triangle, written as arithmetic because the expression
#: evaluator has `sqrt` and no `atan`. ``arm_back = arm·cos(theta)`` with
#: ``cos(theta) = wheelbase / hypotenuse`` — identical to the angle form and it
#: keeps the *relation* in the parameter binding, so a knuckle whose wheelbase
#: is edited re-aims its own steering arm instead of quietly keeping the angle
#: it was born with.
_ACK_HYP = "sqrt(wheelbase*wheelbase + (kingpin_track/2)*(kingpin_track/2))"
_ARM_BACK = f"=arm_length*wheelbase/({_ACK_HYP})"
_ARM_IN = f"=arm_length*(kingpin_track/2)/({_ACK_HYP})"


# ---------------------------------------------------------------- the parts --

_PLATE = Template(
    key="plate",
    name="Mounting plate",
    summary="A rectangular plate with an M4 clearance hole inset from each corner.",
    tags=("plate", "bracket", "mount", "base"),
    parameters=_mm(length=100, width=60, thickness=8, hole_inset=10),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            length=100, width=60, thickness=8, hole_inset=10)}},
        _box((0.100, 0.060, 0.008), (0.0, 0.0, 0.004)),                        # 1
        _bind(1, {"size": {"x": "=length", "y": "=width", "z": "=thickness"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=thickness/2"}}),
        _hole("M4", (0.040, 0.020, 0.0)),                                      # 3
        _hole("M4", (-0.040, 0.020, 0.0)),                                     # 4
        _hole("M4", (0.040, -0.020, 0.0)),                                     # 5
        _hole("M4", (-0.040, -0.020, 0.0)),                                    # 6
        # The hole depth follows the plate: a bore sized against the old
        # thickness stops inside a plate somebody thickened.
        _bind(3, {"height": "=thickness*4",
                  "center": {"x": "=length/2-hole_inset", "y": "=width/2-hole_inset", "z": 0.0}}),
        _bind(4, {"height": "=thickness*4",
                  "center": {"x": "=-(length/2-hole_inset)", "y": "=width/2-hole_inset", "z": 0.0}}),
        _bind(5, {"height": "=thickness*4",
                  "center": {"x": "=length/2-hole_inset", "y": "=-(width/2-hole_inset)", "z": 0.0}}),
        _bind(6, {"height": "=thickness*4",
                  "center": {"x": "=-(length/2-hole_inset)", "y": "=-(width/2-hole_inset)", "z": 0.0}}),
    ],
)

_BRACKET = Template(
    key="bracket",
    name="L-bracket",
    summary="Two legs at a right angle, one M5 clearance hole through each.",
    tags=("bracket", "angle", "L", "corner", "mount"),
    parameters=_mm(leg_a=80, leg_b=60, width=40, thickness=6, hole_inset=15),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            leg_a=80, leg_b=60, width=40, thickness=6, hole_inset=15)}},
        _box((0.080, 0.040, 0.006), (0.040, 0.0, 0.003)),                      # 1  foot
        _bind(1, {"size": {"x": "=leg_a", "y": "=width", "z": "=thickness"},
                  "center": {"x": "=leg_a/2", "y": 0.0, "z": "=thickness/2"}}),
        _box((0.006, 0.040, 0.060), (0.003, 0.0, 0.030)),                      # 3  upright
        _bind(3, {"size": {"x": "=thickness", "y": "=width", "z": "=leg_b"},
                  "center": {"x": "=thickness/2", "y": 0.0, "z": "=leg_b/2"}}),
        _hole("M5", (0.065, 0.0, 0.0)),                                        # 5
        _bind(5, {"height": "=thickness*4",
                  "center": {"x": "=leg_a-hole_inset", "y": 0.0, "z": 0.0}}),
        # Through the upright, so the axis is X and the depth follows the wall.
        _hole("M5", (0.0, 0.0, 0.045), axis=(1.0, 0.0, 0.0)),                  # 7
        _bind(7, {"height": "=thickness*4",
                  "center": {"x": "=thickness/2", "y": 0.0, "z": "=leg_b-hole_inset"}}),
    ],
)

_ENCLOSURE = Template(
    key="enclosure",
    name="Open-top enclosure",
    summary="A walled box with a floor and an open top, cut as an inner cavity.",
    tags=("enclosure", "box", "case", "housing", "shell"),
    parameters=_mm(length=120, width=80, height=40, wall=3),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            length=120, width=80, height=40, wall=3)}},
        _box((0.120, 0.080, 0.040), (0.0, 0.0, 0.020)),                        # 1
        _bind(1, {"size": {"x": "=length", "y": "=width", "z": "=height"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=height/2"}}),
        # The cavity runs past the top face, which is what opens it. A
        # ShellFeature would hollow uniformly and leave the lid on — that is
        # `abs(d) - t/2`, not a face removal.
        _box((0.114, 0.074, 0.040), (0.0, 0.0, 0.023), operation="subtract"),  # 3
        _bind(3, {"size": {"x": "=length-2*wall", "y": "=width-2*wall", "z": "=height"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=wall+height/2"}}),
    ],
)

_SHAFT = Template(
    key="shaft",
    name="Stepped shaft",
    summary="A round shaft with a smaller journal on one end for a bearing.",
    tags=("shaft", "axle", "spindle", "pin", "round"),
    parameters=_mm(shaft_dia=20, shaft_length=120, journal_dia=12, journal_length=25),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            shaft_dia=20, shaft_length=120, journal_dia=12, journal_length=25)}},
        _cyl(0.010, 0.120, (0.0, 0.0, 0.060)),                                 # 1
        _bind(1, {"radius": "=shaft_dia/2", "height": "=shaft_length",
                  "center": {"x": 0.0, "y": 0.0, "z": "=shaft_length/2"}}),
        _cyl(0.006, 0.025, (0.0, 0.0, 0.1325)),                                # 3
        _bind(3, {"radius": "=journal_dia/2", "height": "=journal_length",
                  "center": {"x": 0.0, "y": 0.0, "z": "=shaft_length+journal_length/2"}}),
    ],
)


# ------------------------------------------------------------ the RC toolkit --
#
# Eight small mechanisms rather than one "RC car", because a car is not a part
# and a template that built one would be a body nobody could edit into the car
# they actually wanted. Each of these is a subassembly's worth of envelope with
# the numbers that drive it named, and each carries its own mechanism notes, so
# the drawing of one says which way its shaft turns without anybody annotating
# it by hand.
#
# The dimensions come from `rc_presets`: 3 mm axle stock, 5 mm layshafts, MR105
# bearings, M2/M2.5 hardware, module-0.6 gears. Where a number is a band rather
# than a standard the template takes the middle of the band, which is a starting
# point and is documented as one.

_RC_AXLE = Template(
    key="rc_axle",
    name="Solid axle housing",
    summary=("A tube axle bored through for a 5 mm shaft, with a bearing pocket "
             "counterbored at each end."),
    tags=("rc", "axle", "drivetrain", "housing", "1/16"),
    parameters=_mm(axle_span=120, tube_dia=14, bore_dia=8,
                   bearing_dia=10, bearing_depth=4),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            axle_span=120, tube_dia=14, bore_dia=8,
            bearing_dia=10, bearing_depth=4)}},
        _cyl(0.007, 0.120, (0.0, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),             # 1
        _bind(1, {"radius": "=tube_dia/2", "height": "=axle_span"}),
        # Through-bore: twice the span so it cannot stop inside a lengthened tube.
        _cyl(0.004, 0.240, (0.0, 0.0, 0.0), axis=(1.0, 0.0, 0.0),
             operation="subtract"),                                            # 3
        _bind(3, {"radius": "=bore_dia/2", "height": "=axle_span*2"}),
        _cyl(0.005, 0.004, (0.058, 0.0, 0.0), axis=(1.0, 0.0, 0.0),
             operation="subtract"),                                            # 5
        _bind(5, {"radius": "=bearing_dia/2", "height": "=bearing_depth",
                  "center": {"x": "=axle_span/2-bearing_depth/2", "y": 0.0, "z": 0.0}}),
        _cyl(0.005, 0.004, (-0.058, 0.0, 0.0), axis=(1.0, 0.0, 0.0),
             operation="subtract"),                                            # 7
        _bind(7, {"radius": "=bearing_dia/2", "height": "=bearing_depth",
                  "center": {"x": "=-(axle_span/2-bearing_depth/2)", "y": 0.0, "z": 0.0}}),
        _mech("shaft", "Axle", axis={"x": 1.0, "y": 0.0, "z": 0.0}, direction="cw",
              text="5 mm ground stock"),
        _mech("bearing", "Axle bearing", designation="MR105",
              text="press fit, one at each end"),
    ],
)

_RC_DIFF = Template(
    key="rc_diff",
    name="Differential placeholder",
    summary=("A diff case with a ring-gear flange and a through bore for the "
             "output stubs — the envelope to build a gearbox around."),
    tags=("rc", "differential", "drivetrain", "gearbox", "1/16"),
    parameters=_mm(case_dia=34, case_width=22, ring_dia=48, ring_thick=3,
                   output_bore=5),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            case_dia=34, case_width=22, ring_dia=48, ring_thick=3,
            output_bore=5)}},
        _cyl(0.017, 0.022, (0.0, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),             # 1
        _bind(1, {"radius": "=case_dia/2", "height": "=case_width"}),
        _cyl(0.024, 0.003, (-0.0125, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),         # 3
        _bind(3, {"radius": "=ring_dia/2", "height": "=ring_thick",
                  "center": {"x": "=-(case_width/2+ring_thick/2)", "y": 0.0, "z": 0.0}}),
        _cyl(0.0025, 0.100, (0.0, 0.0, 0.0), axis=(1.0, 0.0, 0.0),
             operation="subtract"),                                            # 5
        _bind(5, {"radius": "=output_bore/2", "height": "=(case_width+ring_thick)*4"}),
        _mech("gear", "Ring gear", axis={"x": 1.0, "y": 0.0, "z": 0.0},
              direction="cw", teeth=40, module_mm=0.8,
              text="meshes with the pinion on the layshaft"),
        _mech("shaft", "Output shaft", axis={"x": 1.0, "y": 0.0, "z": 0.0},
              direction="cw", text="both stubs turn together in this placeholder"),
    ],
)

_RC_DOGBONE = Template(
    key="rc_dogbone",
    name="Dogbone shaft envelope",
    summary=("A driveshaft with a ball on each end — the swept envelope a "
             "dogbone or CVD needs to stay clear of, not the shaft itself."),
    tags=("rc", "dogbone", "cvd", "driveshaft", "envelope", "1/16"),
    parameters=_mm(shaft_dia=4, shaft_length=52, ball_dia=8),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            shaft_dia=4, shaft_length=52, ball_dia=8)}},
        _cyl(0.002, 0.052, (0.0, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),             # 1
        _bind(1, {"radius": "=shaft_dia/2", "height": "=shaft_length"}),
        _sphere(0.004, (0.026, 0.0, 0.0)),                                     # 3
        _bind(3, {"radius": "=ball_dia/2",
                  "center": {"x": "=shaft_length/2", "y": 0.0, "z": 0.0}}),
        _sphere(0.004, (-0.026, 0.0, 0.0)),                                    # 5
        _bind(5, {"radius": "=ball_dia/2",
                  "center": {"x": "=-shaft_length/2", "y": 0.0, "z": 0.0}}),
        _mech("shaft", "Dogbone", axis={"x": 1.0, "y": 0.0, "z": 0.0},
              direction="cw",
              text="envelope at full droop; a plunging shaft sweeps further"),
    ],
)

_RC_UJOINT = Template(
    key="rc_ujoint",
    name="Universal joint envelope",
    summary=("Two shaft legs and the sphere the cross sweeps through — the "
             "envelope a chassis must not intrude into, at any articulation."),
    tags=("rc", "universal", "u-joint", "driveshaft", "envelope", "1/16"),
    parameters=_mm(shaft_dia=5, joint_dia=12, leg_length=25),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            shaft_dia=5, joint_dia=12, leg_length=25)}},
        _sphere(0.006, (0.0, 0.0, 0.0)),                                       # 1
        _bind(1, {"radius": "=joint_dia/2"}),
        _cyl(0.0025, 0.025, (0.0185, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),         # 3
        _bind(3, {"radius": "=shaft_dia/2", "height": "=leg_length",
                  "center": {"x": "=joint_dia/2+leg_length/2", "y": 0.0, "z": 0.0}}),
        _cyl(0.0025, 0.025, (-0.0185, 0.0, 0.0), axis=(1.0, 0.0, 0.0)),        # 5
        _bind(5, {"radius": "=shaft_dia/2", "height": "=leg_length",
                  "center": {"x": "=-(joint_dia/2+leg_length/2)", "y": 0.0, "z": 0.0}}),
        _mech("shaft", "Driveshaft", axis={"x": 1.0, "y": 0.0, "z": 0.0},
              direction="cw",
              text="the sphere is the cross's sweep — keep the chassis out of it"),
    ],
)

_RC_GEARBOX_PLATE = Template(
    key="rc_spur_pinion",
    name="Spur and pinion layout plate",
    summary=("A motor plate bored at the pinion and the layshaft, spaced by the "
             "centre distance the tooth counts demand."),
    tags=("rc", "gear", "spur", "pinion", "motor mount", "drivetrain", "1/16"),
    parameters=(_mm(module=0.6) + _counts(pinion_teeth=16, spur_teeth=64)
                + _mm(plate_margin=8, plate_thickness=4, motor_bore=8,
                      layshaft_dia=5)),
    steps=[
        {"op": "set_parameters", "args": {"parameters": (
            _mm(module=0.6) + _counts(pinion_teeth=16, spur_teeth=64)
            + _mm(plate_margin=8, plate_thickness=4, motor_bore=8,
                  layshaft_dia=5))}},
        # The plate spans from outside the pinion's tips to outside the spur's,
        # so changing a tooth count moves the plate edge with the gear rather
        # than leaving a spur hanging over air.
        _box((0.0652, 0.0556, 0.004), (0.0192, 0.0, 0.002)),                   # 1
        _bind(1, {"size": {
            "x": "=module*(pinion_teeth+spur_teeth)/2 + module*(spur_teeth+2)/2"
                 " + module*(pinion_teeth+2)/2 + 2*plate_margin",
            "y": "=2*(module*(spur_teeth+2)/2 + plate_margin)",
            "z": "=plate_thickness"},
            "center": {
                "x": "=(module*(pinion_teeth+spur_teeth)/2"
                     " + module*(spur_teeth+2)/2"
                     " - module*(pinion_teeth+2)/2)/2",
                "y": 0.0, "z": "=plate_thickness/2"}}),
        _cyl(0.004, 0.016, (0.0, 0.0, 0.0), operation="subtract"),             # 3
        _bind(3, {"radius": "=motor_bore/2", "height": "=plate_thickness*4",
                  "center": {"x": 0.0, "y": 0.0, "z": 0.0}}),
        _cyl(0.0025, 0.016, (0.024, 0.0, 0.0), operation="subtract"),          # 5
        _bind(5, {"radius": "=layshaft_dia/2", "height": "=plate_thickness*4",
                  "center": {"x": "=module*(pinion_teeth+spur_teeth)/2",
                             "y": 0.0, "z": 0.0}}),
        _mech("gear", "Pinion", axis={"x": 0.0, "y": 0.0, "z": 1.0},
              direction="cw", teeth=16, module_mm=0.6, text="on the motor shaft"),
        _mech("gear", "Spur", axis={"x": 0.0, "y": 0.0, "z": 1.0},
              direction="ccw", teeth=64, module_mm=0.6,
              text="externally meshed, so it turns the other way"),
        _mech("bearing", "Layshaft bearing", designation="MR105"),
    ],
)

_RC_MOTOR_MOUNT = Template(
    key="rc_motor_mount",
    name="Motor mount plate",
    summary=("A plate with a centre bore and a four-hole square bolt pattern — "
             "the 16 mm cross a 22xx-class outrunner sits on."),
    tags=("rc", "motor", "mount", "bldc", "plate", "1/16"),
    parameters=_mm(plate_size=30, plate_thickness=4, bolt_span=16, bore_dia=8),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            plate_size=30, plate_thickness=4, bolt_span=16, bore_dia=8)}},
        _box((0.030, 0.030, 0.004), (0.0, 0.0, 0.002)),                        # 1
        _bind(1, {"size": {"x": "=plate_size", "y": "=plate_size",
                           "z": "=plate_thickness"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=plate_thickness/2"}}),
        _cyl(0.004, 0.016, (0.0, 0.0, 0.0), operation="subtract"),             # 3
        _bind(3, {"radius": "=bore_dia/2", "height": "=plate_thickness*4"}),
        _hole("M3", (0.008, 0.008, 0.0)),                                      # 5
        _hole("M3", (-0.008, 0.008, 0.0)),                                     # 6
        _hole("M3", (0.008, -0.008, 0.0)),                                     # 7
        _hole("M3", (-0.008, -0.008, 0.0)),                                    # 8
        _bind(5, {"height": "=plate_thickness*4",
                  "center": {"x": "=bolt_span/2", "y": "=bolt_span/2", "z": 0.0}}),
        _bind(6, {"height": "=plate_thickness*4",
                  "center": {"x": "=-bolt_span/2", "y": "=bolt_span/2", "z": 0.0}}),
        _bind(7, {"height": "=plate_thickness*4",
                  "center": {"x": "=bolt_span/2", "y": "=-bolt_span/2", "z": 0.0}}),
        _bind(8, {"height": "=plate_thickness*4",
                  "center": {"x": "=-bolt_span/2", "y": "=-bolt_span/2", "z": 0.0}}),
        _mech("shaft", "Motor shaft", axis={"x": 0.0, "y": 0.0, "z": 1.0},
              direction="cw", text="through the centre bore"),
        _mech("hardware", "Motor screws", designation="M3",
              text="four, on a 16 mm square"),
    ],
)

_RC_SERVO_TRAY = Template(
    key="rc_servo_tray",
    name="Servo tray",
    summary=("A tray cut for a 9 g micro servo, screwed through its tabs, with "
             "the output end relieved so the horn can swing."),
    tags=("rc", "servo", "steering", "tray", "mount", "1/16"),
    parameters=_mm(tray_length=40, tray_width=24, tray_thickness=3,
                   servo_length=22.8, servo_width=12.2, tab_span=32.5,
                   horn_dia=14, horn_offset=6),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            tray_length=40, tray_width=24, tray_thickness=3,
            servo_length=22.8, servo_width=12.2, tab_span=32.5,
            horn_dia=14, horn_offset=6)}},
        _box((0.040, 0.024, 0.003), (0.0, 0.0, 0.0015)),                       # 1
        _bind(1, {"size": {"x": "=tray_length", "y": "=tray_width",
                           "z": "=tray_thickness"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=tray_thickness/2"}}),
        # The case pocket. Through the tray, because a servo drops in from above
        # and its body is deeper than any tray worth printing.
        _box((0.0228, 0.0122, 0.012), (0.0, 0.0, 0.0015), operation="subtract"),  # 3
        _bind(3, {"size": {"x": "=servo_length", "y": "=servo_width",
                           "z": "=tray_thickness*4"},
                  "center": {"x": 0.0, "y": 0.0, "z": "=tray_thickness/2"}}),
        # Horn clearance: a relief on the output end, wider than the case, so
        # the horn sweeps without touching the tray.
        _cyl(0.007, 0.012, (-0.0054, 0.0, 0.0015), operation="subtract"),      # 5
        _bind(5, {"radius": "=horn_dia/2", "height": "=tray_thickness*4",
                  "center": {"x": "=-(servo_length/2-horn_offset)", "y": 0.0,
                             "z": "=tray_thickness/2"}}),
        _hole("M2", (0.01625, 0.0, 0.0)),                                      # 7
        _hole("M2", (-0.01625, 0.0, 0.0)),                                     # 8
        _bind(7, {"height": "=tray_thickness*4",
                  "center": {"x": "=tab_span/2", "y": 0.0, "z": 0.0}}),
        _bind(8, {"height": "=tray_thickness*4",
                  "center": {"x": "=-tab_span/2", "y": 0.0, "z": 0.0}}),
        _mech("shaft", "Servo output", axis={"x": 0.0, "y": 0.0, "z": 1.0},
              text="the horn swings in the relieved end"),
        _mech("hardware", "Servo screws", designation="M2",
              text="through the case tabs, 32.5 mm apart"),
    ],
)

_RC_KNUCKLE = Template(
    key="rc_knuckle",
    name="Steering knuckle",
    summary=("An upright with a kingpin bore, a stub-axle bore and a steering "
             "arm aimed by the Ackermann construction at the rear axle."),
    tags=("rc", "steering", "knuckle", "upright", "ackermann", "1/16"),
    parameters=(_mm(body_length=14, body_width=10, body_height=22,
                    kingpin_dia=3, axle_dia=5, arm_length=22, arm_dia=6,
                    wheelbase=250, kingpin_track=180)),
    steps=[
        {"op": "set_parameters", "args": {"parameters": _mm(
            body_length=14, body_width=10, body_height=22,
            kingpin_dia=3, axle_dia=5, arm_length=22, arm_dia=6,
            wheelbase=250, kingpin_track=180)}},
        _box((0.014, 0.010, 0.022), (0.0, 0.0, 0.0)),                          # 1
        _bind(1, {"size": {"x": "=body_length", "y": "=body_width",
                           "z": "=body_height"}}),
        # The steering arm, as the capsule between the kingpin axis and the
        # tie-rod ball. Where that ball sits is not a style choice: it is the
        # Ackermann construction, and the expression is the construction.
        _sweep([[0.0, 0.0, -0.008], [-0.0207, 0.007452, -0.008]], 0.003),      # 3
        _bind(3, {"radius": "=arm_dia/2",
                  "path": [[0.0, 0.0, "=-body_height/2+arm_dia/2"],
                           [_ARM_BACK.replace("=", "=-", 1), _ARM_IN,
                            "=-body_height/2+arm_dia/2"]]}),
        _cyl(0.0015, 0.100, (0.0, 0.0, 0.0), operation="subtract"),            # 5
        _bind(5, {"radius": "=kingpin_dia/2", "height": "=body_height*4"}),
        _cyl(0.0025, 0.100, (0.0, 0.0, 0.0), axis=(0.0, 1.0, 0.0),
             operation="subtract"),                                            # 7
        _bind(7, {"radius": "=axle_dia/2", "height": "=body_width*8"}),
        _mech("shaft", "Kingpin", axis={"x": 0.0, "y": 0.0, "z": 1.0},
              designation="3 mm", text="the knuckle pivots about this"),
        _mech("shaft", "Stub axle", axis={"x": 0.0, "y": 1.0, "z": 0.0},
              direction="cw", designation="5 mm"),
        _mech("bearing", "Kingpin bearing", designation="623",
              text="3x10x4, or bush it"),
    ],
)


TEMPLATES: Dict[str, Template] = {
    t.key: t for t in (_PLATE, _BRACKET, _ENCLOSURE, _SHAFT,
                       _RC_AXLE, _RC_DIFF, _RC_DOGBONE, _RC_UJOINT,
                       _RC_GEARBOX_PLATE, _RC_MOTOR_MOUNT, _RC_SERVO_TRAY,
                       _RC_KNUCKLE)
}


def by_key(key: str) -> Optional[Template]:
    return TEMPLATES.get(str(key).strip().lower())


def describe() -> List[dict]:
    """The gallery listing: what each one is, and what it will let you change."""
    return [{"key": t.key, "name": t.name, "summary": t.summary,
             "tags": list(t.tags), "steps": len(t.steps),
             "parameters": [{"name": p["name"], "value": float(p["expr"]), "unit": p["unit"]}
                            for p in t.parameters]}
            for t in TEMPLATES.values()]


def apply(key: str, doc_id: str) -> dict:
    """Run a template's operation sequence into a document.

    Returns what was built. A step that fails stops the sequence — `replay`
    breaks on the first non-ok envelope — and the failure is reported with the
    op that produced it rather than as a generic "template failed", because the
    only useful thing to say about a broken template is which step broke.
    """
    from app.services import decompiler

    template = TEMPLATES[str(key).strip().lower()]
    envelopes = decompiler.replay(template.steps, doc_id)
    failed = next((i for i, e in enumerate(envelopes) if not e.get("ok")), None)
    if failed is not None:
        error = envelopes[failed].get("error") or {}
        return {"ok": False, "failed_step": failed,
                "failed_op": template.steps[failed]["op"],
                "error": error.get("message", "the step did not succeed")}
    created = [e.get("result", {}).get("feature_id") for e in envelopes]
    return {"ok": True, "template": template.key, "name": template.name,
            "steps": len(envelopes),
            "feature_ids": [c for c in created if c],
            "parameters": [p["name"] for p in template.parameters]}
