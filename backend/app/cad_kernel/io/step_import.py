"""STEP/IGES import as exact B-rep, the mirror of `step_brep.write_step_brep`.

The mesh importer voxelises whatever it is given, which is the only honest thing
to do with an STL — a triangle soup has no analytic surfaces to recover. Doing
the same to a STEP file throws away the only reason the format exists: the
cylinder that arrives as a `CYLINDRICAL_SURFACE` becomes a density grid, its
radius becomes a sample spacing, and nothing about it can be selected, filleted,
measured exactly or handed back out as an editable solid.

This module reads the file as OpenCascade geometry instead, so an imported part
is the same kind of object as a modelled one: addressable faces and edges, exact
mass properties, real STEP on the way back out.

**Units come from the file, not from a heuristic.** Both formats declare their
length unit in the header and OCC applies it during transfer, so the mesh path's
guess ("bigger than 5 m, so it must be millimetres") has nothing to do here.

Getting metres out of that is fiddlier than it looks, and the fix is worth
keeping: ``xstep.cascade.unit`` is read through a *cached* global, and
``STEPControl_Writer`` resets that cache to millimetres. So a STEP read that
followed any STEP export in the same process silently came back 1000× too
large — an 80 mm plate as an 80 m one — with the static still reading ``M``.
`STEPControl_Reader.SetSystemLengthUnit`, called on the loaded reader, sets the
unit for that transfer alone and is unaffected by whatever ran before it. IGES
reads the static directly and is not subject to this.

IGES is frequently a *surface* model with no solid at all, so faces are sewn and
closed shells promoted to solids. What does not close is refused rather than
kept: OCC reads a file it only half understands and returns the faces it managed,
and a handful of loose faces would look like a body, measure as nothing, and — on
the STEP path — displace a voxel import that would have worked.
"""

from __future__ import annotations

import os
import tempfile
from typing import Any, Dict, Optional, Tuple

from app.cad_kernel.evaluator import brep

STEP_FORMATS = {"step", "stp"}
IGES_FORMATS = {"iges", "igs"}
EXACT_FORMATS = STEP_FORMATS | IGES_FORMATS

# Length units either format may declare, in metres. STEP spells them out;
# IGES uses the short forms in its global section.
_UNIT_METRES = {
    "metre": 1.0, "meter": 1.0, "m": 1.0,
    "millimetre": 0.001, "millimeter": 0.001, "mm": 0.001,
    "centimetre": 0.01, "centimeter": 0.01, "cm": 0.01,
    "micrometre": 1e-6, "micrometer": 1e-6, "um": 1e-6,
    "kilometre": 1000.0, "kilometer": 1000.0, "km": 1000.0,
    "inch": 0.0254, "in": 0.0254,
    "foot": 0.3048, "ft": 0.3048,
    "mile": 1609.344, "mi": 1609.344,
    "mil": 2.54e-5,
}

# Sewing tolerance for IGES surface models, in metres. 10 µm: an order of
# magnitude below the thinnest feature this kernel deals with (0.15 mm sheet),
# and wide enough for the gaps a surface exporter leaves between patches. It is
# not conservative-by-default — at 1 µm a plate-with-hole from a real IGES
# surface export leaves the bore unsewn, and the shell then closes over it into
# a solid plate with no hole. Too tight silently changes the part; too loose
# only merges edges that were meant to meet.
_SEW_TOL = 1e-5

# OCC's internal length unit is the millimetre.
_MM_PER_METRE = 1000.0


class ExactImportError(ValueError):
    """The file could not be read as exact B-rep, with a reason worth showing."""


def available() -> bool:
    return brep.AVAILABLE


def _suffix(fmt: str) -> str:
    return ".step" if fmt in STEP_FORMATS else ".iges"


def _reader(fmt: str):
    if fmt in STEP_FORMATS:
        from OCP.STEPControl import STEPControl_Reader

        return STEPControl_Reader()
    from OCP.IGESControl import IGESControl_Reader

    return IGESControl_Reader()


def _file_unit(reader, fmt: str) -> Optional[str]:
    """The length unit the file declares, or None when it does not say."""
    try:
        if fmt in STEP_FORMATS:
            from OCP.TColStd import TColStd_SequenceOfAsciiString

            lengths, angles, solids = (TColStd_SequenceOfAsciiString() for _ in range(3))
            reader.FileUnits(lengths, angles, solids)
            if lengths.Length():
                return lengths.Value(1).ToCString().strip().lower()
            return None
        name = reader.IGESModel().GlobalSection().UnitName()
        return name.ToCString().strip().lower() if name is not None else None
    except Exception:
        return None


