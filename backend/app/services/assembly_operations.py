"""Assembly operations, on the same catalog the part operations live in.

An agent could model a part and could not compose two: 58 operations, not one of
which inserted a component or added a mate, while the mate solver, the
interference check and the BOM were all implemented, routed and tested. The
copilot, the MCP server and every model driving this system stopped at the part.

The id an operation is given now names either a part document or an assembly,
declared per operation as `subject` and published in the catalog — so a caller
knows which id to pass rather than discovering it from a 404, and
`observation.summarize_assembly` gives it something to reason about that is not
a feature tree.

**Creating** an assembly is deliberately not here, for the same reason creating
a *document* is not: both are `POST` on a collection, not an edit of a thing
that exists. Adding one to the catalog would be the only operation whose id
argument means nothing.
"""

from __future__ import annotations

from typing import Any, Dict

from app.models.schemas import ComponentTransform, Vector3
from app.services import assembly_service
from app.services.cad_operations import operation
from app.services.op_errors import BadArgument, NoSuchDocument, PreconditionFailed, require

MM = 1000.0

_COMPONENT = {"type": "string", "required": True,
              "description": "component id, from list_components"}

# Imported rather than restated: the enum a constrained decoder reads and
# the solver's own freedom table must name the same set, or a mate type can
# be accepted by the schema and have no degrees of freedom defined for it.
from app.services.assembly_mates import (  # noqa: E402
    CONNECTOR_TYPES as _CONNECTOR_TYPES,
    MATE_TYPES as _MATE_TYPES_TUPLE,
)

_MATE_TYPES = list(_MATE_TYPES_TUPLE)


def _assembly(asm_id: str):
    asm = assembly_service.get_assembly(asm_id)
    if asm is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    return asm


def _component(asm, component_id: str):
    found = next((c for c in asm.components if c.id == component_id), None)
    if found is None:
        known = ", ".join(c.id for c in asm.components) or "none"
        raise BadArgument(
            f"no component {component_id!r} in this assembly; it has: {known}",
            arg="component_id")
    return found


def _has_connector(part_id: str, connector_id: str) -> bool:
    from app.cad_kernel.core.feature_tree import walk_nodes
    from app.services import document_service

    part = document_service.get_document(part_id)
    if part is None:
        return False
    return any(n.get("type") == "MateConnectorFeature" and n.get("id") == connector_id
               for n in walk_nodes(part.feature_tree))


@operation("dof", "query",
           "What each component can still move. A free body has six degrees of "
           "freedom; each mate removes what its type removes — a fasten all six, "
           "a revolute five, a slider five, a planar three. A component at zero "
           "is fully located; one whose mates remove more than six is "
           "over-constrained, which two individually valid mates can be jointly. "
           "This is a property of the constraints, not of where the solver "
           "happened to put things.",
           {}, subject="assembly")
def _dof(asm_id, args):
    from app.services import assembly_mates

    asm = _assembly(asm_id)
    return assembly_mates.degrees_of_freedom(asm.components, asm.mates)


# Not `mass_properties`: that name is already the part-level operation, and the
# catalog is keyed by name. `mass_budget` is also what an airframe actually
# calls this — the sheet you check before you fly.
@operation("mass_budget", "query",
           "All-up weight, centre of gravity and inertia tensor for the whole "
           "assembly, in assembly space — the three numbers an airframe, a drone "
           "or a robot is checked against before it flies or moves. Each part's "
           "own centre of mass is rotated and translated by its placement first, "
           "and the inertia tensors are rotated into assembly axes and shifted "
           "by the parallel-axis theorem. Components are listed by how far they "
           "move the balance, which is not the same order as by weight: a light "
           "part on a long arm outvotes a heavy one at the centre.",
           {}, subject="assembly")
def _assembly_mass_properties(asm_id, args):
    from app.services import assembly_mass

    _assembly(asm_id)
    return assembly_mass.assembly_mass_properties(asm_id)


@operation("export_urdf", "query",
           "The assembly as URDF, for a robotics simulator. Connector mates "
           "become real joints — revolute, prismatic, planar — and bounding-box "
           "mates become fixed, because a face-on-face placement is not an "
           "articulation. Says what it could not represent rather than quietly "
           "dropping it: URDF is a tree, so a closed loop has no representation, "
           "and it has no cylindrical or spherical joint.",
           {"name": {"type": "string",
                     "description": "robot name in the file; defaults to the "
                                    "assembly's"},
            "mesh_urls": {"type": "boolean", "default": True,
                          "description": "reference each link's mesh for visual "
                                         "and collision geometry"}},
           subject="assembly")
