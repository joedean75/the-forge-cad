"""Lightweight 2D sketch constraint solver.

A sketch is points + lines + arcs + circles with geometric/dimensional
constraints. We map the free coordinates (and circle radii) to a variable
vector, express each constraint as a residual, and solve the nonlinear
least-squares system with scipy. Solved closed loops of lines and arcs become a
polygon profile for extrusion (arcs are tessellated).

Supported constraints: fixed, coincident, horizontal, vertical, distance,
parallel, perpendicular, equal_length, radius, point_on_line. Arcs carry an
implicit equal-radius residual (both endpoints equidistant from the center) so
they stay circular without an explicit constraint.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

import numpy as np
from scipy.optimize import least_squares


class SketchError(ValueError):
    pass


def _index_variables(points: List[dict]) -> Tuple[Dict[str, int], List[float]]:
    """Assign each point's (x, y) a slot in the variable vector."""
    var_index: Dict[str, int] = {}
    x0: List[float] = []
    for p in points:
        var_index[p["id"]] = len(x0)
        x0.append(float(p.get("x", 0.0)))
        x0.append(float(p.get("y", 0.0)))
    return var_index, x0


def _residuals(x: np.ndarray, spec: dict, vi: Dict[str, int], radii_index: Dict[str, int]) -> np.ndarray:
    def px(pid):
        return x[vi[pid]]

    def py(pid):
        return x[vi[pid] + 1]

    def line_dir(lid, lines):
        ln = lines[lid]
        return px(ln["b"]) - px(ln["a"]), py(ln["b"]) - py(ln["a"])

    lines = {l["id"]: l for l in spec.get("lines", [])}
    arcs = {a["id"]: a for a in spec.get("arcs", [])}
    res: List[float] = []
    for c in spec.get("constraints", []):
        t = c["type"]
        if t == "fixed":
            p = c["point"]
            res += [px(p) - float(c.get("x", px(p))), py(p) - float(c.get("y", py(p)))]
        elif t == "coincident":
            res += [px(c["a"]) - px(c["b"]), py(c["a"]) - py(c["b"])]
        elif t == "horizontal":
            dx, dy = line_dir(c["line"], lines)
            res.append(dy)
        elif t == "vertical":
            dx, dy = line_dir(c["line"], lines)
            res.append(dx)
        elif t == "distance":
            d = np.hypot(px(c["a"]) - px(c["b"]), py(c["a"]) - py(c["b"]))
            res.append(d - float(c["value"]))
        elif t == "parallel":
            d1x, d1y = line_dir(c["line1"], lines)
            d2x, d2y = line_dir(c["line2"], lines)
            res.append(d1x * d2y - d1y * d2x)
        elif t == "perpendicular":
            d1x, d1y = line_dir(c["line1"], lines)
            d2x, d2y = line_dir(c["line2"], lines)
            res.append(d1x * d2x + d1y * d2y)
        elif t == "equal_length":
            l1 = np.hypot(*line_dir(c["line1"], lines))
            l2 = np.hypot(*line_dir(c["line2"], lines))
            res.append(l1 - l2)
        elif t == "radius":
            res.append(x[radii_index[c["circle"]]] - float(c["value"]))
        elif t == "point_on_line":
            ln = lines[c["line"]]
            ex, ey = px(ln["b"]) - px(ln["a"]), py(ln["b"]) - py(ln["a"])
            wx, wy = px(c["point"]) - px(ln["a"]), py(c["point"]) - py(ln["a"])
            length = np.hypot(ex, ey) + 1e-12
            res.append((ex * wy - ey * wx) / length)  # perpendicular distance
        elif t == "arc_radius":
            arc = arcs.get(c["arc"])
            if arc is None:
                raise SketchError(f"arc_radius references unknown arc: {c['arc']}")
            ra = np.hypot(px(arc["a"]) - px(arc["center"]), py(arc["a"]) - py(arc["center"]))
            res.append(ra - float(c["value"]))
        else:
            raise SketchError(f"unknown constraint: {t}")

    # Implicit per-arc residual: keep the arc circular by forcing both endpoints
    # equidistant from the center.
    for arc in arcs.values():
        ra = np.hypot(px(arc["a"]) - px(arc["center"]), py(arc["a"]) - py(arc["center"]))
        rb = np.hypot(px(arc["b"]) - px(arc["center"]), py(arc["b"]) - py(arc["center"]))
        res.append(ra - rb)

    return np.array(res, dtype=float)