def _count(shape, kind) -> int:
    from OCP.TopExp import TopExp_Explorer

    exp, n = TopExp_Explorer(shape, kind), 0
    while exp.More():
        n += 1
        exp.Next()
    return n


def _solidify(shape):
    """Sew loose faces into shells and close shells into solids.

    STEP usually arrives as a solid already and passes straight through. IGES
    routinely does not: it is a surface format, and an exporter that writes
    `MANIFOLD_SOLID_BREP`-equivalent geometry is the exception rather than the
    rule.
    """
    from OCP.BRep import BRep_Builder
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeSolid, BRepBuilderAPI_Sewing
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopoDS import TopoDS, TopoDS_Compound

    if _count(shape, TopAbs_SOLID):
        return shape
    if not _count(shape, TopAbs_FACE):
        return shape

    sewing = BRepBuilderAPI_Sewing(_SEW_TOL)
    exp = TopExp_Explorer(shape, TopAbs_FACE)
    while exp.More():
        sewing.Add(exp.Current())
        exp.Next()
    sewing.Perform()
    sewed = sewing.SewedShape()
    if sewed.IsNull():
        return shape

    # A shell is promoted only when the solid built from it is valid and keeps
    # every face. Both guards earn their place: `ShapeFix_Solid` will happily
    # close a shell that is missing a bore's cylindrical face, turning a plate
    # with a hole into a plate — geometry silently different from the file.
    solids = []
    exp = TopExp_Explorer(sewed, TopAbs_SHELL)
    while exp.More():
        shell = TopoDS.Shell_s(exp.Current())
        try:
            solid = BRepBuilderAPI_MakeSolid(shell).Solid()
        except Exception:
            exp.Next()
            continue
        if (not solid.IsNull() and brep.is_valid(solid)
                and _count(solid, TopAbs_FACE) == _count(shell, TopAbs_FACE)):
            solids.append(solid)
        exp.Next()

    if not solids:
        return sewed  # `read_exact` refuses it, and its face count says why
    if len(solids) == 1:
        return solids[0]
    builder, compound = BRep_Builder(), TopoDS_Compound()
    builder.MakeCompound(compound)
    for solid in solids:
        builder.Add(compound, solid)
    return compound