def _export_urdf(asm_id, args):
    from app.services import urdf

    _assembly(asm_id)
    try:
        return urdf.export_urdf(asm_id, name=args.get("name"),
                                mesh_urls=bool(args.get("mesh_urls", True)))
    except ValueError as exc:
        raise PreconditionFailed(str(exc)) from exc


@operation("list_components", "query",
           "The parts placed in this assembly: id, which part document each "
           "references, where it sits (mm) and how big it is. A component whose "
           "part was deleted is reported as missing rather than omitted.",
           {}, subject="assembly")
def _list_components(asm_id, args):
    asm = _assembly(asm_id)
    return {"components": [
        {"id": c.id, "part_id": c.part_id, "name": c.name, "missing": c.missing,
         "position_mm": {"x": round(c.transform.translate.x * MM, 3),
                         "y": round(c.transform.translate.y * MM, 3),
                         "z": round(c.transform.translate.z * MM, 3)}}
        for c in asm.components]}


@operation("insert_part", "mutate",
           "Place a part document into this assembly as a component. The part "
           "keeps its own feature tree — editing the part changes every place it "
           "is used, which is what makes an assembly a reference rather than a copy.",
           {"part_id": {"type": "string", "required": True,
                        "description": "the part document to place"}},
           subject="assembly")
def _insert_part(asm_id, args):
    part_id = require(args, "part_id")
    try:
        updated = assembly_service.add_component(asm_id, part_id)
    except ValueError as exc:
        # The service refuses a part that does not exist *or* that this
        # assembly's owner cannot open, and both are the same answer here: the
        # id names nothing this caller can place. Re-raised with the argument
        # attached, because "part not found" without a field name is what the
        # REST route has always said and what an agent cannot act on.
        raise BadArgument(f"no part document {part_id!r} ({exc})", arg="part_id") from exc
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    placed = updated.components[-1]
    return {"message": f"inserted {placed.name}", "component_id": placed.id,
            "component_count": len(updated.components)}


@operation("insert_standard_part", "mutate",
           "Place a purchased part without modelling it — a screw, nut, washer, "
           "dowel pin, bearing, stepper or T-slot extrusion from the published "
           "dimensional standards, or a brushless motor, flight controller, "
           "4-in-1 ESC, RC servo, hex standoff, propeller, carbon plate or tube "
           "or LiPo pack from the settled conventions of RC, drone and robotics "
           "work. Every one arrives as an ordinary editable part, not an "
           "imported blob. Browse the keys with list_standard_parts. Inserting "
           "the same key twice reuses one part document, so the BOM says qty 2 "
           "rather than listing it twice. Where a dimension varies by "
           "manufacturer it is a parameter rather than a table row — pass those "
           "in `params`; a row's listing says which it takes and what they "
           "default to.",
           {"key": {"type": "string", "required": True,
                    "description": "catalog key, e.g. iso4762-m4-16, bearing-608 "
                                   "or bldc-2207"},
            "quantity": {"type": "integer", "default": 1,
                         "description": "how many to place"},
            # Deliberately an opaque object: which names are valid depends on
            # the value of `key`, which a static JSON Schema cannot say. The
            # handler checks them against that row's declared parameters, which
            # is exact where a schema here could only be a weaker second
            # definition — and an unknown name is refused rather than dropped,
            # which is the same rule `additionalProperties: false` enforces
            # everywhere else on this surface.
            "params": {"type": "object",
                       "description": "values for the parameters this catalog "
                                      "row declares, in the units its names "
                                      "carry (…_mm millimetres, …_in inches, "
                                      "mass_g grams). Omitted parameters take "
                                      "the row's default. A row placed with "
                                      "different parameters is a different "
                                      "part and gets its own BOM line"}},
           subject="assembly")
def _insert_standard_part(asm_id, args):
    from app.services import parts_catalog, standard_parts, supplier_parts

    asm = _assembly(asm_id)
    key = require(args, "key")
    quantity = int(args.get("quantity", 1) or 1)
    if not 1 <= quantity <= 64:
        raise BadArgument("quantity must be between 1 and 64", arg="quantity")
    params = args.get("params")
    if params is not None and not isinstance(params, dict):
        raise BadArgument("params must be an object of parameter values",
                          arg="params")

    # One `key` covers both halves of the catalog. A supplier part is resolved
    # against the *assembly owner's* imports, not the caller's, for the same
    # reason `document_for` creates under that account: `add_component` refuses
    # a part the assembly's owner cannot open.
    if str(key).startswith(supplier_parts.PREFIX):
        row = supplier_parts.by_key(key, asm.owner_id)
        if row is None:
            raise BadArgument(
                f"no imported part {key!r} on this account; list them with "
                f"list_standard_parts", arg="key")
        designation = row["designation"]
    else:
        entry = parts_catalog.by_key(key)
        if entry is None:
            raise BadArgument(
                f"no catalog part {key!r}; browse them with list_standard_parts",
                arg="key")
        try:
            designation = parts_catalog.designation_of(entry, params)
        except ValueError as exc:
            raise BadArgument(str(exc), arg="params") from exc

    try:
        part_id = standard_parts.document_for_key(key, owner_id=asm.owner_id,
                                                  params=params)
    except ValueError as exc:
        # An unknown parameter name, a value outside the row's range, or a pair
        # that cannot both be true (a tube bored wider than itself). All three
        # are the caller's arguments rather than the catalog's state.
        raise BadArgument(str(exc), arg="params") from exc
    if part_id is None:
        raise BadArgument(f"catalog part {key!r} has no document", arg="key")
    placed = []
    for _ in range(quantity):
        updated = assembly_service.add_component(asm_id, part_id)
        placed.append(updated.components[-1].id)
    return {"message": f"placed {quantity} × {designation}",
            "part_id": part_id, "component_ids": placed,
            "designation": designation,
            "component_count": len(updated.components)}