def solve_sketch(spec: dict) -> dict:
    """Solve a sketch. Returns solved point coords, radii, residual and convergence."""
    points = spec.get("points", [])
    if not points:
        raise SketchError("sketch has no points")

    vi, x0 = _index_variables(points)
    circles = spec.get("circles", [])
    radii_index: Dict[str, int] = {}
    for cir in circles:
        radii_index[cir["id"]] = len(x0)
        x0.append(float(cir.get("radius", 0.01)))

    x0_arr = np.array(x0, dtype=float)
    n_res = _residuals(x0_arr, spec, vi, radii_index).size
    if n_res == 0:
        sol_x = x0_arr
        residual = 0.0
    else:
        # 'lm' is fast/exact but requires residuals >= variables; under-constrained
        # sketches (fewer constraints than DOF) need 'trf', which handles both.
        method = "lm" if n_res >= x0_arr.size else "trf"
        result = least_squares(_residuals, x0_arr, args=(spec, vi, radii_index), method=method, max_nfev=400)
        sol_x = result.x
        residual = float(np.sqrt(np.mean(result.fun ** 2))) if result.fun.size else 0.0

    solved_points = {p["id"]: [float(sol_x[vi[p["id"]]]), float(sol_x[vi[p["id"]] + 1])] for p in points}
    solved_radii = {cid: float(sol_x[idx]) for cid, idx in radii_index.items()}
    return {
        "points": solved_points,
        "radii": solved_radii,
        "residual": round(residual, 9),
        "converged": residual < 1e-6,
    }


def _arc_points(
    center: List[float],
    p_from: List[float],
    p_to: List[float],
    ccw: bool,
    segments: int = 24,
) -> List[List[float]]:
    """Tessellate the arc from ``p_from`` to ``p_to`` about ``center``.

    Returns the intermediate points and the endpoint (``p_from`` is assumed to
    already be in the polygon). Radius is interpolated between the two endpoint
    radii so a not-quite-circular solve still produces a smooth curve.
    """
    cx, cy = center
    a0 = math.atan2(p_from[1] - cy, p_from[0] - cx)
    a1 = math.atan2(p_to[1] - cy, p_to[0] - cx)
    r0 = math.hypot(p_from[0] - cx, p_from[1] - cy)
    r1 = math.hypot(p_to[0] - cx, p_to[1] - cy)
    da = a1 - a0
    if ccw:
        while da <= 1e-9:
            da += 2 * math.pi
    else:
        while da >= -1e-9:
            da -= 2 * math.pi
    out: List[List[float]] = []
    for i in range(1, segments + 1):
        t = i / segments
        ang = a0 + da * t
        r = r0 + (r1 - r0) * t
        out.append([cx + r * math.cos(ang), cy + r * math.sin(ang)])
    return out


def _edges_of(spec: dict) -> List[dict]:
    """Lines and arcs as a uniform edge list, in spec order."""
    edges: List[dict] = []
    for line in spec.get("lines", []):
        edges.append({"a": line["a"], "b": line["b"], "kind": "line"})
    for arc in spec.get("arcs", []):
        edges.append({
            "a": arc["a"],
            "b": arc["b"],
            "kind": "arc",
            "center": arc["center"],
            "ccw": bool(arc.get("ccw", True)),
        })
    return edges


