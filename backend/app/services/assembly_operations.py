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

import numpy as np

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
    DRIVEN_ROTATION as _DRIVEN_ROTATION,
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


@operation("component_interfaces", "query",
           "How one placed component mounts, read off its part's exact geometry "
           "(the part-document reading of inspect_interfaces): shaft diameter and "
           "projection, mounting-hole pattern with candidate screws, can "
           "diameter. The axis and the shaft tip are also given in assembly "
           "space, so a mount can be designed where the part actually sits. Use "
           "it before asking the user for a drawing of an imported part.",
           {"component_id": {"type": "string", "required": True,
                             "description": "the placed component, from the "
                                            "assembly's component list"}},
           subject="assembly")
def _component_interfaces(asm_id, args):
    from app.cad_kernel.evaluator.sdf import _euler_matrix
    from app.services import interfaces

    asm = _assembly(asm_id)
    comp = _component(asm, require(args, "component_id"))
    try:
        reading = interfaces.read_document(comp.part_id)
    except ValueError as exc:
        raise PreconditionFailed(str(exc)) from exc
    t = comp.transform
    scale = float(t.scale or 1.0)
    rotation = _euler_matrix(t.rotate_deg.x, t.rotate_deg.y, t.rotate_deg.z)
    translation = np.array([t.translate.x, t.translate.y, t.translate.z])
    reading["component"] = comp.name
    reading["in_assembly"] = interfaces.placed(reading, rotation, scale, translation)
    if abs(scale - 1.0) > 1e-9:
        reading["note"] = (f"this instance is scaled by {scale:g}; the dimensions above "
                           "are the part's own, not the scaled instance's")
    return reading


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


@operation("build_part", "mutate",
           "**Design a part that does not exist yet, then place it here.** This "
           "is how an assembly gets a part nobody has modelled: it runs the part "
           "copilot on a new document — the same step-by-step operation loop, "
           "against the part catalog — and inserts the result as a component. "
           "Use it for bespoke geometry (a bracket, a housing, a chassis plate); "
           "for anything purchased, insert_standard_part is cheaper and more "
           "accurate than modelling a screw. The `goal` is a self-contained "
           "part description with real millimetre dimensions: say \"a 120 x 80 x 6 mm plate with M4 "
           "clearance holes 10 mm in from each corner\", not \"the base plate\". "
           "The part run receives a focused assembly brief. Name relevant "
           "component_ids for mating interfaces, explicit requirements, and "
           "image_indices for relevant parent reference images (zero-based; "
           "omit to inherit all, [] to inherit none). "
           "The new component still has to be mated: building it places it at "
           "the origin, which is where everything else built this way already "
           "is. Costs one step here and a whole part run inside it.",
           {"goal": {"type": "string", "required": True,
                     "description": "what the part is, with explicit mm dimensions"},
            "name": {"type": "string",
                     "description": "what to call the part document; defaults to "
                                    "the first few words of the goal"},
            "steps": {"type": "number", "default": 8,
                      "description": "step budget for the part's own copilot run"},
            "component_ids": {"type": "array", "items": {"type": "string"}, "maxItems": 8,
                              "description": "existing assembly components this part must fit or mate to"},
            "requirements": {"type": "array", "items": {"type": "string", "maxLength": 1000}, "maxItems": 30,
                             "description": "part-specific dimensions, hole patterns, clearances, material and manufacturing constraints"},
            "image_indices": {"type": "array", "items": {"type": "integer", "minimum": 0, "maximum": 5}, "maxItems": 6,
                              "description": "zero-based indices of relevant parent reference images; [] for none, omit for all"}},
           subject="assembly")
