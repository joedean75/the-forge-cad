"""Parametric surface definitions used by B-rep faces."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List, Tuple

import numpy as np

from app.cad_kernel.core.topology import FaceMesh
from app.cad_kernel.core.types import Point3, Vector3


class Surface(ABC):
    @abstractmethod
    def eval(self, u: float, v: float) -> Point3:
        raise NotImplementedError

    @abstractmethod
    def normal(self, u: float, v: float) -> Vector3:
        raise NotImplementedError

    @abstractmethod
    def tessellate(self, **kwargs) -> FaceMesh:
        raise NotImplementedError


class PlaneSurface(Surface):
    """Planar face defined by an ordered list of 3D corner points.

    The corners are assumed to be coplanar and ordered counter-clockwise
    when viewed from the side of the outward normal.
    """

    def __init__(self, corners: List[Point3]):
        if len(corners) < 3:
            raise ValueError("A plane face needs at least 3 corners")
        self.corners = corners
        self.origin = corners[0]
        v0 = corners[1] - corners[0]
        v1 = corners[2] - corners[0]
        n = v0.cross(v1)
        self.normal = n.normalized()

    def eval(self, u: float, v: float) -> Point3:
        # Parameterization is local to the first three corners:
        # P = origin + u*(p1-origin) + v*(p2-origin)
        return self.origin + (self.corners[1] - self.origin) * u + (self.corners[2] - self.origin) * v

    def normal(self, u: float, v: float) -> Vector3:
        return self.normal

    def tessellate(self, **kwargs) -> FaceMesh:
        # Fan triangulation from the first corner.
        verts = np.array([c.to_array() for c in self.corners], dtype=float)
        faces = np.array([[0, i, i + 1] for i in range(1, len(self.corners) - 1)], dtype=int)
        return FaceMesh(verts, faces)


class CylinderSurface(Surface):
    """Cylindrical side surface."""

    def __init__(self, bottom_center: Point3, axis: Vector3, radius: float, height: float):
        self.bottom_center = bottom_center
        self.axis = axis.normalized()
        self.radius = radius
        self.height = height
        # Build an orthonormal basis where z aligns with axis
        if abs(abs(self.axis.z) - 1.0) < 1e-6:
            ref = Vector3(1.0, 0.0, 0.0) if self.axis.z < 0 else Vector3(0.0, 1.0, 0.0)
        else:
            ref = Vector3(0.0, 0.0, 1.0)
        self.u_dir = self.axis.cross(ref).normalized()
        self.v_dir = self.axis.cross(self.u_dir).normalized()

    def eval(self, u: float, v: float) -> Point3:
        angle = 2.0 * math.pi * u
        r = self.radius
        x = math.cos(angle) * r
        y = math.sin(angle) * r
        radial = self.u_dir * x + self.v_dir * y
        return self.bottom_center + radial + self.axis * (v * self.height)

    def normal(self, u: float, v: float) -> Vector3:
        angle = 2.0 * math.pi * u
        radial = self.u_dir * math.cos(angle) + self.v_dir * math.sin(angle)
        return radial.normalized()

    def tessellate(self, segments: int = 32, stacks: int = 1) -> FaceMesh:
        verts = []
        faces = []
        for j in range(stacks + 1):
            v = j / stacks
            for i in range(segments):
                u = i / segments
                verts.append(self.eval(u, v).to_array())
        # close ring: duplicate last vertex not needed because u=0 and u=1 coincide
        # Instead, indices wrap around using modulo segments.
        base = lambda j, i: j * segments + (i % segments)
        for j in range(stacks):
            for i in range(segments):
                a = base(j, i)
                b = base(j, i + 1)
                c = base(j + 1, i + 1)
                d = base(j + 1, i)
                faces.append([a, b, c])
                faces.append([a, c, d])
        return FaceMesh(np.array(verts, dtype=float), np.array(faces, dtype=int))


class SphereSurface(Surface):
    """Spherical surface."""

    def __init__(self, center: Point3, radius: float, axis: Vector3 = Vector3(0.0, 0.0, 1.0)):
        self.center = center
        self.radius = radius
        self.axis = axis.normalized()
        if abs(abs(self.axis.z) - 1.0) < 1e-6:
            ref = Vector3(1.0, 0.0, 0.0)
        else:
            ref = Vector3(0.0, 0.0, 1.0)
        self.u_dir = self.axis.cross(ref).normalized()
        self.v_dir = self.axis.cross(self.u_dir).normalized()

    def eval(self, u: float, v: float) -> Point3:
        theta = 2.0 * math.pi * u
        phi = math.pi * v
        x = math.sin(phi) * math.cos(theta)
        y = math.sin(phi) * math.sin(theta)
        z = math.cos(phi)
        radial = self.u_dir * x + self.v_dir * y + self.axis * z
        return self.center + radial * self.radius

    def normal(self, u: float, v: float) -> Vector3:
        p = self.eval(u, v) - self.center
        return Vector3(p.x, p.y, p.z).normalized()

    def tessellate(self, segments: int = 32, stacks: int = 16) -> FaceMesh:
        verts = []
        faces = []
        for j in range(stacks + 1):
            v = j / stacks
            for i in range(segments):
                u = i / segments
                verts.append(self.eval(u, v).to_array())
        idx = lambda j, i: j * segments + (i % segments)
        for j in range(stacks):
            for i in range(segments):
                a = idx(j, i)
                b = idx(j, i + 1)
                c = idx(j + 1, i + 1)
                d = idx(j + 1, i)
                faces.append([a, b, c])
                faces.append([a, c, d])
        return FaceMesh(np.array(verts, dtype=float), np.array(faces, dtype=int))


class ConeSurface(Surface):
    """Conical side surface."""

    def __init__(self, apex: Point3, axis: Vector3, base_radius: float, height: float):
        self.apex = apex
        self.axis = axis.normalized()
        self.base_radius = base_radius
        self.height = height
        self.base_center = apex + self.axis * height
        if abs(abs(self.axis.z) - 1.0) < 1e-6:
            ref = Vector3(1.0, 0.0, 0.0)
        else:
            ref = Vector3(0.0, 0.0, 1.0)
        self.u_dir = self.axis.cross(ref).normalized()
        self.v_dir = self.axis.cross(self.u_dir).normalized()

    def eval(self, u: float, v: float) -> Point3:
        angle = 2.0 * math.pi * u
        r = self.base_radius * v
        x = math.cos(angle) * r
        y = math.sin(angle) * r
        radial = self.u_dir * x + self.v_dir * y
        return self.apex + self.axis * (v * self.height) + radial

    def normal(self, u: float, v: float) -> Vector3:
        angle = 2.0 * math.pi * u
        # Cone normal is perpendicular to axis and to radial at base, tilted
        radial = self.u_dir * math.cos(angle) + self.v_dir * math.sin(angle)
        tangent = radial * self.height - self.axis * self.base_radius
        n = tangent.cross(self.axis.cross(radial))
        return n.normalized()

    def tessellate(self, segments: int = 32, stacks: int = 1) -> FaceMesh:
        verts = [self.apex.to_array()]
        for j in range(stacks + 1):
            v = j / stacks
            for i in range(segments):
                u = i / segments
                verts.append(self.eval(u, v).to_array())
        # index mapping: apex at 0, ring j starts at 1 + j*segments
        faces = []
        for i in range(segments):
            a = 0
            b = 1 + (i + 1) % segments
            c = 1 + i
            faces.append([a, b, c])
        for j in range(stacks - 1):
            for i in range(segments):
                a = 1 + j * segments + i
                b = 1 + j * segments + (i + 1) % segments
                c = 1 + (j + 1) * segments + (i + 1) % segments
                d = 1 + (j + 1) * segments + i
                faces.append([a, b, c])
                faces.append([a, c, d])
        return FaceMesh(np.array(verts, dtype=float), np.array(faces, dtype=int))
