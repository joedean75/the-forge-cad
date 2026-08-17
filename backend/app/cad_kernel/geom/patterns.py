"""Where a pattern's copies go, decided once for both kernels.

`PatternFeature` was linear or circular, and each kernel worked out the
placements for itself. The B-rep builder carried a comment saying it picks a
single dominant world axis for a circular pattern "because the SDF does, and
matching that is what keeps the two kernels in agreement" — which is the
comment you write when the arithmetic lives in two places and you have already
been bitten once.

It lives here now, and both read it, as `geom/holes.py` and `geom/ribs.py`
already do for their shapes. Two kinds are added on top of the original two:

* **curve** — instances spaced by equal arc length along a polyline. Translation
  only: the copies keep the seed's orientation rather than turning to follow the
  tangent. That is the right answer for the case this exists to serve — bolt
  holes around an arc stay parallel to their axis, they do not fan out — and
  "follow the tangent" needs a second reference direction to be well defined at
  all, which would be a parameter whose meaning nobody could guess.
* **table** — the placements listed outright. This is the kind the decompiler
  can recover an arbitrary arrangement into, and the one that lets a caller do
  something the other three cannot express. Its list is *complete*: the first
  entry is the first instance, and there is no implicit copy at the origin,
  because an implicit seed would take explicit control away from the only kind
  that exists to provide it.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

#: The kinds a `PatternFeature` may be. `linear` and `circular` were here first
#: and their placements are unchanged, byte for byte.
PATTERN_KINDS = ("linear", "circular", "curve", "table")

Placement = Tuple[np.ndarray, np.ndarray]        # (rotation 3x3, translation 3)


def _euler(rx: float, ry: float, rz: float) -> np.ndarray:
    from app.cad_kernel.evaluator.sdf import _euler_matrix

    return _euler_matrix(rx, ry, rz)


def _dominant(axis) -> int:
    """Which world axis a circular pattern turns about.

    A single dominant axis rather than the vector as given. That is a real
    limitation and it is the *original* behaviour — changing it now would
    rotate every stored circular pattern whose axis was not exactly a world
    one, which is a body moving under a document nobody edited.
    """
    x, y, z = abs(axis.x), abs(axis.y), abs(axis.z)
    if z >= x and z >= y:
        return 2
    return 1 if y >= x else 0


def _resample(path: Sequence[Any], count: int) -> List[np.ndarray]:
    """`count` points spaced by equal arc length along a polyline."""
    points = np.asarray([[float(p["x"]), float(p["y"]), float(p["z"])]
                         if isinstance(p, dict) else [float(c) for c in p]
                         for p in path], dtype=float)
    if len(points) < 2:
        raise ValueError("a curve pattern needs a path of at least two points")
    steps = np.linalg.norm(np.diff(points, axis=0), axis=1)
    total = float(steps.sum())
    if total < 1e-12:
        raise ValueError("a curve pattern's path has no length")
    travelled = np.concatenate([[0.0], np.cumsum(steps)])
    # `count` instances span the whole path end to end; one instance sits at the
    # start, which is what makes a pattern of 1 the seed and nothing else.
    wanted = (np.linspace(0.0, total, count) if count > 1 else np.array([0.0]))
    return [np.array([np.interp(d, travelled, points[:, i]) for i in range(3)])
            for d in wanted]


def placements(feature) -> List[Placement]:
    """`(rotation, translation)` per instance, in order, instance 0 first."""
    kind = getattr(feature, "kind", "linear")
    count = max(1, int(getattr(feature, "count", 1)))
    identity = np.eye(3)

    if kind == "table":
        out: List[Placement] = []
        for entry in (getattr(feature, "transforms", None) or []):
            t = entry.get("translate") or {}
            r = entry.get("rotate") or {}
            out.append((_euler(float(r.get("x", 0.0)), float(r.get("y", 0.0)),
                               float(r.get("z", 0.0))),
                        np.array([float(t.get("x", 0.0)), float(t.get("y", 0.0)),
                                  float(t.get("z", 0.0))])))
        if not out:
            raise ValueError("a table pattern needs at least one entry in `transforms`")
        return out

    if kind == "curve":
        path = getattr(feature, "path", None) or []
        anchors = _resample(path, count)
        origin = anchors[0]
        return [(identity, point - origin) for point in anchors]

    if kind == "circular":
        axis = _dominant(feature.axis)
        centre = np.array([feature.center.x, feature.center.y, feature.center.z])
        out = []
        for i in range(count):
            angle = 360.0 * i / count
            rotation = _euler(*[angle if j == axis else 0.0 for j in range(3)])
            # world = R (p - centre) + centre
            out.append((rotation, centre - rotation @ centre))
        return out

    step = np.array([feature.axis.x, feature.axis.y, feature.axis.z]) * feature.spacing
    return [(identity, step * i) for i in range(count)]


def axis_and_angle(rotation: np.ndarray) -> Tuple[Tuple[float, float, float], float]:
    """A rotation matrix as `(axis, radians)`, for handing to OCC's `gp_Trsf`.

    Both kernels place copies with the same matrix; this is only the change of
    representation OCC wants. Deriving OCC's transform from the matrix rather
    than rebuilding it from angles is what stops a circular pattern being one
    rotation on one kernel and another on the other.
    """
    trace = float(np.clip((np.trace(rotation) - 1.0) / 2.0, -1.0, 1.0))
    angle = math.acos(trace)
    if angle < 1e-12:
        return (0.0, 0.0, 1.0), 0.0
    if abs(angle - math.pi) < 1e-9:
        # At 180 degrees the skew part vanishes; the axis is the eigenvector.
        values, vectors = np.linalg.eigh(rotation + np.eye(3))
        axis = vectors[:, int(np.argmax(values))]
        return (float(axis[0]), float(axis[1]), float(axis[2])), angle
    skew = np.array([rotation[2, 1] - rotation[1, 2],
                     rotation[0, 2] - rotation[2, 0],
                     rotation[1, 0] - rotation[0, 1]])
    axis = skew / (2.0 * math.sin(angle))
    return (float(axis[0]), float(axis[1]), float(axis[2])), angle
