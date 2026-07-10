"""Core geometric types for the DAPS CAD kernel.

These are intentionally lightweight value types.  Topology classes in
`topology.py` reference them to build a boundary representation.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Tuple, Union

import numpy as np


@dataclass(frozen=True)
class Vector3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)

    def length(self) -> float:
        return math.sqrt(self.x * self.x + self.y * self.y + self.z * self.z)

    def normalized(self) -> "Vector3":
        length = self.length()
        if length < 1e-12:
            return Vector3(0.0, 0.0, 0.0)
        return Vector3(self.x / length, self.y / length, self.z / length)

    def cross(self, other: "Vector3") -> "Vector3":
        return Vector3(
            self.y * other.z - self.z * other.y,
            self.z * other.x - self.x * other.z,
            self.x * other.y - self.y * other.x,
        )

    def dot(self, other: "Vector3") -> float:
        return self.x * other.x + self.y * other.y + self.z * other.z

    def __add__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vector3") -> "Vector3":
        return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, scalar: float) -> "Vector3":
        return Vector3(self.x * scalar, self.y * scalar, self.z * scalar)

    def __rmul__(self, scalar: float) -> "Vector3":
        return self.__mul__(scalar)

    def __neg__(self) -> "Vector3":
        return Vector3(-self.x, -self.y, -self.z)


@dataclass(frozen=True)
class Point3:
    x: float = 0.0
    y: float = 0.0
    z: float = 0.0

    def to_array(self) -> np.ndarray:
        return np.array([self.x, self.y, self.z], dtype=float)

    def __add__(self, vec: Vector3) -> "Point3":
        return Point3(self.x + vec.x, self.y + vec.y, self.z + vec.z)

    def __sub__(self, other: Union["Point3", Vector3]) -> Union[Vector3, "Point3"]:
        if isinstance(other, Point3):
            return Vector3(self.x - other.x, self.y - other.y, self.z - other.z)
        return Point3(self.x - other.x, self.y - other.y, self.z - other.z)


@dataclass(frozen=True)
class Plane:
    origin: Point3 = Point3()
    normal: Vector3 = Vector3(0.0, 0.0, 1.0)

    def signed_distance(self, p: Point3) -> float:
        return (p - self.origin).dot(self.normal)


@dataclass(frozen=True)
class Transform:
    """Rigid-body transform stored as a 4x4 matrix."""

    matrix: np.ndarray

    def __post_init__(self):
        if self.matrix.shape != (4, 4):
            raise ValueError("Transform matrix must be 4x4")

    @staticmethod
    def identity() -> "Transform":
        return Transform(np.eye(4, dtype=float))

    @staticmethod
    def translate(vec: Vector3) -> "Transform":
        m = np.eye(4, dtype=float)
        m[:3, 3] = [vec.x, vec.y, vec.z]
        return Transform(m)

    @staticmethod
    def scale(sx: float, sy: float, sz: float) -> "Transform":
        m = np.eye(4, dtype=float)
        m[0, 0] = sx
        m[1, 1] = sy
        m[2, 2] = sz
        return Transform(m)

    def apply_to_point(self, p: Point3) -> Point3:
        v = np.append(p.to_array(), 1.0)
        r = self.matrix @ v
        return Point3(float(r[0]), float(r[1]), float(r[2]))

    def apply_to_vector(self, v: Vector3) -> Vector3:
        a = np.append(v.to_array(), 0.0)
        r = self.matrix @ a
        return Vector3(float(r[0]), float(r[1]), float(r[2]))

    def __matmul__(self, other: "Transform") -> "Transform":
        return Transform(self.matrix @ other.matrix)


BBox3D = Tuple[Point3, Point3]
