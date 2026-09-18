"""An assembly as URDF — the file a robotics simulator actually reads.

This is the payoff of mate connectors. A URDF link needs a mass, a centre of
mass and an inertia tensor; a URDF joint needs two frames and an axis. Until
`MateConnectorFeature` existed there were no frames to write — the four
bounding-box mates cannot express a rotation, so every joint would have been
`fixed` and the file would have described a statue.

**Only connector mates become joints.** A bounding-box mate says "this face sits
on that face along Z", which is a *placement*, not an articulation, so it is
emitted as `fixed`. That is not a limitation being worked around: it is what
those mates mean.

**URDF is a tree, and an assembly is not necessarily one.** A closed chain — a
four-bar linkage, a differential — has no URDF representation at all, and the
right answer is to say which mate closes the loop rather than to drop it and
emit a file that silently describes a different machine. Components no mate
reaches are emitted as links fixed to the base, because a payload bolted on
without a mate is still mass the simulator has to carry.

**Two of our six kinematic types have no URDF equivalent.** URDF has revolute,
continuous, prismatic, fixed, floating and planar; it has no cylindrical and no
spherical. Those are reported as unsupported and emitted `fixed`, with the
substitution named in the result — a joint that silently loses a degree of
freedom is a robot that behaves differently in simulation than the one on the
bench, which is the single failure this whole file exists to avoid.

**A driven `angle_deg` is a joint state on an articulated joint and part of the
geometry on a fixed one, and it is written to whichever of those it is.** A
URDF joint's `<origin>` is its *zero* configuration; baking a hinge's current
angle into it would move the zero of the machine every time somebody swung the
door in the viewport, and the simulator commanding q=0 would then get the door
open. So for `continuous`, `prismatic` and `planar` the origin stays at the
connectors' alignment and the angle is reported in `driven`, which is where a
caller writing a `joint_states` message finds it. A `cylindrical` or `ball` mate
is emitted `fixed` and a fixed joint has no state to carry anything, so there
the angle **is** baked in — dropping it would put the part somewhere the
assembly says it is not, permanently and with no way to correct it downstream.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from app.services import assembly_mass, assembly_service, document_service
from app.services import mass_properties as part_mass

MM = 1000.0

#: Our mate type -> the URDF joint that means the same thing.
#: `revolute` becomes `continuous` because we carry no joint limits; a URDF
#: `revolute` *requires* a `<limit>` and inventing one would put a travel stop
#: in the simulation that does not exist on the machine.
JOINT_TYPES: Dict[str, str] = {
    "fasten": "fixed",
    "coincident": "fixed",
    "concentric": "fixed",
    "distance": "fixed",
    "revolute": "continuous",
    "slider": "prismatic",
    "planar": "planar",
    "parallel": "fixed",
    # No URDF equivalent. Emitted fixed and named in `unsupported`.
    "cylindrical": None,
    "ball": None,
}

#: Prismatic joints require a limit in URDF. This is a stated default, not a
#: measured one, and the result says so — a travel stop nobody chose is still a
#: travel stop the simulator will honour.
DEFAULT_PRISMATIC_TRAVEL_M = 1.0


def _safe(name: Optional[str], fallback: str) -> str:
    """A URDF-legal identifier. Names come from users and end up in XML."""
    cleaned = "".join(ch if (ch.isalnum() or ch in "_-") else "_"
                      for ch in (name or "").strip())
    return cleaned or fallback


def _rpy(rotation: np.ndarray) -> Tuple[float, float, float]:
    """URDF's fixed-axis roll-pitch-yaw, which is `Rz @ Ry @ Rx` — the same
    convention `sdf._euler_matrix` builds, so this is `assembly_mates`'
    inverse in radians rather than a second derivation."""
    from app.services.assembly_mates import _euler_degrees

    return tuple(math.radians(v) for v in _euler_degrees(rotation))     # type: ignore[return-value]


def _spin(angle_deg: float) -> np.ndarray:
    """The mate's driven rotation about its own axis, in the base frame.

    The same rotation `assembly_mates._spin` composes. Imported rather than
    re-derived, so a change to the joint convention cannot land in one file and
    not the other.
    """
    from app.services.assembly_mates import _spin as spin

    return spin(angle_deg)


def _frames(asm) -> Dict[str, Dict[str, Any]]:
    return assembly_service.connector_frames(asm)


def link_names_for(asm) -> Dict[str, str]:
    """Component id -> the link name every robot-description exporter must use.

    Derived once because three files now describe the same assembly — URDF,
    SDFormat and SRDF — and two transcriptions of a naming rule agree on the day
    they are written. After that a URDF and its SRDF disagree about which link
    is `arm_2`, and the SRDF plans against a link that is not there: a failure
    with no error message anywhere, which is the kind this codebase keeps
    single-sourcing to avoid.
    """
    names: Dict[str, str] = {}
    used: set = set()
    for index, component in enumerate(asm.components):
        base = _safe(component.name, f"link_{index}")
        candidate, suffix = base, 2
        while candidate in used:
            candidate, suffix = f"{base}_{suffix}", suffix + 1
        used.add(candidate)
        names[component.id] = candidate
    return names


def _link_xml(name: str, mass_kg: float, cog_m, inertia_si, mesh_url: Optional[str]) -> str:
    ixx, ixy, ixz = inertia_si[0]
    _, iyy, iyz = inertia_si[1]
    _, _, izz = inertia_si[2]
    out = [f'  <link name={quoteattr(name)}>']
    out.append("    <inertial>")
    out.append(f'      <origin xyz="{cog_m[0]:.9g} {cog_m[1]:.9g} {cog_m[2]:.9g}" rpy="0 0 0"/>')
    out.append(f'      <mass value="{mass_kg:.9g}"/>')
    out.append(f'      <inertia ixx="{ixx:.9g}" ixy="{ixy:.9g}" ixz="{ixz:.9g}" '
               f'iyy="{iyy:.9g}" iyz="{iyz:.9g}" izz="{izz:.9g}"/>')
    out.append("    </inertial>")
    if mesh_url:
        for tag in ("visual", "collision"):
            out.append(f"    <{tag}>")
            out.append('      <origin xyz="0 0 0" rpy="0 0 0"/>')
            out.append(f'      <geometry><mesh filename={quoteattr(mesh_url)}/></geometry>')
            out.append(f"    </{tag}>")
    out.append("  </link>")
    return "\n".join(out)


def export_urdf(asm_id: str, *, name: Optional[str] = None,
                mesh_urls: bool = True) -> Dict[str, Any]:
    """The assembly as a URDF string, plus what could not be represented."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)
    if not asm.components:
        raise ValueError("an empty assembly has no links to write")

    frames = _frames(asm)
    link_names = link_names_for(asm)

    warnings: List[str] = []
    unsupported: List[Dict[str, str]] = []
    #: Joints whose mate is driven to a non-zero pose. Not a warning and not
    #: unsupported — the URDF is correct, it just describes the zero pose, and
    #: this is the state a caller has to send to see the assembly it exported.
    driven: List[Dict[str, Any]] = []

    # ---- links -------------------------------------------------------------
    links: List[str] = []
    for component in asm.components:
        label = link_names[component.id]
        mass_kg, cog_m, inertia_si = 0.0, np.zeros(3), np.zeros((3, 3))
        if component.missing:
            warnings.append(f"{label}: the part it references is gone; emitted "
                            f"as a massless link")
        else:
            try:
                props = part_mass.mass_properties(component.part_id)
                mass_kg = float(props["mass_kg"])
                cog_m = np.asarray(props["center_of_mass_mm"], dtype=float) / MM
                inertia_si = np.asarray(props["inertia_kg_mm2"], dtype=float) / (MM ** 2)
            except (KeyError, ValueError) as exc:
                warnings.append(f"{label}: {exc}; emitted as a massless link")
        if mass_kg <= 0:
            # A zero-mass link makes most solvers singular the moment it is
            # driven, so this is said rather than left to be discovered.
            warnings.append(f"{label}: zero mass — assign a material to the part, "
                            f"or the solver will treat this link as singular")
        links.append(_link_xml(label, mass_kg, cog_m, inertia_si,
                               component.mesh_url if mesh_urls else None))

    # ---- joints ------------------------------------------------------------
    joints: List[str] = []
    parented: set = set()
    for mate in asm.mates:
        if mate.base not in link_names or mate.moved not in link_names:
            continue
        if mate.moved in parented:
            # URDF is a tree: a link has exactly one parent. A second mate onto
            # the same component closes a loop, which URDF cannot express — so
            # name it rather than drop it and emit a different machine.
            unsupported.append({
                "mate_id": mate.id, "type": mate.type,
                "reason": (f"{link_names[mate.moved]} already has a parent; this "
                           f"mate closes a kinematic loop, which URDF cannot "
                           f"represent. Model the loop in the simulator, or "
                           f"remove one mate."),
            })
            continue

        urdf_type = JOINT_TYPES.get(mate.type)
        if urdf_type is None:
            unsupported.append({
                "mate_id": mate.id, "type": mate.type,
                "reason": (f"URDF has no {mate.type} joint — it offers revolute, "
                           f"continuous, prismatic, fixed, floating and planar. "
                           f"Emitted as fixed, so the simulated machine has "
                           f"fewer degrees of freedom than the real one."),
            })
            urdf_type = "fixed"

        # Where the joint sits, and which way its axis points.
        #
        # URDF's joint frame *is* the child link frame, so the origin has to
        # carry both connectors, not just the base one: the child's own origin
        # sits wherever its connector is not. That is the same composition
        # `assembly_mates._apply_connectors` does, expressed relative to the
        # parent rather than to the world —
        #
        #     child_in_parent = F_base . offset . F_moved⁻¹
        #
        # Emitting `F_base` alone places the child's origin on the base
        # connector, which is right only when the moved connector happens to be
        # at the part origin — a test case, and nothing else.
        origin, axis = np.zeros(3), np.array([0.0, 0.0, 1.0])
        rpy = (0.0, 0.0, 0.0)
        base_component = next(c for c in asm.components if c.id == mate.base)
        moved_component = next(c for c in asm.components if c.id == mate.moved)
        base_frame = (frames.get(base_component.part_id) or {}).get(
            getattr(mate, "base_connector", None) or "")
        moved_frame = (frames.get(moved_component.part_id) or {}).get(
            getattr(mate, "moved_connector", None) or "")

        if base_frame is not None and moved_frame is not None:
            (base_origin, base_rotation) = base_frame
            (moved_origin, moved_rotation) = moved_frame
            # An articulated joint's origin is its zero pose and the driven
            # angle is state; a `fixed` joint has nowhere to put state, so the
            # angle has to go into the geometry or be lost. See the module
            # docstring — this is the one line that decides which.
            angle = float(getattr(mate, "angle_deg", 0.0) or 0.0)
            spin = _spin(angle if urdf_type == "fixed" else 0.0)
            placed = base_rotation @ spin
            rotation = placed @ moved_rotation.T
            origin = (base_origin
                      + placed @ np.array([0.0, 0.0, mate.offset_mm / MM])
                      - rotation @ moved_origin)
            rpy = _rpy(rotation)
            if angle and urdf_type != "fixed":
                driven.append({
                    "joint": _safe(mate.id, f"joint_{len(joints)}"),
                    "mate_id": mate.id, "type": mate.type,
                    "urdf_type": urdf_type,
                    "angle_deg": angle,
                    "note": ("the URDF origin is this joint's zero pose; the "
                             "angle is a joint state, not part of the model"),
                })
            # `<axis>` is expressed in the joint frame, and the joint frame is
            # the child's — so the mate axis reduces to the *moved* connector's
            # own z, which is what makes this a one-column read rather than a
            # change of basis that is easy to get backwards.
            axis = moved_rotation[:, 2]
        elif urdf_type != "fixed":
            warnings.append(
                f"{link_names[mate.moved]}: {mate.type} joint with no resolvable "
                f"connector pair — its frame defaults to the parent's origin and "
                f"its axis to +Z")

        label = _safe(mate.id, f"joint_{len(joints)}")
        joints.append("\n".join([
            f'  <joint name={quoteattr(label)} type={quoteattr(urdf_type)}>',
            f'    <parent link={quoteattr(link_names[mate.base])}/>',
            f'    <child link={quoteattr(link_names[mate.moved])}/>',
            f'    <origin xyz="{origin[0]:.9g} {origin[1]:.9g} {origin[2]:.9g}" '
            f'rpy="{rpy[0]:.9g} {rpy[1]:.9g} {rpy[2]:.9g}"/>',
            *([f'    <axis xyz="{axis[0]:.9g} {axis[1]:.9g} {axis[2]:.9g}"/>']
              if urdf_type in ("continuous", "prismatic", "revolute", "planar") else []),
            *([f'    <limit effort="0" velocity="0" lower="{-DEFAULT_PRISMATIC_TRAVEL_M}" '
               f'upper="{DEFAULT_PRISMATIC_TRAVEL_M}"/>']
              if urdf_type == "prismatic" else []),
            "  </joint>",
        ]))
        parented.add(mate.moved)

    # A component nothing mates is still mass the simulator has to carry, so it
    # is bolted to the first link rather than silently dropped.
    roots = [c.id for c in asm.components if c.id not in parented]
    if len(roots) > 1:
        anchor = roots[0]
        for orphan in roots[1:]:
            joints.append("\n".join([
                f'  <joint name={quoteattr(_safe(None, "fixed_" + link_names[orphan]))}'
                f' type="fixed">',
                f'    <parent link={quoteattr(link_names[anchor])}/>',
                f'    <child link={quoteattr(link_names[orphan])}/>',
                '    <origin xyz="0 0 0" rpy="0 0 0"/>',
                "  </joint>",
            ]))
        warnings.append(
            f"{len(roots) - 1} component(s) had no mate and were fixed to "
            f"{link_names[anchor]} — URDF needs a single connected tree")

    robot = _safe(name or asm.name, "robot")
    xml = "\n".join([
        '<?xml version="1.0"?>',
        f"<!-- Generated from Forge assembly {escape(asm_id)} -->",
        f"<robot name={quoteattr(robot)}>",
        *links,
        *joints,
        "</robot>",
        "",
    ])
    return {
        "urdf": xml,
        "robot": robot,
        "links": len(links),
        "joints": len(joints),
        "unsupported": unsupported,
        "driven": driven,
        "warnings": warnings,
    }
