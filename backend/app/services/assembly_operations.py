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

_MATE_TYPES = ["fasten", "coincident", "concentric", "distance"]


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
           "`coincident` mates a face of each along an axis (stacking), "
           "`concentric` makes them coaxial, `distance` holds a gap, `fasten` "
           "attaches rigidly at an offset. Faces are the part's bounding-box "
           "extremes along the axis, not individual model faces, and mates keep "
           "parts axis-aligned.",
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
                          "description": "gap held between the two faces"}},
           subject="assembly")
def _add_mate(asm_id, args):
    asm = _assembly(asm_id)
    base, moved = require(args, "base"), require(args, "moved")
    if base == moved:
        raise BadArgument("a mate needs two different components", arg="moved")
    _component(asm, base)
    _component(asm, moved)

    before = next((c.transform.translate.model_dump()
                   for c in asm.components if c.id == moved), None)
    updated = assembly_service.add_mate(asm_id, {
        "id": "", "type": require(args, "type"), "base": base, "moved": moved,
        "axis": args.get("axis", "z"),
        "base_face": args.get("base_face", "max"),
        "moved_face": args.get("moved_face", "min"),
        "offset_mm": float(args.get("offset_mm", 0.0)),
    })
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")

    after = next((c.transform.translate.model_dump()
                  for c in updated.components if c.id == moved), None)
    mate = updated.mates[-1]
    result: Dict[str, Any] = {"message": f"mated {moved} to {base}", "mate_id": mate.id,
                              "mate_count": len(updated.mates)}
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