@operation("remove_component", "mutate", "Take a component out of the assembly.",
           {"component_id": _COMPONENT}, subject="assembly")
def _remove_component(asm_id, args):
    component_id = require(args, "component_id")
    _component(_assembly(asm_id), component_id)
    updated = assembly_service.remove_component(asm_id, component_id)
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    return {"message": "removed", "component_count": len(updated.components)}


@operation("place_component", "mutate",
           "Position a component by hand. A component positioned by a mate is "
           "placed by the solver instead, and writing a position here would be "
           "overwritten on the next solve — mate it or place it, not both.",
           {"component_id": _COMPONENT,
            "translate": {"type": "object", "description": "position of the part's origin",
                          "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                         "z": {"type": "number"}},
                          "additionalProperties": False},
            "rotate": {"type": "object", "description": "XYZ euler angles",
                       "properties": {"x": {"type": "number"}, "y": {"type": "number"},
                                      "z": {"type": "number"}},
                       "additionalProperties": False},
            "scale": {"type": "number", "default": 1.0,
                      "description": "uniform scale factor"}},
           subject="assembly")
def _place_component(asm_id, args):
    asm = _assembly(asm_id)
    component_id = require(args, "component_id")
    component = _component(asm, component_id)

    if any(m.moved == component_id for m in asm.mates):
        raise PreconditionFailed(
            f"{component.name} is positioned by a mate; remove the mate before "
            f"placing it by hand, or the next solve will overwrite this")

    current = component.transform
    translate = args.get("translate")
    rotate = args.get("rotate")
    transform = ComponentTransform(
        translate=Vector3(**{**current.translate.model_dump(), **(translate or {})}),
        rotate_deg=Vector3(**{**current.rotate_deg.model_dump(), **(rotate or {})}),
        scale=float(args.get("scale", current.scale) or 1.0),
    )
    updated = assembly_service.update_component(asm_id, component_id, transform)
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    return {"message": f"placed {component.name}", "component_id": component_id}


@operation("list_mates", "query", "The constraints holding this assembly together.",
           {}, subject="assembly")
def _list_mates(asm_id, args):
    asm = _assembly(asm_id)
    return {"mates": [m.model_dump() for m in asm.mates]}


@operation("add_mate", "mutate",
           "Constrain one component against another and re-solve every placement. "
           "Two schemes. Without connectors the mate reads the parts' "
           "bounding-box extremes along one world axis: `coincident` stacks a "
           "face on a face, `concentric` makes them coaxial, `distance` holds a "
           "gap, `fasten` attaches rigidly — and the parts stay axis-aligned, "
           "because an extreme cannot express a rotation. With "
           "`base_connector`/`moved_connector` (ids from list_mate_connectors) "
           "the mate aligns two frames that live *in* the parts, which is the "
           "only way a part gets rotated into place: `revolute` is a hinge, "
           "`slider` a rail, `cylindrical` a shaft in a bearing, plus `planar`, "
           "`ball` and `parallel`. Those six need a connector on each side. Use "
           "`dof` afterwards to see what is still free.",
           {"type": {"enum": _MATE_TYPES, "required": True,
                     "description": "which constraint to apply"},
            "base": {"type": "string", "required": True,
                     "description": "component held fixed"},
            "moved": {"type": "string", "required": True,
                      "description": "component the mate positions"},
            "axis": {"enum": ["x", "y", "z"], "default": "z",
                     "description": "the axis the constraint acts along"},
            "base_face": {"enum": ["min", "center", "max"], "default": "max",
                          "description": "which extreme of the base part to mate to"},
            "moved_face": {"enum": ["min", "center", "max"], "default": "min",
                           "description": "which extreme of the moved part meets it"},
            "offset_mm": {"type": "number", "default": 0.0, "unit": "mm",
                          "description": "gap held between the two faces, or "
                                         "along the connectors' shared axis"},
            "base_connector": {"type": "string",
                               "description": "mate connector on the base part, "
                                              "from list_mate_connectors"},
            "moved_connector": {"type": "string",
                                "description": "mate connector on the moved part"}},
           subject="assembly")
