"""An assembly as SDFormat — the joints URDF made us decline.

**The file name is `sdformat`, not `sdf`, and that is not fussiness.** This
codebase's `sdf` is the signed *distance field* that the whole kernel evaluates
(`cad_kernel/evaluator/sdf.py`). Gazebo's Simulation Description Format shares
the acronym and nothing else, and a module called `sdf` next to a kernel called
`sdf` is a mis-import waiting to become a very confusing bug.

`export_urdf` exists and is honest about what URDF cannot say. Two of those
refusals are **format** limits rather than facts about the assembly, and
SDFormat does not share them:

- **A ball mate becomes a real `ball` joint.** URDF has no spherical joint, so
  it emits `fixed` and reports the lost degrees of freedom. SDFormat has one.
- **A closed loop is expressible.** URDF is a tree, so the second mate onto a
  component has no representation and is named as unsupported. An SDFormat model
  is a *graph*: the second joint is simply another joint.

It is not a strict improvement, and the places it is worse are reported rather
than smoothed over:

- **SDFormat has no `planar` joint**, where URDF does. That one inverts.
- **SDFormat has no cylindrical joint either.** The tempting map is `screw`, and
  it is wrong: a screw *couples* rotation and translation through a pitch, while
  a cylindrical mate leaves them independent. That is the same class of lie as
  emitting a joint with fewer degrees of freedom, so it is declined and said.

**The frames are simpler here, and that is the format's doing.** A URDF joint's
frame *is* the child link frame, which is why `urdf.export_urdf` composes
`F_base . offset . F_moved⁻¹` to find where a child's origin lands. An SDFormat
link carries its own `<pose>` in the model frame, so each component is written
where the assembly actually puts it, and the joint only has to say where its
anchor sits relative to the child. The mate axis is then the anchor frame's own
z by construction, which is why `<xyz>` is a constant here and a column read
there.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple
from xml.sax.saxutils import escape, quoteattr

import numpy as np

from app.services import assembly_service
from app.services import mass_properties as part_mass
from app.services.urdf import _rpy, _safe, _spin, link_names_for

MM = 1000.0

#: Our mate type -> the SDFormat joint that means the same thing.
#:
#: `revolute` maps to `revolute` rather than to URDF's `continuous` dodge: an
#: SDFormat `<limit>` is optional and unbounded by default, so a hinge with no
#: travel stop is expressible without inventing one.
JOINT_TYPES: Dict[str, str] = {
    "fasten": "fixed",
    "coincident": "fixed",
    "concentric": "fixed",
    "distance": "fixed",
    "revolute": "revolute",
    "slider": "prismatic",
    "ball": "ball",
    "parallel": "fixed",
}

#: Why each unmappable type is declined, in the terms of the format rather than
#: of the assembly — a reader has to know whether to change the model or the
#: target.
NO_EQUIVALENT: Dict[str, str] = {
    "cylindrical": ("SDFormat has no cylindrical joint. `screw` is the near miss "
                    "and it couples rotation to translation through a pitch, "
                    "while this mate leaves them independent — emitting it would "
                    "describe a machine that cannot move the way this one does. "
                    "Emitted as fixed."),
    "planar": ("SDFormat has no planar joint, though URDF does — export_urdf "
               "represents this mate exactly. Emitted as fixed."),
}


def _pose(origin, rotation) -> str:
    r, p, y = _rpy(rotation)
    return (f"{origin[0]:.9g} {origin[1]:.9g} {origin[2]:.9g} "
            f"{r:.9g} {p:.9g} {y:.9g}")


def _component_placement(component) -> Tuple[np.ndarray, np.ndarray]:
    """Where a component sits in assembly space, as (translation, rotation)."""
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    t = component.transform
    rotation = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
    return np.array([t.translate.x, t.translate.y, t.translate.z], dtype=float), rotation


def _link_xml(name: str, placement, mass_kg: float, cog_m, inertia_si,
              mesh_url: Optional[str]) -> str:
    translation, rotation = placement
    ixx, ixy, ixz = inertia_si[0]
    _, iyy, iyz = inertia_si[1]
    _, _, izz = inertia_si[2]
    out = [f'    <link name={quoteattr(name)}>']
    # The component's own placement, which an SDFormat link may carry and a URDF
    # link may not — there, position is whatever the joint tree implies.
    out.append(f"      <pose>{_pose(translation, rotation)}</pose>")
    out.append("      <inertial>")
    out.append(f'        <pose>{cog_m[0]:.9g} {cog_m[1]:.9g} {cog_m[2]:.9g} 0 0 0</pose>')
    out.append(f"        <mass>{mass_kg:.9g}</mass>")
    out.append("        <inertia>")
    out.append(f"          <ixx>{ixx:.9g}</ixx><ixy>{ixy:.9g}</ixy><ixz>{ixz:.9g}</ixz>")
    out.append(f"          <iyy>{iyy:.9g}</iyy><iyz>{iyz:.9g}</iyz><izz>{izz:.9g}</izz>")
    out.append("        </inertia>")
    out.append("      </inertial>")
    if mesh_url:
        for tag in ("visual", "collision"):
            out.append(f'      <{tag} name={quoteattr(f"{name}_{tag}")}>')
            out.append(f"        <geometry><mesh><uri>{escape(mesh_url)}</uri></mesh></geometry>")
            out.append(f"      </{tag}>")
    out.append("    </link>")
    return "\n".join(out)


def export_sdf(asm_id: str, *, name: Optional[str] = None,
               mesh_urls: bool = True) -> Dict[str, Any]:
    """The assembly as an SDFormat model, plus what it could not represent."""
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise KeyError(asm_id)
    if not asm.components:
        raise ValueError("an empty assembly has no links to write")

    frames = assembly_service.connector_frames(asm)
    placements = {c.id: _component_placement(c) for c in asm.components}
    link_names = link_names_for(asm)

    warnings: List[str] = []
    unsupported: List[Dict[str, str]] = []
    driven: List[Dict[str, Any]] = []

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
            warnings.append(f"{label}: zero mass — assign a material to the part, "
                            f"or the solver will treat this link as singular")
        links.append(_link_xml(label, placements[component.id], mass_kg, cog_m,
                               inertia_si,
                               component.mesh_url if mesh_urls else None))

    joints: List[str] = []
    for mate in asm.mates:
        if mate.base not in link_names or mate.moved not in link_names:
            continue
        joint_type = JOINT_TYPES.get(mate.type)
        if joint_type is None:
            unsupported.append({"mate_id": mate.id, "type": mate.type,
                                "reason": NO_EQUIVALENT.get(
                                    mate.type, f"SDFormat has no {mate.type} joint. "
                                               f"Emitted as fixed.")})
            joint_type = "fixed"

        base_component = next(c for c in asm.components if c.id == mate.base)
        moved_component = next(c for c in asm.components if c.id == mate.moved)
        base_frame = (frames.get(base_component.part_id) or {}).get(
            getattr(mate, "base_connector", None) or "")
        moved_frame = (frames.get(moved_component.part_id) or {}).get(
            getattr(mate, "moved_connector", None) or "")

        anchor_origin, anchor_rotation = np.zeros(3), np.eye(3)
        if base_frame is not None:
            base_origin, base_rotation = base_frame
            t_base, r_base = placements[mate.base]
            angle = float(getattr(mate, "angle_deg", 0.0) or 0.0)
            # A driven angle is joint *state* on an articulated joint; only a
            # fixed joint, which has nowhere to carry state, takes it into the
            # anchor. Same rule as the URDF writer, same reason.
            spin = _spin(angle if joint_type == "fixed" else 0.0)
            anchor_rotation = r_base @ base_rotation @ spin
            anchor_origin = r_base @ base_origin + t_base
            if angle and joint_type != "fixed":
                driven.append({"joint": _safe(mate.id, f"joint_{len(joints)}"),
                               "mate_id": mate.id, "type": mate.type,
                               "sdf_type": joint_type, "angle_deg": angle,
                               "note": ("the pose is this joint's zero; the angle "
                                        "is joint state, not part of the model")})
        elif joint_type not in ("fixed",):
            warnings.append(
                f"{link_names[mate.moved]}: {mate.type} joint with no resolvable "
                f"connector pair — its anchor defaults to the child's origin")

        # The joint pose is expressed **relative to the child link**, so the
        # anchor is carried out of assembly space and into the child's frame.
        t_moved, r_moved = placements[mate.moved]
        local_rotation = r_moved.T @ anchor_rotation
        local_origin = r_moved.T @ (anchor_origin - t_moved)

        label = _safe(mate.id, f"joint_{len(joints)}")
        body = [
            f'    <joint name={quoteattr(label)} type={quoteattr(joint_type)}>',
            f"      <parent>{escape(link_names[mate.base])}</parent>",
            f"      <child>{escape(link_names[mate.moved])}</child>",
            f'      <pose relative_to={quoteattr(link_names[mate.moved])}>'
            f"{_pose(local_origin, local_rotation)}</pose>",
        ]
        if joint_type in ("revolute", "prismatic"):
            # The mate axis is the anchor frame's own z, and the pose above put
            # the joint frame *on* that anchor — so the axis is a constant here
            # rather than a column read out of a rotation.
            body.append("      <axis><xyz>0 0 1</xyz></axis>")
        body.append("    </joint>")
        joints.append("\n".join(body))

    model = _safe(name or asm.name, "model")
    xml = "\n".join([
        '<?xml version="1.0"?>',
        f"<!-- Generated from Forge assembly {escape(asm_id)} -->",
        '<sdf version="1.9">',
        f"  <model name={quoteattr(model)}>",
        *links,
        *joints,
        "  </model>",
        "</sdf>",
        "",
    ])
    return {"sdf": xml, "model": model, "links": len(links), "joints": len(joints),
            "unsupported": unsupported, "driven": driven, "warnings": warnings}