def _scaled(shape, factor: float):
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf

    trsf = gp_Trsf()
    trsf.SetScale(gp_Pnt(0.0, 0.0, 0.0), factor)
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def read_exact(data: bytes, fmt: str, units: str = "auto") -> Tuple[Any, Dict[str, Any]]:
    """Read STEP or IGES bytes into an exact shape in metres.

    ``units`` other than ``auto`` reinterprets the file's raw coordinates in
    that unit, undoing whatever the header declared. That is an override for a
    file whose header is wrong; left on ``auto`` the declaration is trusted,
    which is the case that should hold for anything a real CAD system wrote.
    """
    if not brep.AVAILABLE:
        raise ExactImportError(
            "Exact STEP/IGES import needs OpenCascade (pip install cadquery-ocp).")

    fmt = fmt.lower()
    if fmt not in EXACT_FORMATS:
        raise ExactImportError(f"'{fmt}' is not a B-rep exchange format.")

    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.TopAbs import TopAbs_EDGE, TopAbs_FACE, TopAbs_SHELL, TopAbs_SOLID

    from app.cad_kernel.io.step_brep import _quiet

    _quiet()
    # Interface_Static is global OCC state; set it per call, as the writer does.
    Interface_Static.SetCVal_s("xstep.cascade.unit", "M")

    handle, path = tempfile.mkstemp(suffix=_suffix(fmt))
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)

        reader = _reader(fmt)
        if reader.ReadFile(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ExactImportError(
                "OpenCascade could not parse this file — it may be truncated or "
                "not really a STEP/IGES file.")
        if reader.NbRootsForTransfer() < 1:
            raise ExactImportError("The file contains no transferable geometry.")

        file_unit = _file_unit(reader, fmt)
        if fmt in STEP_FORMATS:
            # Per-transfer, and immune to the cached global (see module docstring).
            # OCC counts in millimetres, so 1000 mm per unit is metres.
            reader.SetSystemLengthUnit(_MM_PER_METRE)
        reader.TransferRoots()
        shape = reader.OneShape()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    if shape is None or shape.IsNull():
        raise ExactImportError("The file transferred to an empty shape.")

    shape = _solidify(shape)

    if units != "auto":
        wanted = _UNIT_METRES.get(units.strip().lower())
        if wanted is None:
            raise ExactImportError(
                f"Unknown units '{units}'. Use auto, m, mm, cm or inch.")
        applied = _UNIT_METRES.get(file_unit or "", 1.0)
        if abs(wanted - applied) > 1e-15:
            shape = _scaled(shape, wanted / applied)
        units_note = f"{units} (override; file said {file_unit or 'nothing'})"
    else:
        units_note = f"{file_unit} (from file)" if file_unit else "unspecified"

    solids = _count(shape, TopAbs_SOLID)
    enclosed = brep.volume(shape) if solids else 0.0
    if not solids or enclosed <= 0.0:
        # Deliberately strict. OCC will read a file it only half understands and
        # hand back a few disconnected faces, which is worse than a clean
        # refusal: it looks like a body, measures as nothing, and would replace
        # a perfectly good voxel import. STEP falls back to the faceted parser
        # from here; IGES has nowhere to fall back to and says so.
        raise ExactImportError(
            f"The file read as {_count(shape, TopAbs_FACE)} loose face(s) rather "
            "than a closed solid — it may use surface geometry this importer "
            "cannot stitch back together.")

    info = {
        "units": units_note,
        "solids": solids,
        "shells": _count(shape, TopAbs_SHELL),
        "faces": _count(shape, TopAbs_FACE),
        "edges": _count(shape, TopAbs_EDGE),
        "closed": True,
        "volume_mm3": round(enclosed * 1e9, 6),
    }
    return shape, info


# ---------------------------------------------------------------------------
# Assemblies: the product structure a STEP file carries, not just its bodies
# ---------------------------------------------------------------------------

#: Refused past these before a single document is created. Every distinct part
#: becomes a document that is evaluated and meshed on import, and the route is
#: reachable by anyone who can sign in.
MAX_ASSEMBLY_PARTS = 200
MAX_ASSEMBLY_INSTANCES = 1000
_MAX_ASSEMBLY_DEPTH = 32


def _mirror_z(shape):
    """The shape reflected through its own XY plane."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt, gp_Trsf

    trsf = gp_Trsf()
    trsf.SetMirror(gp_Ax2(gp_Pnt(0.0, 0.0, 0.0), gp_Dir(0.0, 0.0, 1.0)))
    return BRepBuilderAPI_Transform(shape, trsf, True).Shape()


def _location_matrix(location):
    """A `TopLoc_Location` as a 4x4 numpy matrix. `gp_Trsf.Value` includes scale."""
    import numpy as np

    t = location.Transformation()
    return np.array([[t.Value(i, j) for j in (1, 2, 3, 4)] for i in (1, 2, 3)]
                    + [[0.0, 0.0, 0.0, 1.0]])


def unmirror(matrix):
    """Split a placement into (proper placement, mirrored?).

    A component placement is a rotation, a translation and a positive scale, and
    none of those can express a reflection. Decomposing a mirrored matrix anyway
    yields a plausible rotation of the *unmirrored* part — a left-hand bracket
    placed as a right-hand one, with nothing to say so. So the reflection is
    moved into the part: ``M = M' . diag(1, 1, -1)``, where ``M'`` is proper and
    the part is mirrored through its own XY plane. A mirrored bracket is also a
    different part to order, which is what the BOM should then say.
    """
    import numpy as np

    if np.linalg.det(matrix[:3, :3]) >= 0:
        return matrix, False
    return matrix @ np.diag([1.0, 1.0, -1.0, 1.0]), True


def read_assembly(data: bytes, units: str = "auto") -> Dict[str, Any]:
    """Read a STEP file's product structure: distinct parts and placed instances.

    Returns ``{name, structured, units, parts, instances, skipped}``:

    - ``parts``: ``[{key, name, shape, info}]``, one per distinct part. Two
      instances of one part in the file share a key, which is what lets the
      importer make one document and the BOM say qty 2 rather than two lines.
    - ``instances``: ``[{part, matrix}]``, each a 4x4 placement in metres with
      every enclosing sub-assembly's location already composed in, because an
      assembly here is flat.
    - ``structured``: False when the file had no assembly structure at all —
      many exporters flatten to one compound — in which case each solid is a
      part placed where the file put it. Reported, because it is inferred.
    - ``skipped``: what could not become a part, with the reason, rather than
      silently dropped.
    """
    if not brep.AVAILABLE:
        raise ExactImportError(
            "STEP assembly import needs OpenCascade (pip install cadquery-ocp).")

    import numpy as np
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPCAFControl import STEPCAFControl_Reader
    from OCP.TCollection import TCollection_AsciiString, TCollection_ExtendedString
    from OCP.TDataStd import TDataStd_Name
    from OCP.TDF import TDF_Label, TDF_LabelSequence, TDF_Tool
    from OCP.TDocStd import TDocStd_Document
    from OCP.TopAbs import TopAbs_FACE, TopAbs_SOLID
    from OCP.TopExp import TopExp_Explorer
    from OCP.TopLoc import TopLoc_Location
    from OCP.XCAFApp import XCAFApp_Application
    from OCP.XCAFDoc import XCAFDoc_DocumentTool

    from app.cad_kernel.io.step_brep import _quiet

    _quiet()
    # **Millimetres here, and the opposite of `read_exact` on purpose.** The
    # XCAF transfer scales into the *document's* length unit (pinned to metres
    # below) relative to this global. Left at "M" — which `read_exact` sets, and
    # which persists for the life of the process — the two compound and every
    # part and placement comes back 1000x too large. "MM" is OCC's own base, so
    # the document unit alone decides, whichever reader ran last.
    Interface_Static.SetCVal_s("xstep.cascade.unit", "MM")

    handle, path = tempfile.mkstemp(suffix=".step")
    try:
        with os.fdopen(handle, "wb") as fh:
            fh.write(data)
        reader = STEPCAFControl_Reader()
        reader.SetNameMode(True)
        if reader.ReadFile(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ExactImportError(
                "OpenCascade could not parse this file — it may be truncated or "
                "not really a STEP file.")
        step = reader.Reader()
        if step.NbRootsForTransfer() < 1:
            raise ExactImportError("The file contains no transferable geometry.")
        file_unit = _file_unit(step, "step")
        doc = TDocStd_Document(TCollection_ExtendedString("XmlXCAF"))
        XCAFApp_Application.GetApplication_s().InitDocument(doc)
        # **The document's unit, not the reader's, decides the XCAF transfer.**
        # `STEPCAFControl_Reader.Transfer` ignores `SetSystemLengthUnit` in
        # favour of the document's length unit, and a document with none takes
        # a cached global that STEP writers and `read_exact` both move. Measured
        # across every combination: only this, with the global at "MM" above,
        # reads metres on every call regardless of what ran before.
        XCAFDoc_DocumentTool.SetLengthUnit_s(doc, 1.0)
        if not reader.Transfer(doc):
            raise ExactImportError("The file's product structure could not be transferred.")
    finally:
        try:
            os.remove(path)
        except OSError:
            pass

    factor = 1.0
    if units != "auto":
        wanted = _UNIT_METRES.get(units.strip().lower())
        if wanted is None:
            raise ExactImportError(f"Unknown units '{units}'. Use auto, m, mm, cm or inch.")
        factor = wanted / _UNIT_METRES.get(file_unit or "", 1.0)
        units_note = f"{units} (override; file said {file_unit or 'nothing'})"
    else:
        units_note = f"{file_unit} (from file)" if file_unit else "unspecified"

    tool = XCAFDoc_DocumentTool.ShapeTool_s(doc.Main())

    def name_of(label) -> Optional[str]:
        attr = TDataStd_Name()
        if label.FindAttribute(TDataStd_Name.GetID_s(), attr):
            text = attr.Get().ToExtString().strip()
            return text or None
        return None

    def entry_of(label) -> str:
        text = TCollection_AsciiString()
        TDF_Tool.Entry_s(label, text)
        return text.ToCString()

    parts: Dict[str, Dict[str, Any]] = {}
    refused: set = set()
    instances: list = []
    skipped: list = []

    def add(key: str, name: str, shape, matrix) -> None:
        matrix, mirrored = unmirror(matrix)
        if mirrored:
            key, name = f"{key}#mirrored", f"{name} (mirrored)"
        if key in refused:
            return
        if key not in parts:
            body = _solidify(_mirror_z(shape) if mirrored else shape)
            solids = _count(body, TopAbs_SOLID)
            enclosed = brep.volume(body) if solids else 0.0
            if not solids or enclosed <= 0.0:
                refused.add(key)
                skipped.append({"name": name, "reason":
                                f"read as {_count(body, TopAbs_FACE)} loose face(s) "
                                "rather than a closed solid"})
                return
            if factor != 1.0:
                body = _scaled(body, factor)
                enclosed = brep.volume(body)
            if len(parts) >= MAX_ASSEMBLY_PARTS:
                raise ExactImportError(
                    f"This assembly has more than {MAX_ASSEMBLY_PARTS} distinct parts, "
                    "which is more than one import can create.")
            parts[key] = {"key": key, "name": name, "shape": body, "info": {
                "units": units_note, "solids": solids,
                "faces": _count(body, TopAbs_FACE),
                "volume_mm3": round(enclosed * 1e9, 6), "mirrored": mirrored}}
        if len(instances) >= MAX_ASSEMBLY_INSTANCES:
            raise ExactImportError(
                f"This assembly places more than {MAX_ASSEMBLY_INSTANCES} parts, "
                "which is more than one import can create.")
        placed = matrix.copy()
        placed[:3, 3] *= factor
        instances.append({"part": key, "matrix": placed})

    def walk(label, matrix, depth: int) -> None:
        if depth > _MAX_ASSEMBLY_DEPTH:
            raise ExactImportError(
                f"The assembly is nested more than {_MAX_ASSEMBLY_DEPTH} levels deep.")
        if tool.IsAssembly_s(label):
            components = TDF_LabelSequence()
            tool.GetComponents_s(label, components, False)
            for i in range(1, components.Length() + 1):
                component = components.Value(i)
                referred = TDF_Label()
                if not tool.GetReferredShape_s(component, referred):
                    skipped.append({"name": name_of(component) or entry_of(component),
                                    "reason": "an instance that names no part"})
                    continue
                walk(referred, matrix @ _location_matrix(tool.GetLocation_s(component)),
                     depth + 1)
            return
        shape = tool.GetShape_s(label)
        # A part's own shape can carry a location; fold it into the placement so
        # the document holds the part in its own frame, the same for every
        # instance that shares it.
        add(entry_of(label), name_of(label) or f"Part {len(parts) + 1}",
            shape.Located(TopLoc_Location()), matrix @ _location_matrix(shape.Location()))

    roots = TDF_LabelSequence()
    tool.GetFreeShapes(roots)
    labels = [roots.Value(i) for i in range(1, roots.Length() + 1)]
    structured = any(tool.IsAssembly_s(label) for label in labels)

    for label in labels:
        if structured:
            start = (_location_matrix(tool.GetShape_s(label).Location())
                     if tool.IsAssembly_s(label) else np.eye(4))
            walk(label, start, 0)
            continue
        # No structure: each solid is its own part, left where the file put it.
        shape = tool.GetShape_s(label)
        matrix = _location_matrix(shape.Location())
        body = _solidify(shape.Located(TopLoc_Location()))
        found = []
        explorer = TopExp_Explorer(body, TopAbs_SOLID)
        while explorer.More():
            found.append(explorer.Current())
            explorer.Next()
        base = name_of(label) or "Body"
        if not found:
            skipped.append({"name": base, "reason":
                            f"read as {_count(body, TopAbs_FACE)} loose face(s) "
                            "rather than a closed solid"})
        for i, solid in enumerate(found, start=1):
            add(f"{entry_of(label)}/solid{i}",
                base if len(found) == 1 else f"{base} {i}", solid, matrix)

    if not parts:
        detail = "; ".join(f"{s['name']}: {s['reason']}" for s in skipped[:5])
        raise ExactImportError(
            "The file contains no solid parts" + (f" ({detail})." if detail else "."))

    return {
        "name": name_of(labels[0]) if len(labels) == 1 else None,
        "structured": structured,
        "units": units_note,
        "parts": list(parts.values()),
        "instances": instances,
        "skipped": skipped,
    }