def _walk_loop(edges: List[dict], used: List[bool], start_index: int):
    """Chain edges from ``edges[start_index]`` until they close or run out.

    Yields ``(edge, from_pid, to_pid)`` — the traversal direction matters,
    because an arc stored ``b→a`` sweeps the other way round.
    """
    start = edges[start_index]["a"]
    cur = start
    ordered: List[Tuple[dict, str, str]] = []
    for _ in range(len(edges)):
        found = None
        for i, edge in enumerate(edges):
            if used[i]:
                continue
            if edge["a"] == cur:
                found = (i, edge["a"], edge["b"])
                break
            if edge["b"] == cur:
                found = (i, edge["b"], edge["a"])
                break
        if found is None:
            break
        i, frm, to = found
        used[i] = True
        ordered.append((edges[i], frm, to))
        cur = to
        if cur == start:
            break
    return start, ordered


def _loop_points(pts, start: str, ordered, arc_segments: int) -> List[List[float]]:
    poly: List[List[float]] = [list(pts[start])]
    for edge, frm, to in ordered:
        if edge["kind"] == "line":
            poly.append(list(pts[to]))
        else:
            traverse_ccw = edge["ccw"] if frm == edge["a"] else (not edge["ccw"])
            poly.extend(_arc_points(pts[edge["center"]], pts[frm], pts[to],
                                    traverse_ccw, arc_segments))
    if len(poly) > 1 and poly[-1] == poly[0]:
        poly.pop()
    return poly


def sketch_to_polygon(spec: dict, solved: dict, arc_segments: int = 24) -> List[List[float]]:
    """The sketch's first closed loop as a ring of 2D points, arcs tessellated.

    Superseded by :func:`sketch_to_loops`, which keeps curves as curves and
    returns *every* loop. Kept because documents persisted before that carry
    ``{"type": "polygon"}`` profiles and must go on meaning exactly what they
    meant when they were saved — tessellation included.
    """
    edges = _edges_of(spec)
    if not edges:
        raise SketchError("sketch has no lines or arcs to form a profile")
    start, ordered = _walk_loop(edges, [False] * len(edges), 0)
    return _loop_points(solved["points"], start, ordered, arc_segments)


def sketch_to_loops(spec: dict, solved: dict) -> dict:
    """Every closed loop in the sketch, as typed curves rather than points.

    This is the profile the kernels actually want. `sketch_to_polygon` flattened
    a sketch to one ring of points before either kernel saw it, which cost three
    things that matter here: a circle-only sketch had no lines or arcs and so
    could not be extruded at all; an arc became 24 straight segments, so a
    sketched bore arrived as 24 planar faces and was invisible to everything in
    this codebase that counts cylindrical ones (`design_spec._c_hole`,
    `dfm_topology`'s smallest bore, `naming`'s face ids, STEP); and only the
    first loop was walked, so a profile with a hole in it was not expressible.

    Loops are unordered and unnested here — containment is worked out where the
    profile is evaluated, by `cad_kernel.geom.profile2d`, so the SDF and the
    B-rep cannot disagree about which loop is a hole.
    """
    pts = solved["points"]
    radii = solved.get("radii", {})
    loops: List[dict] = []

    edges = _edges_of(spec)
    used = [False] * len(edges)
    for index in range(len(edges)):
        if used[index]:
            continue
        start, ordered = _walk_loop(edges, used, index)
        if not ordered:
            continue
        curves = []
        for edge, frm, to in ordered:
            if edge["kind"] == "line":
                curves.append({"kind": "line", "a": list(pts[frm]), "b": list(pts[to])})
            else:
                curves.append({
                    "kind": "arc", "a": list(pts[frm]), "b": list(pts[to]),
                    "center": list(pts[edge["center"]]),
                    "ccw": edge["ccw"] if frm == edge["a"] else (not edge["ccw"]),
                })
        loops.append({"curves": curves})

    for circle in spec.get("circles", []):
        radius = float(radii.get(circle["id"], circle.get("radius", 0.01)))
        if radius <= 0:
            continue
        loops.append({"curves": [{"kind": "circle",
                                  "center": list(pts[circle["center"]]),
                                  "r": radius}]})

    if not loops:
        raise SketchError(
            "sketch has nothing closed to extrude — draw a closed chain of "
            "lines/arcs, or a circle")
    return {"type": "loops", "loops": loops}