def _build_part(asm_id, args):
    from app.services import copilot, document_service, feature_ops

    asm = _assembly(asm_id)
    goal = str(require(args, "goal")).strip()
    if not goal:
        raise BadArgument("a part needs a description to be built from", arg="goal")
    name = str(args.get("name") or " ".join(goal.split()[:6]))[:80]
    steps = max(1, min(int(args.get("steps", 8)), copilot.MAX_STEPS_CAP))
    from app.services import copilot_context
    import time

    parent = copilot.nested_context() or {}
    brief, reference_images = copilot_context.part_brief(asm, args, parent)
    remaining = parent.get("deadline", time.monotonic() + copilot.MAX_SECONDS_DEFAULT) - time.monotonic()
    if remaining <= 0 or (parent.get("cancelled") and parent["cancelled"]()):
        raise PreconditionFailed("The parent run stopped before this part could be built")

    # Built under the *assembly's* owner, because `add_component` refuses a part
    # its owner cannot open — the same rule `standard_parts.document_for`
    # follows, and for the same reason.
    doc = document_service.create_document(name=name, owner_id=asm.owner_id)
    outcome = copilot.run(doc.id, goal, max_steps=steps, subject="document",
                          meter=copilot.nested_meter(),
                          # The part is designed by the model the build is being
                          # run with. A build planned by one and detailed by
                          # another compares nothing.
                          model=copilot.nested_model(), parent_brief=brief,
                          images=reference_images, max_seconds=min(remaining, copilot.MAX_SECONDS_DEFAULT),
                          cancelled=parent.get("cancelled"), on_action=parent.get("on_action"),
                          # No visual review inside a nested part run: the
                          # assembly's own review shows this part where it ends
                          # up, which is the thing worth looking at, and a
                          # render per sub-run multiplies a whole build's cost.
                          review=False,
                          run_metadata=parent.get("run_metadata"))

    built = document_service.get_document(doc.id)
    if built is None or not feature_ops.has_solid(built.feature_tree):
        # A component whose part has no body is a row in the BOM, a name in the
        # tree and nothing in the viewport — harder to notice than a refusal.
        # The empty document goes with it rather than being left in the library.
        document_service.delete_document(doc.id)
        raise PreconditionFailed(
            f"the part run finished without building a body, so nothing was "
            f"placed: {outcome.get('summary')}")

    with document_service.writing(asm_id):
        latest = _assembly(asm_id)
        if latest.revision != asm.revision:
            raise PreconditionFailed(f"Assembly changed since you read it; partial part {doc.id} is saved. Reconcile before inserting.")
        if parent.get("cancelled") and parent["cancelled"]():
            raise PreconditionFailed(f"Stopped; partial part {doc.id} is saved for continuation.")
        updated = assembly_service.add_component(asm_id, doc.id)
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    placed = updated.components[-1]
    return {"message": f"designed and placed {name}",
            "part_id": doc.id,
            "component_id": placed.id,
            "component_count": len(updated.components),
            # What the sub-run did, so the assembly planner can tell a part that
            # was built as asked from one that ran out of budget half way.
            "part_steps": len([x for x in outcome["steps"] if x.get("kind") == "op"]),
            "part_finished": outcome["finished"],
            "part_summary": outcome["summary"],
            "part_task_state": outcome.get("task_state"), "part_trajectory_id": outcome.get("trajectory_id"),
            "next": "mate this component — building it leaves it at the origin"}


@operation("search_assembly_parts", "query",
           "Browse available catalog parts before inserting. Returns exact keys, dimensions, parameters and fidelity. Never guess keys.",
           {"search": {"type": "string",
                       "description": "free text matched against a row's key, "
                                      "name and designation, e.g. \"m4 screw\" "
                                      "or \"608 bearing\""},
            "category": {"type": "string",
                         "description": "narrow to one family, e.g. screw, nut, "
                                        "washer, bearing, motor or extrusion"},
            "source": {"type": "string",
                       "description": "where the row comes from: the generated "
                                      "standards, the Adafruit catalog, or this "
                                      "account's own imported supplier parts"},
            "limit": {"type": "integer", "default": 20,
                      "description": "how many rows to return, 1 to 40"},
            "skip": {"type": "integer", "default": 0,
                     "description": "how many rows to step over first, to read "
                                    "the next page of a long list"}},
           subject="assembly")
def _search_assembly_parts(asm_id, args):
    from app.services import caller, standard_parts
    asm = _assembly(asm_id)
    return standard_parts.listing(caller.acting_account_id() or asm.owner_id,
        search=args.get("search"), category=args.get("category"), source=args.get("source"),
        limit=max(1, min(40, int(args.get("limit", 20)))), offset=max(0, int(args.get("skip", 0))))


