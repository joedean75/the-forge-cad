"""Minimal STEP AP214 exporter for the DAPS CAD kernel.

The output represents a solid as a MANIFOLD_SOLID_BREP whose shell is made of
planar triangular FACE_SURFACE entities.  This is a faceted STEP file: it is
valid AP214 but approximates curved surfaces with their triangulation.
"""

from __future__ import annotations

from datetime import datetime, timezone
from io import StringIO
from typing import List, Tuple, Union

import numpy as np

from app.cad_kernel.core.topology import FaceMesh, Solid
from app.cad_kernel.geom.meshing import face_normals, solid_to_mesh


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

    def shape_representation(self, manifold_id: int, axis_id: int) -> int:
        return self.add(f"SHAPE_REPRESENTATION('NONE',(#{manifold_id}),#{axis_id})")


def write_step(mesh: Union[FaceMesh, Solid], name: str = "part") -> str:
    if isinstance(mesh, Solid):
        mesh = solid_to_mesh(mesh)
    verts = mesh.vertices
    faces = mesh.faces
    normals = face_normals(verts, faces)

    b = _StepBuilder()

    # Global coordinate system
    origin_id = b.point(0.0, 0.0, 0.0)
    z_id = b.direction(0.0, 0.0, 1.0)
    x_id = b.direction(1.0, 0.0, 0.0)
    global_axis = b.axis2(origin_id, z_id, x_id)

    face_ids: List[int] = []
    for tri, normal in zip(faces, normals):
        # Vertex points
        p_ids = [b.point(*verts[idx]) for idx in tri]
        v_ids = [b.vertex(pid) for pid in p_ids]

        # Edges as straight lines between consecutive vertices
        edge_oriented: List[int] = []
        for i in range(3):
            j = (i + 1) % 3
            p1, p2 = p_ids[i], p_ids[j]
            dx = float(verts[tri[j]][0] - verts[tri[i]][0])
            dy = float(verts[tri[j]][1] - verts[tri[i]][1])
            dz = float(verts[tri[j]][2] - verts[tri[i]][2])
            dir_id = b.direction(dx, dy, dz)
            vec_id = b.vector(dir_id)
            line_id = b.line(p1, vec_id)
            edge_id = b.edge_curve(v_ids[i], v_ids[j], line_id)
            edge_oriented.append(b.oriented_edge(edge_id))

        loop_id = b.edge_loop(edge_oriented)
        bound_id = b.face_bound(loop_id)

        # Plane for this triangle
        centroid = verts[tri].mean(axis=0)
        center_id = b.point(*centroid)
        ref_dir = b.direction(1.0, 0.0, 0.0) if abs(float(normal[0])) < 0.9 else b.direction(0.0, 1.0, 0.0)
        axis_id = b.axis2(center_id, b.direction(*normal), ref_dir)
        plane_id = b.plane(axis_id)
        face_ids.append(b.face_surface(bound_id, plane_id))

    shell_id = b.closed_shell(face_ids)
    manifold_id = b.manifold(shell_id)
    shape_id = b.shape_representation(manifold_id, global_axis)

    # Product structure boilerplate (must reference the shape representation)
    app_ctx = b.add("APPLICATION_CONTEXT('automotive design')")
    prod_ctx = b.add(f"PRODUCT_CONTEXT('NONE',#{app_ctx},'mechanical')")
    prod_def_ctx = b.add(f"PRODUCT_DEFINITION_CONTEXT('3D mechanical part',#{prod_ctx},'design')")
    product = b.add(f"PRODUCT('NONE','NONE','',(#{prod_ctx}))")
    prod_def = b.add(f"PRODUCT_DEFINITION('NONE','NONE',#{product},#{prod_def_ctx})")
    prod_def_shape = b.add(f"PRODUCT_DEFINITION_SHAPE('NONE','NONE',#{prod_def})")
    b.add(f"SHAPE_DEFINITION_REPRESENTATION(#{prod_def_shape},#{shape_id})")

    now = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    out = StringIO()
    out.write("ISO-10303-21;\n")
    out.write("HEADER;\n")
    out.write("FILE_DESCRIPTION(('DAPS CAD kernel export'),'2;1');\n")
    out.write(f"FILE_NAME('{name}.step','{now}',(''),(''),'','DAPS CAD kernel','');\n")
    out.write("FILE_SCHEMA(('AUTOMOTIVE_DESIGN { 1 0 10303 214 3 1 1 }'));\n")
    out.write("ENDSEC;\n")
    out.write("DATA;\n")
    for line in b.lines:
        out.write(line + "\n")
    out.write("ENDSEC;\n")
    out.write("END-ISO-10303-21;\n")
    return out.getvalue()
