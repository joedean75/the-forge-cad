"""Parametric curve definitions used by B-rep edges."""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import List

import numpy as np

from app.cad_kernel.core.types import Point3, Vector3


class Curve(ABC):
    """1D parametric curve."""

    @abstractmethod
    def eval(self, t: float) -> Point3:
        """Evaluate curve at parameter t in [0, 1]."""
        raise NotImplementedError

    @abstractmethod
    def tangent(self, t: float) -> Vector3:
        raise NotImplementedError

    def tessellate(self, n_segments: int = 32) -> List[Point3]:
        return [self.eval(t / max(1, n_segments)) for t in range(n_segments + 1)]


class LineCurve(Curve):
    def __init__(self, start: Point3, end: Point3):
        self.start = start
        self.end = end

    def eval(self, t: float) -> Point3:
        return Point3(
            self.start.x + t * (self.end.x - self.start.x),
            self.start.y + t * (self.end.y - self.start.y),
            self.start.z + t * (self.end.z - self.start.z),
        )

    def tangent(self, t: float) -> Vector3:
        return Vector3(
            self.end.x - self.start.x,
            self.end.y - self.start.y,
            self.end.z - self.start.z,
        ).normalized()


class CircleCurve(Curve):
    def __init__(self, center: Point3, radius: float, normal: Vector3, start_dir: Vector3):
        self.center = center
        self.radius = radius
        self.normal = normal.normalized()
        self.start_dir = start_dir.normalized()
        # Ensure start_dir is perpendicular to normal
        dot = self.start_dir.dot(self.normal)
        if abs(dot) > 1e-6:
            self.start_dir = (self.start_dir - self.normal * dot).normalized()
        self.binormal = self.normal.cross(self.start_dir)

    def eval(self, t: float) -> Point3:
        angle = 2.0 * math.pi * t
        cos_a = math.cos(angle)
        sin_a = math.sin(angle)
        offset = self.start_dir * (self.radius * cos_a) + self.binormal * (self.radius * sin_a)
        return self.center + offset

    def tangent(self, t: float) -> Vector3:
        angle = 2.0 * math.pi * t
        dx = -math.sin(angle) * self.radius
        dy = math.cos(angle) * self.radius
        tangent = self.start_dir * dx + self.binormal * dy
        return tangent.normalized()
