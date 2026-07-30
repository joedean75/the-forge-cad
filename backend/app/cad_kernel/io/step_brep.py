"""STEP export from exact B-rep geometry.

The faceted exporter in `step.py` writes a valid file that is nonetheless a dead
end downstream: a cylinder arrives in Fusion or SolidWorks as several thousand
planar facets, so nothing about it can be measured, filleted or edited. That is
the interop wall, and no amount of work on the API fixes it — it is a property of
having no analytic surfaces to write.

When the feature tree is exactly representable (see `evaluator/brep.py`), OCC
writes real `CYLINDRICAL_SURFACE`, `SPHERICAL_SURFACE`, `B_SPLINE_SURFACE` and
`ADVANCED_FACE` entities and the part round-trips as an editable solid.

Geometry is in metres internally and written in **millimetres**, which is what
mechanical STEP files use in practice and what every importer assumes when a
file is ambiguous.
"""

from __future__ import annotations

import os
import tempfile

from app.cad_kernel.evaluator import brep

# The kernel works in metres; STEP consumers expect millimetres.
_M_TO_MM = 1000.0


def available() -> bool:
    return brep.AVAILABLE


def _quiet() -> None:
    """Stop OCC writing transfer banners to stdout.

    ``STEPControl_Writer`` prints a "Statistics on Transfer" block on every call.
    In a server that lands in the process log — or worse, interleaved with
    whatever else is on stdout — for something the caller never asked to see.
    Failures still surface: they come back as return codes, which are checked.
    """
    from OCP.Message import Message

    messenger = Message.DefaultMessenger_s()
    for printer in list(messenger.Printers()):
        messenger.RemovePrinter(printer)


def write_step_brep(shape, name: str = "part") -> str:
    """Serialise an exact shape to AP214 STEP text, in millimetres."""
    from OCP.BRepBuilderAPI import BRepBuilderAPI_Transform
    from OCP.gp import gp_Pnt, gp_Trsf
    from OCP.IFSelect import IFSelect_ReturnStatus
    from OCP.Interface import Interface_Static
    from OCP.STEPControl import STEPControl_AsIs, STEPControl_Writer

    _quiet()

    scale = gp_Trsf()
    scale.SetScale(gp_Pnt(0.0, 0.0, 0.0), _M_TO_MM)
    scaled = BRepBuilderAPI_Transform(shape, scale, True).Shape()

    # Interface_Static is global OCC state, so it is set immediately before each
    # write rather than once at import — another exporter could have moved it.
    Interface_Static.SetCVal_s("write.step.unit", "MM")
    Interface_Static.SetCVal_s("write.step.schema", "AP214IS")
    Interface_Static.SetCVal_s("write.step.product.name", name)

    writer = STEPControl_Writer()
    # The shape above is already in millimetres, so OCC must not convert again.
    # Left unset, it converts from a *cached global* "CasCade length unit" that
    # `step_import` raises to metres on every read — which turned an export
    # taken after an import into a file 1000× too large. Pinning both units on
    # this model makes the writer's own conversion the identity, whatever ran
    # before it.
    model = writer.Model()
    model.SetLocalLengthUnit(1.0)
    model.SetWriteLengthUnit(1.0)

    if writer.Transfer(scaled, STEPControl_AsIs) != IFSelect_ReturnStatus.IFSelect_RetDone:
        raise ValueError("OCC could not transfer the shape for STEP export")

    handle, path = tempfile.mkstemp(suffix=".step")
    os.close(handle)
    try:
        if writer.Write(path) != IFSelect_ReturnStatus.IFSelect_RetDone:
            raise ValueError("OCC could not write the STEP file")
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            return fh.read()
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def surface_kinds(step_text: str) -> dict:
    """Count the analytic surface entities in a STEP file.

    Used by tests and by the export endpoint's reporting: it is the difference
    between a real solid and a triangle soup, and it is worth being able to state.
    """
    kinds = ("CYLINDRICAL_SURFACE", "CONICAL_SURFACE", "SPHERICAL_SURFACE",
             "TOROIDAL_SURFACE", "B_SPLINE_SURFACE", "PLANE", "ADVANCED_FACE")
    return {k: step_text.count(f"={k}(") + step_text.count(f"= {k}(") for k in kinds}
