"""Minimal STEP AP214 exporter for the Forge CAD kernel.

The output represents a solid as a MANIFOLD_SOLID_BREP whose shell is made of
planar triangular FACE_SURFACE entities.  This is a faceted STEP file: it is
valid AP214 but approximates curved surfaces with their triangulation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from typing import Iterator, List, Tuple, Union

import numpy as np

from app.cad_kernel.core.topology import FaceMesh, Solid
from app.cad_kernel.geom.meshing import face_normals, solid_to_mesh


# The kernel works in metres; STEP consumers expect millimetres.
_M_TO_MM = 1000.0


def _fmt_float(value: float) -> str:
    return f"{float(value):.6e}"


class _StepBuilder:
    def __init__(self):
        self.lines: List[str] = []
        self.count = 0
        self.point_ids: dict[Tuple[float, float, float], int] = {}

    def add(self, value: str) -> int:
        self.count += 1
        self.lines.append(f"#{self.count}={value};")
        return self.count

    def point(self, x: float, y: float, z: float) -> int:
        key = (round(float(x), 8), round(float(y), 8), round(float(z), 8))
        if key in self.point_ids:
            return self.point_ids[key]
        idx = self.add(f"CARTESIAN_POINT('NONE',({_fmt_float(x)},{_fmt_float(y)},{_fmt_float(z)}))")
        self.point_ids[key] = idx
        return idx

    def direction(self, dx: float, dy: float, dz: float) -> int:
        return self.add(f"DIRECTION('NONE',({_fmt_float(dx)},{_fmt_float(dy)},{_fmt_float(dz)}))")

    def vector(self, dir_id: int) -> int:
        return self.add(f"VECTOR('NONE',#{dir_id},1.0)")

    def vertex(self, point_id: int) -> int:
        return self.add(f"VERTEX_POINT('NONE',#{point_id})")

    def line(self, point_id: int, vector_id: int) -> int:
        return self.add(f"LINE('NONE',#{point_id},#{vector_id})")

    def edge_curve(self, v1: int, v2: int, line_id: int) -> int:
        return self.add(f"EDGE_CURVE('NONE',#{v1},#{v2},#{line_id},.T.)")

    def oriented_edge(self, edge_id: int, orientation: str = ".T.") -> int:
        return self.add(f"ORIENTED_EDGE('NONE',*,*,#{edge_id},{orientation})")

    def edge_loop(self, edge_ids: List[int]) -> int:
        members = ",".join(f"#{e}" for e in edge_ids)
        return self.add(f"EDGE_LOOP('NONE',({members}))")

    def face_bound(self, loop_id: int) -> int:
        return self.add(f"FACE_BOUND('NONE',#{loop_id},.T.)")

    def axis2(self, origin_id: int, axis_id: int, ref_id: int) -> int:
        return self.add(f"AXIS2_PLACEMENT_3D('NONE',#{origin_id},#{axis_id},#{ref_id})")

    def plane(self, axis_id: int) -> int:
        return self.add(f"PLANE('NONE',#{axis_id})")

    def face_surface(self, bound_id: int, plane_id: int) -> int:
        return self.add(f"FACE_SURFACE('NONE',(#{bound_id}),#{plane_id},.T.)")

    def closed_shell(self, face_ids: List[int]) -> int:
        members = ",".join(f"#{f}" for f in face_ids)
        return self.add(f"CLOSED_SHELL('NONE',({members}))")

    def manifold(self, shell_id: int) -> int:
        return self.add(f"MANIFOLD_SOLID_BREP('NONE',#{shell_id})")

    def unit_context(self) -> int:
        """A geometric representation context declaring millimetres.

        Without this the file states no unit at all, and an importer falls back
        to its own default — so an 80 mm plate written as raw metre values
        arrived as 0.08 mm. Coordinates are scaled to millimetres to match
        (`_M_TO_MM` in `_build`), which is the convention for mechanical STEP.
        """
        length = self.add("(LENGTH_UNIT()NAMED_UNIT(*)SI_UNIT(.MILLI.,.METRE.))")
        angle = self.add("(NAMED_UNIT(*)PLANE_ANGLE_UNIT()SI_UNIT($,.RADIAN.))")
        solid_angle = self.add("(NAMED_UNIT(*)SI_UNIT($,.STERADIAN.)SOLID_ANGLE_UNIT())")
        uncertainty = self.add(
            f"UNCERTAINTY_MEASURE_WITH_UNIT(LENGTH_MEASURE(1.E-05),#{length},"
            f"'distance_accuracy_value','confusion accuracy')")
        return self.add(
            f"(GEOMETRIC_REPRESENTATION_CONTEXT(3)"
            f"GLOBAL_UNCERTAINTY_ASSIGNED_CONTEXT((#{uncertainty}))"
            f"GLOBAL_UNIT_ASSIGNED_CONTEXT((#{length},#{angle},#{solid_angle}))"
            f"REPRESENTATION_CONTEXT('Context','3D'))")

    def shape_representation(self, manifold_id: int, axis_id: int, context_id: int) -> int:
        # The last argument is the representation *context*, not a placement. It
        # previously received the global AXIS2_PLACEMENT_3D — the wrong entity
        # type in that slot, and it left the file with no unit context at all.
        return self.add(f"ADVANCED_BREP_SHAPE_REPRESENTATION"
                        f"('NONE',(#{manifold_id},#{axis_id}),#{context_id})")


def _build(mesh: Union[FaceMesh, Solid]) -> "_StepBuilder":
    """Construct every STEP entity for the mesh, returning the populated builder."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    # The kernel works in metres; the file declares millimetres (see
    # `_StepBuilder.unit_context`), so coordinates are converted to match.
    verts = np.asarray(mesh.vertices, dtype=float) * _M_TO_MM
    faces = mesh.faces
    normals = face_normals(verts, faces)

    b = _StepBuilder()

    # Global coordinate system
    origin_id = b.point(0.0, 0.0, 0.0)
    z_id = b.direction(0.0, 0.0, 1.0)
    x_id = b.direction(1.0, 0.0, 0.0)
    global_axis = b.axis2(origin_id, z_id, x_id)
    context_id = b.unit_context()

    # Shared topology, as a closed shell should have it: one VERTEX_POINT per
    # point and one EDGE_CURVE per edge, the second triangle to use an edge
    # traversing it reversed. Emitting both per triangle — as this did — wrote
    # every edge twice and three vertices a triangle, so a 210k-triangle shell
    # came to 293 MB and sixteen seconds, and the "closed" shell had no two
    # faces sharing an edge, leaving every importer to sew it back together.
    x_ref, y_ref = x_id, b.direction(0.0, 1.0, 0.0)
    vertex_of: dict = {}
    edge_of: dict = {}

    def vertex(point_id: int) -> int:
        found = vertex_of.get(point_id)
        if found is None:
            found = vertex_of[point_id] = b.vertex(point_id)
        return found

    face_ids: List[int] = []
    for tri, normal in zip(faces, normals):
        corners = [verts[idx] for idx in tri]
        p_ids = [b.point(*c) for c in corners]
        if len(set(p_ids)) < 3:
            continue                  # degenerate: two corners are one point
        edge_oriented: List[int] = []
        for i in range(3):
            j = (i + 1) % 3
            p1, p2 = p_ids[i], p_ids[j]
            existing = edge_of.get((p2, p1))
            if existing is not None:
                edge_oriented.append(b.oriented_edge(existing, ".F."))
                continue
            d = corners[j] - corners[i]
            line_id = b.line(p1, b.vector(b.direction(float(d[0]), float(d[1]), float(d[2]))))
            edge_id = b.edge_curve(vertex(p1), vertex(p2), line_id)
            edge_of[(p1, p2)] = edge_id
            edge_oriented.append(b.oriented_edge(edge_id))

        bound_id = b.face_bound(b.edge_loop(edge_oriented))
        # Any point of the plane locates it; a corner already exists, where a
        # centroid was one more point per triangle.
        ref_dir = x_ref if abs(float(normal[0])) < 0.9 else y_ref
        axis_id = b.axis2(p_ids[0], b.direction(*normal), ref_dir)
        face_ids.append(b.face_surface(bound_id, b.plane(axis_id)))

    shell_id = b.closed_shell(face_ids)
    manifold_id = b.manifold(shell_id)
    shape_id = b.shape_representation(manifold_id, global_axis, context_id)

    # Product structure boilerplate (must reference the shape representation)
    app_ctx = b.add("APPLICATION_CONTEXT('automotive design')")
    prod_ctx = b.add(f"PRODUCT_CONTEXT('NONE',#{app_ctx},'mechanical')")
    prod_def_ctx = b.add(f"PRODUCT_DEFINITION_CONTEXT('3D mechanical part',#{prod_ctx},'design')")
    product = b.add(f"PRODUCT('NONE','NONE','',(#{prod_ctx}))")
    prod_def = b.add(f"PRODUCT_DEFINITION('NONE','NONE',#{product},#{prod_def_ctx})")
    prod_def_shape = b.add(f"PRODUCT_DEFINITION_SHAPE('NONE','NONE',#{prod_def})")
    b.add(f"SHAPE_DEFINITION_REPRESENTATION(#{prod_def_shape},#{shape_id})")
    return b


