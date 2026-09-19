"""Two speedups that must not change an answer.

`exact_bounds` prunes which faces get an optimal box, and the faceted STEP
writer shares vertices and edges instead of emitting them per triangle. Both
were found by profiling a real supplier motor and a shelled body; both are only
worth having if the output is what it was.
"""

import re

import numpy as np
import pytest

from app.cad_kernel.evaluator import brep

pytestmark = pytest.mark.skipif(not brep.AVAILABLE, reason="needs the exact kernel")


def _whole(shape):
    from OCP.Bnd import Bnd_Box
    from OCP.BRepBndLib import BRepBndLib

    box = Bnd_Box()
    BRepBndLib.AddOptimal_s(shape, box, False, False)
    return np.array(box.Get())


def _spline_body():
    """A lofted body with B-spline faces, plus a tilted cylinder cut from it."""
    from OCP.BRepAlgoAPI import BRepAlgoAPI_Cut
    from OCP.BRepBuilderAPI import BRepBuilderAPI_MakeEdge, BRepBuilderAPI_MakeWire
    from OCP.BRepOffsetAPI import BRepOffsetAPI_ThruSections
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeCylinder
    from OCP.gp import gp_Ax2, gp_Circ, gp_Dir, gp_Pnt

    loft = BRepOffsetAPI_ThruSections(True, False)
    for z, r, dx in ((0.0, 0.02, 0.0), (0.015, 0.012, 0.004), (0.03, 0.018, -0.003)):
        circle = gp_Circ(gp_Ax2(gp_Pnt(dx, 0, z), gp_Dir(0.1, 0, 1)), r)
        loft.AddWire(BRepBuilderAPI_MakeWire(BRepBuilderAPI_MakeEdge(circle).Edge()).Wire())
    loft.Build()
    tool = BRepPrimAPI_MakeCylinder(gp_Ax2(gp_Pnt(0, -0.05, 0.012), gp_Dir(0.2, 1, 0.1)),
                                    0.004, 0.1).Shape()
    return BRepAlgoAPI_Cut(loft.Shape(), tool).Shape()


def test_the_pruned_box_is_the_optimal_box():
    from OCP.BRepPrimAPI import BRepPrimAPI_MakeTorus
    from OCP.gp import gp_Ax2, gp_Dir, gp_Pnt

    from app.cad_kernel.geom.measurements import exact_bounds

    for shape in (_spline_body(),
                  BRepPrimAPI_MakeTorus(gp_Ax2(gp_Pnt(0, 0, 0), gp_Dir(0.2, 0.3, 1)),
                                        0.02, 0.004).Shape()):
        assert np.array(exact_bounds(shape)) == pytest.approx(_whole(shape), abs=1e-12)


def test_a_faceted_step_shares_its_edges():
    """Each edge of a closed mesh is written once and used twice, the second
    time reversed — so the file is a closed shell as written, not a pile of
    triangles an importer has to sew."""
    import trimesh

    from app.cad_kernel.core.topology import FaceMesh
    from app.cad_kernel.io import step, step_import

    mesh = trimesh.creation.icosphere(subdivisions=2, radius=0.02)
    text = step.write_step(FaceMesh(vertices=np.asarray(mesh.vertices),
                                    faces=np.asarray(mesh.faces)), "sphere")
    assert len(re.findall(r"=EDGE_CURVE\(", text)) == len(mesh.edges_unique)
    assert len(re.findall(r"=VERTEX_POINT\(", text)) == len(mesh.vertices)
    assert len(re.findall(r"ORIENTED_EDGE\([^)]*\.F\.\)", text)) == len(mesh.edges_unique)

    shape, _ = step_import.read_exact(text.encode(), "step")
    assert brep.free_edges(shape) == 0
    assert brep.volume(shape) == pytest.approx(mesh.volume, rel=1e-6)