def _add_mate(asm_id, args):
    asm = _assembly(asm_id)
    base, moved = require(args, "base"), require(args, "moved")
    if base == moved:
        raise BadArgument("a mate needs two different components", arg="moved")
    base_component = _component(asm, base)
    moved_component = _component(asm, moved)

    mate_type = require(args, "type")
    base_connector = args.get("base_connector")
    moved_connector = args.get("moved_connector")
    if mate_type in _CONNECTOR_TYPES and not (base_connector and moved_connector):
        raise BadArgument(
            f"a {mate_type} mate aligns two frames and needs "
            f"'base_connector' and 'moved_connector' — add them to the parts "
            f"with add_mate_connector, or use one of "
            f"{', '.join(sorted(set(_MATE_TYPES) - _CONNECTOR_TYPES))} instead, "
            f"which read the bounding box.",
            arg="base_connector" if not base_connector else "moved_connector")
    # Checked here rather than at solve time: a connector id that names nothing
    # leaves the solver on the bounding-box path, which places the part
    # plausibly and wrongly. The one moment it is cheap to catch is now.
    for side, component, connector in (("base", base_component, base_connector),
                                       ("moved", moved_component, moved_connector)):
        if connector and not _has_connector(component.part_id, connector):
            raise BadArgument(
                f"the {side} part has no mate connector {connector!r} — "
                f"see list_mate_connectors on that document",
                arg=f"{side}_connector")

    before = next((c.transform.translate.model_dump()
                   for c in asm.components if c.id == moved), None)
    updated = assembly_service.add_mate(asm_id, {
        "id": "", "type": mate_type, "base": base, "moved": moved,
        "axis": args.get("axis", "z"),
        "base_face": args.get("base_face", "max"),
        "moved_face": args.get("moved_face", "min"),
        "offset_mm": float(args.get("offset_mm", 0.0)),
        "base_connector": base_connector,
        "moved_connector": moved_connector,
    })
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")

    after = next((c.transform.translate.model_dump()
                  for c in updated.components if c.id == moved), None)
    mate = updated.mates[-1]
    from app.services import assembly_mates

    rotational, translational = assembly_mates.FREEDOMS.get(mate.type, (0, 0))
    result: Dict[str, Any] = {"message": f"mated {moved} to {base}", "mate_id": mate.id,
                              "mate_count": len(updated.mates),
                              "removes_dof": assembly_mates.removed(mate.type),
                              "leaves_free": {"rotational": rotational,
                                              "translational": translational}}
    # A mate that positions nothing differently is the assembly-side version of
    # an edit that reports success and changes nothing. Say so rather than
    # leaving the caller to diff two transforms.
    if before is not None and before == after:
        result["note"] = ("the mate is satisfied where the part already was — "
                          "nothing moved")
    return result


@operation("remove_mate", "mutate",
           "Remove a constraint. The component it positioned keeps the place the "
           "solver last put it and becomes hand-placed again.",
           {"mate_id": {"type": "string", "required": True,
                        "description": "mate id, from list_mates"}},
           subject="assembly")
def _remove_mate(asm_id, args):
    mate_id = require(args, "mate_id")
    updated = assembly_service.remove_mate(asm_id, mate_id)
    if updated is None:
        raise BadArgument(f"no mate {mate_id!r} in this assembly", arg="mate_id")
    return {"message": "removed", "mate_count": len(updated.mates)}


@operation("check_interference", "query",
           "Do any two parts occupy the same space, and which pairs sit closer "
           "than a clearance. Run this before exporting: parts that interpenetrate "
           "look correct in a viewport. Surfaces are sampled rather than solved, "
           "so a very thin overlap can be missed.",
           {"clearance_mm": {"type": "number", "default": 1.0, "unit": "mm",
                             "description": "pairs closer than this are reported as warnings"}},
           subject="assembly")
def _check_interference(asm_id, args):
    from app.services import interference

    _assembly(asm_id)
    return interference.check_assembly(
        asm_id, clearance_mm=float(args.get("clearance_mm", 1.0)))


@operation("bill_of_materials", "query",
           "The parts list: one line per distinct part with quantity, material, "
           "mass and material cost. Cost is mass x price per kilo — it is not a quote.",
           {}, subject="assembly")
def _bill_of_materials(asm_id, args):
    from app.services import bom

    _assembly(asm_id)
    return bom.bill_of_materials(asm_id)