@operation("insert_standard_part", "mutate",
           "Place a purchased part without modelling it — a screw, nut, washer, "
           "dowel pin, bearing, stepper or T-slot extrusion from the published "
           "dimensional standards, or a brushless motor, flight controller, "
           "4-in-1 ESC, RC servo, hex standoff, propeller, carbon plate or tube "
           "or LiPo pack from the settled conventions of RC, drone and robotics "
           "work. Adafruit catalog keys automatically fetch original manufacturer "
           "CAD and import it as a reusable part. Browse search_assembly_parts. Inserting "
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
    if str(key).startswith("adafruit:"):
        from app.services import adafruit_parts
        row = adafruit_parts.by_key(key)
        if row is None:
            raise BadArgument("Unknown Adafruit catalog key", arg="key")
        designation, fidelity = row["designation"], "supplier"
    elif str(key).startswith(supplier_parts.PREFIX):
        row = supplier_parts.by_key(key, asm.owner_id)
        if row is None:
            raise BadArgument(
                f"no imported part {key!r} on this account; list them with "
                f"search_assembly_parts", arg="key")
        designation = row["designation"]
        fidelity = "supplier"
    else:
        entry = parts_catalog.by_key(key)
        if entry is None:
            raise BadArgument(
                f"no catalog part {key!r}; browse them with list_standard_parts",
                arg="key")
        try:
            designation = parts_catalog.designation_of(entry, params)
            # The *configuration's* fidelity, not the row's: a screw asked for
            # threaded is every dimension the standard gives, and the caller
            # placing one is who needs to be told which body they got.
            fidelity = parts_catalog.fidelity_of(entry, params)
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
            "designation": designation, "fidelity": fidelity,
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
           "`ball` and `parallel`. Those six need a connector on each side. "
           "`angle_deg` positions the mate inside the rotation it leaves free — "
           "a hinge has an angle — and `drive_mate` changes it afterwards "
           "without rebuilding the mate. Use `dof` to see what is still free. "
           "**One bounding-box mate fully places a component**: it sets the "
           "named axis from the faces and centres the other two on the base, so "
           "a second one between the same pair replaces the first rather than "
           "adding to it — to stack a plate centred on another, one `coincident` "
           "on the stacking axis is the whole job. Connector mates do compose.",
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
            "angle_deg": {"type": "number", "default": 0.0, "unit": "deg",
                          "description": "where the mate sits in the rotation it "
                                         "leaves free, about the connectors' "
                                         "shared axis. Only for a type that "
                                         "leaves one free: "
                                         + ", ".join(sorted(_DRIVEN_ROTATION))},
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

    angle_deg = float(args.get("angle_deg", 0.0))
    if angle_deg and mate_type not in _DRIVEN_ROTATION:
        raise BadArgument(
            f"a {mate_type} mate leaves no rotation free about its axis, so "
            f"there is no angle to place it at — driving one would break the "
            f"constraint rather than position the part inside it. "
            f"{', '.join(sorted(_DRIVEN_ROTATION))} take an angle.",
            arg="angle_deg")

    before = next((c.transform.translate.model_dump()
                   for c in asm.components if c.id == moved), None)
    updated = assembly_service.add_mate(asm_id, {
        "id": "", "type": mate_type, "base": base, "moved": moved,
        "axis": args.get("axis", "z"),
        "base_face": args.get("base_face", "max"),
        "moved_face": args.get("moved_face", "min"),
        "offset_mm": float(args.get("offset_mm", 0.0)),
        "angle_deg": angle_deg,
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
                                              "translational": translational},
                              "angle_deg": mate.angle_deg,
                              "drivable": mate.type in _DRIVEN_ROTATION}
    # A mate that positions nothing differently is the assembly-side version of
    # an edit that reports success and changes nothing. Say so rather than
    # leaving the caller to diff two transforms.
    notes = []
    if before is not None and before == after:
        notes.append("the mate is satisfied where the part already was — nothing moved")
    # **A second bounding-box mate on the same pair does not add a constraint.**
    # `_apply` places the component on all three axes — the named one by faces,
    # the other two centred on the base — so the newer mate replaces the older
    # one's placement outright. A planner that mates z, then x, then y to centre
    # a plate undoes its own stack twice and cannot see why, because each call
    # returned ok. Connector mates compose properly and are the way to hold more
    # than one relationship between two parts.
    if mate.base_connector is None and mate.moved_connector is None:
        siblings = [m for m in updated.mates
                    if m.id != mate.id
                    and m.base_connector is None and m.moved_connector is None
                    and {m.base, m.moved} == {mate.base, mate.moved}]
        if siblings:
            notes.append(
                f"this is bounding-box mate {len(siblings) + 1} between these two "
                f"components and they do not combine: each one places the part on "
                f"all three axes, so this one replaced the placement from "
                f"{', '.join(m.id for m in siblings)}. Keep one mate per pair and "
                f"give it the axis that matters — the other two are centred for "
                f"you — or use mate connectors, which do compose")
    if notes:
        result["note"] = " · ".join(notes)
    return result


@operation("drive_mate", "mutate",
           "Move a joint through the travel its mate leaves free, and re-solve "
           "every placement downstream of it. `angle_deg` swings the moved part "
           "about the connectors' shared axis — a hinge opening, a gear "
           "indexing, a servo horn turning — and `offset_mm` slides it along "
           "that axis. This is a **pose**, not a constraint: the mate keeps the "
           "degrees of freedom it had, so `dof` reads the same before and after. "
           "An angle is refused on a mate type with no rotation free about its "
           "axis, because there it would break the constraint rather than "
           "position the part inside it.",
           {"mate_id": {"type": "string", "required": True,
                        "description": "mate id, from list_mates"},
            "angle_deg": {"type": "number", "unit": "deg",
                          "description": "rotation about the connectors' shared "
                                         "axis. Absolute, not a increment."},
            "offset_mm": {"type": "number", "unit": "mm",
                          "description": "translation along that axis, or the "
                                         "gap a bounding-box mate holds"}},
           subject="assembly")
def _drive_mate(asm_id, args):
    asm = _assembly(asm_id)
    mate_id = require(args, "mate_id")
    mate = next((m for m in asm.mates if m.id == mate_id), None)
    if mate is None:
        raise BadArgument(f"no mate {mate_id!r} in this assembly", arg="mate_id")

    angle = args.get("angle_deg")
    offset = args.get("offset_mm")
    if angle is None and offset is None:
        raise BadArgument(
            "nothing to drive — give 'angle_deg', 'offset_mm', or both",
            arg="angle_deg")
    if angle is not None and float(angle) != mate.angle_deg \
            and mate.type not in _DRIVEN_ROTATION:
        raise BadArgument(
            f"a {mate.type} mate leaves no rotation free about its axis, so "
            f"there is no angle to drive it to. "
            f"{', '.join(sorted(_DRIVEN_ROTATION))} take an angle.",
            arg="angle_deg")

    before = next((c.transform for c in asm.components if c.id == mate.moved), None)
    updated = assembly_service.update_mate(
        asm_id, mate_id, {"angle_deg": angle, "offset_mm": offset})
    if updated is None:
        raise NoSuchDocument(f"no assembly {asm_id!r}")
    driven = next(m for m in updated.mates if m.id == mate_id)
    after = next((c.transform for c in updated.components if c.id == driven.moved), None)

    result: Dict[str, Any] = {
        "message": f"drove {driven.type} mate {mate_id} to "
                   f"{driven.angle_deg:g} deg, {driven.offset_mm:g} mm",
        "mate_id": mate_id, "type": driven.type,
        "angle_deg": driven.angle_deg, "offset_mm": driven.offset_mm,
        "moved": driven.moved,
        # Driving picks a value inside a free degree of freedom; it does not
        # spend one. Said on the result because the opposite is the natural
        # assumption and it would make an agent think it had constrained
        # something.
        "removes_dof": 0,
    }
    if before is not None and after is not None \
            and before.model_dump() == after.model_dump():
        result["note"] = ("the part did not move — a mate with no connectors "
                          "has no axis to drive about, or the value was "
                          "already what it is now")
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


@operation("prop_clearance", "query",
           "Rotor clearance, which is not the same question as interference. A "
           "propeller is reduced to the **disc it sweeps** — tip radius measured "
           "across the body, so a two-bladed model and a swept-disc one read "
           "identically — and then: do two discs overlap, is anything standing "
           "inside one, and how long do the arms have to be. Blades frozen at "
           "one azimuth clash or clear depending on where the modeller left "
           "them, which is not a property of the aircraft, and that is why "
           "check_interference is the wrong tool here. Stacked coaxial rotors "
           "are reported as stacked, with the axial separation that makes them "
           "fine, rather than as a clash. Rotors are found by catalog category "
           "unless `props` names them.",
           {"clearance_mm": {"type": "number", "default": 5.0, "unit": "mm",
                             "description": "gaps below this are reported as "
                                            "warnings; below zero is a strike"},
            "props": {"type": "array", "items": {"type": "string"},
                      "description": "component ids to treat as rotors, for a "
                                     "propeller you modelled or imported rather "
                                     "than placed from the catalog"},
            "hub_mm": {"type": "number", "unit": "mm",
                       "description": "hub diameter, which does not sweep and "
                                      "is where the shaft belongs. Taken from "
                                      "the catalog row when the rotor came from "
                                      "one; set it for a rotor you modelled, or "
                                      "the shaft it is bolted to reads as an "
                                      "obstruction"}},
           subject="assembly")
def _prop_clearance(asm_id, args):
    from app.services import interference

    _assembly(asm_id)
    declared = args.get("props")
    if declared is not None and not isinstance(declared, list):
        raise BadArgument("props must be a list of component ids", arg="props")
    try:
        hub = args.get("hub_mm")
        return interference.check_prop_clearance(
            asm_id, clearance_mm=float(args.get("clearance_mm", 5.0)),
            props=declared,
            hub_mm=None if hub is None else float(hub))
    except ValueError as exc:
        # Nothing to measure. A `PRECONDITION` rather than a report saying
        # `clear`, because the fix is an earlier step — place a rotor, or say
        # which components are rotors — and a clearance report that means "I
        # found nothing to check" is the one thing this must not be read as.
        raise PreconditionFailed(str(exc)) from exc


@operation("bill_of_materials", "query",
           "The parts list: one line per distinct part with quantity, material, "
           "mass and material cost. Cost is mass x price per kilo — it is not a quote.",
           {}, subject="assembly")
def _bill_of_materials(asm_id, args):
    from app.services import bom

    _assembly(asm_id)
    return bom.bill_of_materials(asm_id)


@operation("export_srdf", "query",
           "The semantic half a URDF cannot carry: which chains of links are "
           "worth planning for, and which pairs may touch. A planner that "
           "treats every contact as a collision will not move a machine whose "
           "parts are bolted together by design. Groups are the kinematic "
           "chains; disabled pairs are the mated ones (exact) plus the ones "
           "the interference check finds touching at this pose. End effectors, "
           "virtual joints and named states are planning decisions nothing in "
           "an assembly answers, so they are left out rather than guessed.",
           {"name": {"type": "string",
                     "description": "robot name; must match the URDF's"},
            "clearance_mm": {"type": "number", "default": 1.0, "unit": "mm",
                             "description": "pairs closer than this are treated "
                                            "as touching"}},
           subject="assembly")
def _export_srdf(asm_id, args):
    from app.services import srdf

    _assembly(asm_id)
    try:
        return srdf.export_srdf(asm_id, name=args.get("name"),
                                clearance_mm=float(args.get("clearance_mm", 1.0)))
    except ValueError as exc:
        raise PreconditionFailed(str(exc))


@operation("export_sdf", "query",
           "The assembly as SDFormat (Gazebo), which represents two things URDF "
           "cannot: a ball mate becomes a real `ball` joint rather than a fixed "
           "one, and a closed kinematic loop is expressible because an SDFormat "
           "model is a graph rather than a tree. It is not strictly better — "
           "SDFormat has no planar joint where URDF does, and no cylindrical "
           "joint either (`screw` couples rotation to translation, which a "
           "cylindrical mate does not). Whatever it cannot say is named in "
           "`unsupported` rather than quietly emitted as something else.",
           {"name": {"type": "string",
                     "description": "model name in the file; defaults to the "
                                    "assembly's"},
            "mesh_urls": {"type": "boolean", "default": True,
                          "description": "reference each link's mesh for visual "
                                         "and collision geometry"}},
           subject="assembly")
def _export_sdf(asm_id, args):
    from app.services import sdformat

    _assembly(asm_id)
    try:
        return sdformat.export_sdf(asm_id, name=args.get("name"),
                                   mesh_urls=bool(args.get("mesh_urls", True)))
    except ValueError as exc:
        raise PreconditionFailed(str(exc))


@operation("render_assembly", "query",
           "Look at the assembly as it stands. Renders shaded views of every "
           "component **in its placed position** and returns a URL per view, "
           "keyed by the image's content hash — never inline bytes. The "
           "interference check says no two parts occupy the same space; it "
           "cannot say whether the arm is on backwards. Defaults to three "
           "views, because a part in the wrong place is invisible from one "
           "angle.",
           {"cameras": {"type": "array", "items": {"type": "string"},
                        "description": "which directions to look from: iso, "
                                       "iso_opposite, top, bottom, front, right. "
                                       "At most 3 are rendered. Defaults to "
                                       "iso, top and front."},
            "pixels": {"type": "integer", "default": 512,
                       "description": "pixels on a side, 128 to 1024"}},
           subject="assembly")
def _render_assembly_view(asm_id, args):
    from app.services import snapshot

    _assembly(asm_id)
    views = snapshot.packet("assembly", asm_id, args.get("cameras"),
                            int(args.get("pixels") or snapshot.REVIEW_PIXELS))
    if not views:
        raise PreconditionFailed(
            "No component in this assembly has a body to render: insert a part "
            "with insert_part or build_part first.")
    return {"views": snapshot.store_views("assembly", asm_id, views),
            "summary": f"Rendered {len(views)} view(s) of the placed assembly."}