def write_step(mesh: Union[FaceMesh, Solid], name: str = "part") -> str:
    """Return a STEP AP214 file as a single string (see stream_step to avoid the copy)."""
    out = StringIO()
    for chunk in _emit(_build(mesh), name):
        out.write(chunk)
    return out.getvalue()


def _header(name: str) -> str:
    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    return (
        "ISO-10303-21;\n"
        "HEADER;\n"
        "FILE_DESCRIPTION(('Forge CAD kernel export'),'2;1');\n"
        f"FILE_NAME('{name}.step','{now}',(''),(''),'','Forge CAD kernel','');\n"
        "FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }'));\n"
        "ENDSEC;\n"
        "DATA;\n"
    )


def _emit(b: "_StepBuilder", name: str, batch: int = 2000) -> Iterator[str]:
    """Yield the finished file in batches of entity lines."""
    yield _header(name)
    for start in range(0, len(b.lines), batch):
        yield "".join(line + "\n" for line in b.lines[start:start + batch])
    yield "ENDSEC;\nEND-ISO-10303-21;\n"


def triangle_count(mesh: Union[FaceMesh, Solid]) -> int:
    """Triangles the export would emit — used to budget a request before building."""
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    return int(len(mesh.faces))


def stream_step(mesh: Union[FaceMesh, Solid], name: str = "part") -> Iterator[str]:
    """Stream a STEP AP214 file instead of materializing it as one string.

    A faceted STEP carries ~20 entity lines per triangle, so a modest part at a
    high meshing resolution runs to hundreds of megabytes. Building that as a
    single ``str`` and handing it to the response layer costs roughly twice the
    file size in peak RSS, which is enough to OOM a small instance. Emitting in
    batches keeps one batch resident instead of a second full copy.
    """
    b = _build(mesh)
    yield from _emit(b, name)
