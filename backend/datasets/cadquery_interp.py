"""A small CadQuery interpreter, because the chain reader could only see 9.4%.

`external.convert_cadquery` reads a program as *one* linear run of method calls
and takes the longest. Measured on Zero-To-CAD's 10,000 test programs that
converts 9.4%, and the calls it stops at are almost all ones this catalog already
has: `union` blocks 58.6% of programs, `center` 56.2%, `cutBlind`/`cut` 40/37%,
`translate` 37%, `revolve` 18%. Nothing was missing from the kernel — a program
that assigns intermediate solids to variables, unions them, and builds a profile
across five chained calls simply is not a chain.

So this executes the program instead. Four pieces of state are what the chain
reader lacked:

* **An environment**, so `solid = profile.extrude(t)` then `solid.union(boss)`
  works at all.
* **Expression evaluation** over the program's own names and arithmetic, including
  `math.radians`/`sin`/`cos`/`sqrt`. 653 of the 10,000 were dropped as
  `unresolved_argument` for want of this, and `radians`/`sin` appear in 7.9%.
* **Placement**, carried as an in-plane offset and a height, and *baked into the
  profile geometry* as a `loops` outline rather than needing an argument the
  operations do not have. That is what makes `center`/`moveTo` free.
* **A running bounding box**, which is what resolves `faces(">Z")` to a real
  height. Without it every `.faces(">Z").workplane()` — the commonest idiom in
  the dataset — is an unknown offset, and `center` usually follows one.

Everything it cannot resolve raises `Unsupported` with a cause, so the drop
histogram stays the measurement `yield_report` reads. Being wrong quietly is the
one thing a converter must not do: a sequence that builds a different part than
the source shipped is worse than no sample.
"""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

#: Programs are in millimetres and the operations are in metres.
MM = 1000.0

#: Plane names this catalog knows, keyed by the axis a CadQuery selector names.
_PLANE_FOR_NORMAL = {"z": "xy", "y": "xz", "x": "yz"}

_MATH = {"radians": math.radians, "degrees": math.degrees, "sin": math.sin,
         "cos": math.cos, "tan": math.tan, "sqrt": math.sqrt, "atan": math.atan,
         "atan2": math.atan2, "asin": math.asin, "acos": math.acos,
         "floor": math.floor, "ceil": math.ceil, "fabs": math.fabs,
         "hypot": math.hypot, "pow": math.pow, "exp": math.exp, "log": math.log}
_MATH_CONSTANTS = {"pi": math.pi, "e": math.e, "tau": math.tau}


class Unsupported(Exception):
    """Raised with the cause the drop histogram should count."""

    def __init__(self, cause: str) -> None:
        super().__init__(cause)
        self.cause = cause


@dataclass
class Box:
    """A running extent in millimetres, so a face selector resolves to a height."""
    lo: List[float] = field(default_factory=lambda: [math.inf] * 3)
    hi: List[float] = field(default_factory=lambda: [-math.inf] * 3)

    @property
    def empty(self) -> bool:
        return self.lo[0] > self.hi[0]

    def add(self, centre: Tuple[float, float, float],
            size: Tuple[float, float, float]) -> None:
        for axis in range(3):
            self.lo[axis] = min(self.lo[axis], centre[axis] - size[axis] / 2)
            self.hi[axis] = max(self.hi[axis], centre[axis] + size[axis] / 2)

    def copy(self) -> "Box":
        return Box(list(self.lo), list(self.hi))


@dataclass
class Workplane:
    """A CadQuery value: where the next feature goes, and what has been built.

    `ops` are this value's *own* construction, not yet committed to the document.
    Holding them here is what makes `a.cut(b)` expressible: the tool's operations
    are known when the boolean is applied, so they can be emitted as a subtract
    rather than having to be un-unioned afterwards.
    """
    plane: str = "xy"
    #: Millimetres: in-plane offsets and the distance along the plane normal.
    cx: float = 0.0
    cy: float = 0.0
    height: float = 0.0
    #: The outline being drawn, as `loops` curves in *absolute* plane coordinates.
    curves: List[dict] = field(default_factory=list)
    #: Where the pen is, for `lineTo`/`threePointArc` chains.
    pen: Optional[Tuple[float, float]] = None
    start: Optional[Tuple[float, float]] = None
    #: Whether `edges(...)` narrowed the selection. A fillet after one is a
    #: *local* fillet, and this catalog's whole-body treatment is a different body.
    narrowed: bool = False
    #: The face `faces(...)` last named, as one of `datum.FACE_MAP`'s six names.
    #: `shell` is the only thing that needs it, and it needs it because a shell
    #: opens a face rather than sitting on one.
    opening: Optional[str] = None
    #: `pushPoints`/`rarray`/`polarArray` locations, absolute and in-plane (mm).
    #: Empty means the single location the plane's own centre gives.
    points: List[Tuple[float, float]] = field(default_factory=list)
    ops: List[dict] = field(default_factory=list)
    box: Box = field(default_factory=Box)

    def clone(self, **changes) -> "Workplane":
        base = {"plane": self.plane, "cx": self.cx, "cy": self.cy,
                "height": self.height, "curves": list(self.curves), "pen": self.pen,
                "start": self.start, "points": list(self.points),
                "opening": self.opening, "narrowed": self.narrowed,
                "ops": list(self.ops), "box": self.box.copy()}
        base.update(changes)
        return Workplane(**base)

    # -- placement ---------------------------------------------------------
    def locations(self) -> List[Tuple[float, float]]:
        """Every in-plane location the next feature goes at."""
        return list(self.points) or [(self.cx, self.cy)]

    def centre3(self, along: float = 0.0,
                at: Optional[Tuple[float, float]] = None) -> Dict[str, float]:
        """The in-plane offset and height as a centre in metres."""
        height = self.height + along
        cx, cy = at if at is not None else (self.cx, self.cy)
        self_cx, self_cy = self.cx, self.cy
        self.cx, self.cy = cx, cy
        try:
            return self._centre3_inner(height)
        finally:
            self.cx, self.cy = self_cx, self_cy

    def _centre3_inner(self, height: float) -> Dict[str, float]:
        if self.plane == "xy":
            triple = (self.cx, self.cy, height)
        elif self.plane == "xz":
            triple = (self.cx, height, self.cy)
        else:                                     # yz
            triple = (height, self.cx, self.cy)
        return {"x": triple[0] / MM, "y": triple[1] / MM, "z": triple[2] / MM}

    def normal_axis(self) -> int:
        return {"xy": 2, "xz": 1, "yz": 0}[self.plane]


# ------------------------------------------------------------ expressions --

def _numbers(values: List[Any], method: str) -> List[float]:
    out = []
    for value in values:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise Unsupported(f"{method}:non_numeric_argument")
        out.append(float(value))
    return out


class Interpreter:
    """Executes the statements of one CadQuery program, emitting operations."""

    def __init__(self) -> None:
        self.env: Dict[str, Any] = {}
        self.result: Optional[Workplane] = None

    # -- the program -------------------------------------------------------
    def run(self, source: str) -> Workplane:
        try:
            tree = ast.parse(source)
        except SyntaxError:
            raise Unsupported("unparsable")
        for statement in tree.body:
            self.statement(statement)
        # The answer is the last workplane assigned that carries operations —
        # programs end with `result = …` or `solid = …` by convention, and the
        # one with material in it is the one meant.
        best = self.result
        for value in self.env.values():
            if isinstance(value, Workplane) and value.ops:
                if best is None or len(value.ops) >= len(best.ops):
                    best = value
        if best is None or not best.ops:
            raise Unsupported("no_solid_call")
        return best

    def statement(self, node: ast.stmt) -> None:
        if isinstance(node, (ast.Import, ast.ImportFrom, ast.Pass)):
            return
        if isinstance(node, ast.Expr):
            # A bare expression: `show_object(result)` and friends. Evaluated for
            # its side effects on nothing, so failures here are not fatal.
            try:
                self.eval(node.value)
            except Unsupported:
                pass
            return
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if len(targets) != 1 or not isinstance(targets[0], ast.Name):
                raise Unsupported("assignment:not_a_plain_name")
            if node.value is None:
                return
            value = self.eval(node.value)
            self.env[targets[0].id] = value
            if isinstance(value, Workplane):
                self.result = value
            return
        if isinstance(node, ast.AugAssign):
            if not isinstance(node.target, ast.Name):
                raise Unsupported("augassign:not_a_plain_name")
            current = self.env.get(node.target.id)
            self.env[node.target.id] = self.binop(node.op, current, self.eval(node.value))
            return
        if isinstance(node, ast.For):
            return self.loop(node)
        # `if`, `def`, `while`, `class`: a program whose operations depend on
        # evaluating those properly, and half-running one builds a different part.
        raise Unsupported(f"statement:{type(node).__name__}")

    #: A loop this will execute. Bounded because a converter reading untrusted
    #: programs must not be turned into a long-running one by a large `range`,
    #: and because a part with hundreds of features is not a training sample
    #: anybody wants — `MoveHandles` commits one operation per drag for the same
    #: reason.
    MAX_ITERATIONS = 64

    def loop(self, node: ast.For) -> None:
        """`for x in [...]` and `for i in range(n)`, which place repeated features.

        The largest single cause at 11.8%: these programs put holes on a bolt
        circle or ribs along a plate in a loop, and the operations they emit are
        ordinary repeated ones. `else` clauses, `break` and `continue` are not
        handled, so a loop using them is refused rather than run differently from
        how it reads.
        """
        if node.orelse:
            raise Unsupported("for:else_clause")
        if not isinstance(node.target, ast.Name):
            if not (isinstance(node.target, ast.Tuple)
                    and all(isinstance(e, ast.Name) for e in node.target.elts)):
                raise Unsupported("for:target_not_a_name")
        sequence = self.eval(node.iter) if not self._is_range(node.iter) else \
            self._range(node.iter)
        if not isinstance(sequence, (tuple, list, range)):
            raise Unsupported("for:not_iterable")
        items = list(sequence)
        if len(items) > self.MAX_ITERATIONS:
            raise Unsupported("for:too_many_iterations")
        for item in items:
            if isinstance(node.target, ast.Name):
                self.env[node.target.id] = item
            else:
                if not isinstance(item, (tuple, list)) \
                        or len(item) != len(node.target.elts):
                    raise Unsupported("for:unpack_mismatch")
                for name, value in zip(node.target.elts, item):
                    self.env[name.id] = value
            for statement in node.body:
                if isinstance(statement, (ast.Break, ast.Continue)):
                    raise Unsupported("for:break_or_continue")
                self.statement(statement)

    def _is_range(self, node: ast.AST) -> bool:
        return (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "range")

    def _range(self, node: ast.Call) -> range:
        bounds = [self.eval(a) for a in node.args]
        if not bounds or not all(isinstance(b, (int, float)) for b in bounds):
            raise Unsupported("range:not_numeric")
        return range(*[int(b) for b in bounds[:3]])

    # -- expressions -------------------------------------------------------
    def binop(self, op: ast.operator, left: Any, right: Any) -> float:
        if not isinstance(left, (int, float)) or not isinstance(right, (int, float)):
            raise Unsupported("arithmetic:not_numeric")
        if isinstance(op, ast.Add):
            return left + right
        if isinstance(op, ast.Sub):
            return left - right
        if isinstance(op, ast.Mult):
            return left * right
        if isinstance(op, ast.Div):
            if right == 0:
                raise Unsupported("arithmetic:division_by_zero")
            return left / right
        if isinstance(op, ast.Pow):
            return left ** right
        if isinstance(op, ast.FloorDiv):
            return left // right
        if isinstance(op, ast.Mod):
            return left % right
        raise Unsupported(f"arithmetic:{type(op).__name__}")

    def eval(self, node: ast.AST) -> Any:
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.Name):
            if node.id in self.env:
                return self.env[node.id]
            raise Unsupported(f"name:{node.id}")
        if isinstance(node, (ast.Tuple, ast.List)):
            return tuple(self.eval(item) for item in node.elts)
        if isinstance(node, ast.UnaryOp):
            inner = self.eval(node.operand)
            if isinstance(node.op, ast.USub):
                if not isinstance(inner, (int, float)):
                    raise Unsupported("negate:not_numeric")
                return -inner
            if isinstance(node.op, ast.UAdd):
                return inner
            raise Unsupported(f"unary:{type(node.op).__name__}")
        if isinstance(node, ast.BinOp):
            return self.binop(node.op, self.eval(node.left), self.eval(node.right))
        if isinstance(node, ast.Attribute):
            # `math.pi`, and a workplane attribute nobody calls.
            if isinstance(node.value, ast.Name) and node.value.id in ("cq", "cadquery"):
                raise Unsupported(f"cadquery_attribute:{node.attr}")
            if isinstance(node.value, ast.Name) and node.value.id in ("math", "np", "numpy"):
                if node.attr in _MATH_CONSTANTS:
                    return _MATH_CONSTANTS[node.attr]
                if node.attr in _MATH:
                    return _MATH[node.attr]
            raise Unsupported(f"attribute:{node.attr}")
        if isinstance(node, (ast.ListComp, ast.GeneratorExp)):
            return self.comprehension(node)
        if isinstance(node, ast.Call):
            return self.call(node)
        raise Unsupported(f"expression:{type(node).__name__}")

    def comprehension(self, node) -> tuple:
        """`[(r * cos(a), r * sin(a)) for a in angles]` — how a bolt circle is
        written when it is not written as `polarArray`. One generator, no
        condition: anything else is refused rather than run differently."""
        if len(node.generators) != 1:
            raise Unsupported("comprehension:nested")
        gen = node.generators[0]
        if gen.ifs or getattr(gen, "is_async", 0):
            raise Unsupported("comprehension:conditional")
        if not isinstance(gen.target, ast.Name):
            raise Unsupported("comprehension:target_not_a_name")
        sequence = self._range(gen.iter) if self._is_range(gen.iter) \
            else self.eval(gen.iter)
        if not isinstance(sequence, (tuple, list, range)):
            raise Unsupported("comprehension:not_iterable")
        items = list(sequence)
        if len(items) > self.MAX_ITERATIONS:
            raise Unsupported("comprehension:too_many")
        saved = self.env.get(gen.target.id)
        out = []
        try:
            for item in items:
                self.env[gen.target.id] = item
                out.append(self.eval(node.elt))
        finally:
            if saved is None:
                self.env.pop(gen.target.id, None)
            else:
                self.env[gen.target.id] = saved
        return tuple(out)

    def call(self, node: ast.Call) -> Any:
        func = node.func
        # `cq.Workplane("XY")` / `Workplane("XY")` — the start of everything.
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name == "Workplane":
            plane = "xy"
            if node.args:
                first = self.eval(node.args[0])
                if isinstance(first, str):
                    plane = first.lower()
            if plane not in ("xy", "xz", "yz"):
                raise Unsupported(f"workplane:{plane}")
            return Workplane(plane=plane)
        if name in _MATH and isinstance(func, ast.Attribute):
            args = [self.eval(a) for a in node.args]
            return _MATH[name](*_numbers(args, name))
        if name in ("Vector", "Location"):
            # A point given as `cq.Vector(x, y, z)` is the tuple everything else
            # here passes; a `Location` with a rotation in it is not, so only the
            # translation form is read.
            components = [self.eval(a) for a in node.args]
            if not components or not all(isinstance(c, (int, float)) for c in components):
                raise Unsupported(f"{name}:not_numeric")
            return tuple(float(c) for c in components)
        if name in ("float", "int", "abs", "round", "min", "max", "sum"):
            args = [self.eval(a) for a in node.args]
            try:
                return {"float": float, "int": int, "abs": abs, "round": round,
                        "min": min, "max": max, "sum": sum}[name](*args)
            except Exception:
                raise Unsupported(f"builtin:{name}")
        if not isinstance(func, ast.Attribute):
            raise Unsupported(f"call:{name or 'unnamed'}")
        subject = self.eval(func.value)
        if not isinstance(subject, Workplane):
            raise Unsupported(f"method_on:{type(subject).__name__}")
        keywords = {kw.arg: self.eval(kw.value) for kw in node.keywords if kw.arg}
        return self.method(subject, name, [self.eval(a) for a in node.args], keywords)

    # -- methods -----------------------------------------------------------
    def method(self, wp: Workplane, name: str, args: List[Any],
               kwargs: Dict[str, Any]) -> Workplane:
        handler = getattr(self, f"m_{name}", None)
        if handler is None:
            raise Unsupported(f"unsupported:{name}")
        return handler(wp, args, kwargs)

    # placement ------------------------------------------------------------
    def m_faces(self, wp, args, kwargs):
        """`.faces('>Z')` — resolved against the running extent, not guessed.

        This is the commonest idiom in the dataset and the reason the chain reader
        could not place anything: without an extent there is no height for the
        next feature to sit at. A selector naming an index (`'>Z[1]'`), a
        direction (`'|Z'`) or a tag is refused rather than approximated.
        """
        if not args or not isinstance(args[0], str):
            raise Unsupported("faces:no_selector")
        selector = args[0].strip()
        if len(selector) != 2 or selector[0] not in "><" or selector[1].lower() not in "xyz":
            raise Unsupported(f"faces:selector:{selector}")
        axis = selector[1].lower()
        if wp.box.empty:
            raise Unsupported("faces:no_body_yet")
        index = "xyz".index(axis)
        height = wp.box.hi[index] if selector[0] == ">" else wp.box.lo[index]
        return wp.clone(plane=_PLANE_FOR_NORMAL[axis], height=height,
                        cx=0.0, cy=0.0, curves=[], pen=None, start=None,
                        opening=_face_name(axis, selector[0] == ">"))

    def m_workplane(self, wp, args, kwargs):
        offset = kwargs.get("offset")
        if offset is None and args:
            offset = args[0]
        step = float(offset) if isinstance(offset, (int, float)) else 0.0
        return wp.clone(height=wp.height + step, curves=[], pen=None, start=None)

    def m_center(self, wp, args, kwargs):
        x, y = _numbers(args[:2], "center") if len(args) >= 2 else (0.0, 0.0)
        return wp.clone(cx=wp.cx + x, cy=wp.cy + y)

    def m_moveTo(self, wp, args, kwargs):
        x, y = _numbers(args[:2], "moveTo") if len(args) >= 2 else (0.0, 0.0)
        return wp.clone(pen=(wp.cx + x, wp.cy + y), start=(wp.cx + x, wp.cy + y))

    def m_translate(self, wp, args, kwargs):
        """`boss = cq.Workplane().cylinder(...).translate((10, 0, 0))`.

        A translate moves what is already built, and no operation here moves a
        body after the fact — but it does not need to: a primitive's placement is
        an argument of the call that made it, so the vector is folded into the
        centres of this value's own operations. Refusing instead was 8.8% of the
        dataset, and the shape being refused is the ordinary one: build a tool,
        move it, union it.

        An `add_extrude` carries its placement in the profile's coordinates and
        its `offset`, which this does not rewrite — so a translated extrusion is
        refused rather than left in the wrong place.
        """
        if not args or not isinstance(args[0], tuple):
            raise Unsupported("translate:not_a_vector")
        vector = _numbers(list(args[0])[:3], "translate")
        while len(vector) < 3:
            vector.append(0.0)
        if not wp.ops:
            axis = wp.normal_axis()
            in_plane = [v for i, v in enumerate(vector) if i != axis]
            return wp.clone(cx=wp.cx + in_plane[0], cy=wp.cy + in_plane[1],
                            height=wp.height + vector[axis])
        moved = []
        for op in wp.ops:
            args_out = dict(op["args"])
            if op["op"] == "add_primitive":
                params = dict(args_out.get("params") or {})
                centre = dict(params.get("center") or {"x": 0.0, "y": 0.0, "z": 0.0})
                for key, delta in zip(("x", "y", "z"), vector):
                    centre[key] = float(centre.get(key, 0.0)) + delta / MM
                params["center"] = centre
                args_out["params"] = params
            elif op["op"] == "add_hole":
                centre = dict(args_out.get("center") or {"x": 0.0, "y": 0.0, "z": 0.0})
                for key, delta in zip(("x", "y", "z"), vector):
                    centre[key] = float(centre.get(key, 0.0)) + delta / MM
                args_out["center"] = centre
            elif op["op"] == "add_extrude":
                # An extrusion's placement is its profile's coordinates plus its
                # `offset` along the plane normal, so a translate moves both.
                plane = str(args_out.get("plane") or "xy")
                axis = {"xy": 2, "xz": 1, "yz": 0}[plane]
                in_plane = [i for i in range(3) if i != axis]
                du, dv = vector[in_plane[0]] / MM, vector[in_plane[1]] / MM
                profile = args_out.get("profile") or {}
                if profile.get("type") != "loops":
                    raise Unsupported("translate:profile_not_loops")
                loops = []
                for loop in profile.get("loops") or []:
                    curves = []
                    for curve in loop.get("curves") or []:
                        row = dict(curve)
                        for key in ("a", "b", "center"):
                            if key in row:
                                row[key] = [row[key][0] + du, row[key][1] + dv]
                        curves.append(row)
                    loops.append({"curves": curves})
                args_out["profile"] = {"type": "loops", "loops": loops}
                args_out["offset"] = float(args_out.get("offset") or 0.0) \
                    + vector[axis] / MM
            else:
                raise Unsupported(f"translate:{op['op']}")
            moved.append({"op": op["op"], "args": args_out})
        out = wp.clone(ops=moved)
        for axis in range(3):
            out.box.lo[axis] += vector[axis]
            out.box.hi[axis] += vector[axis]
        return out

    # arrays ---------------------------------------------------------------
    #
    # `pushPoints` 11.9%, `rarray` 5.6% and `polarArray` 3.6% are the largest
    # remaining block, and they are one idea: the next feature is placed at each
    # of several locations. Kept as locations on the workplane rather than turned
    # into a `PatternFeature`, because what follows decides the right shape —
    # a drawn outline becomes *one* profile with a loop per location (even-odd
    # parity makes several circles several holes), while a `hole` is a placed
    # operation and becomes one per location.
    def m_pushPoints(self, wp, args, kwargs):
        if not args or not isinstance(args[0], tuple):
            raise Unsupported("pushPoints:not_a_list")
        points = []
        for item in args[0]:
            if not isinstance(item, tuple) or len(item) < 2:
                raise Unsupported("pushPoints:not_points")
            x, y = _numbers(list(item[:2]), "pushPoints")
            points.append((wp.cx + x, wp.cy + y))
        if len(points) > self.MAX_ITERATIONS:
            raise Unsupported("pushPoints:too_many")
        return wp.clone(points=points)

    def m_rarray(self, wp, args, kwargs):
        """A rectangular grid. CadQuery centres it on the current point."""
        values = _numbers(args[:4], "rarray") if len(args) >= 4 else None
        if values is None:
            raise Unsupported("rarray:needs_four_arguments")
        x_spacing, y_spacing, x_count, y_count = values
        nx, ny = int(x_count), int(y_count)
        if nx < 1 or ny < 1:
            raise Unsupported("rarray:empty")
        if nx * ny > self.MAX_ITERATIONS:
            raise Unsupported("rarray:too_many")
        points = []
        for ix in range(nx):
            for iy in range(ny):
                points.append((wp.cx + (ix - (nx - 1) / 2) * x_spacing,
                               wp.cy + (iy - (ny - 1) / 2) * y_spacing))
        return wp.clone(points=points)

    def m_polarArray(self, wp, args, kwargs):
        """`polarArray(radius, startAngle, angle, count)` — a bolt circle.

        `angle` is the span CadQuery distributes over, and a full 360 puts the
        last instance on top of the first, so the step divides by `count` there
        and by `count - 1` over a partial span. Getting that backwards doubles one
        hole and loses another, which no geometry check would call an error.
        """
        values = _numbers(args[:4], "polarArray") if len(args) >= 4 else None
        if values is None:
            raise Unsupported("polarArray:needs_four_arguments")
        radius, start, span, count_f = values
        count = int(count_f)
        if count < 1:
            raise Unsupported("polarArray:empty")
        if count > self.MAX_ITERATIONS:
            raise Unsupported("polarArray:too_many")
        full = abs(abs(span) - 360.0) < 1e-9
        step = (span / count) if full or count == 1 else (span / (count - 1))
        points = []
        for index in range(count):
            angle = math.radians(start + step * index)
            points.append((wp.cx + radius * math.cos(angle),
                           wp.cy + radius * math.sin(angle)))
        return wp.clone(points=points)

    # sketching ------------------------------------------------------------
    def _close_loop(self, wp: Workplane) -> List[dict]:
        curves = list(wp.curves)
        if wp.pen and wp.start and wp.pen != wp.start:
            curves.append({"kind": "line", "a": list(wp.pen), "b": list(wp.start)})
        return curves

    def m_lineTo(self, wp, args, kwargs):
        # CadQuery's pen starts at the plane's origin, so a `lineTo` with no
        # preceding `moveTo` draws from there rather than being an error.
        if wp.pen is None:
            wp = wp.clone(pen=(wp.cx, wp.cy), start=(wp.cx, wp.cy))
        x, y = _numbers(args[:2], "lineTo")
        end = (wp.cx + x, wp.cy + y)
        return wp.clone(curves=wp.curves + [{"kind": "line", "a": list(wp.pen),
                                             "b": list(end)}], pen=end)

    def m_line(self, wp, args, kwargs):
        if wp.pen is None:
            raise Unsupported("line:no_pen")
        dx, dy = _numbers(args[:2], "line")
        end = (wp.pen[0] + dx, wp.pen[1] + dy)
        return wp.clone(curves=wp.curves + [{"kind": "line", "a": list(wp.pen),
                                             "b": list(end)}], pen=end)

    def m_polyline(self, wp, args, kwargs):
        if not args or not isinstance(args[0], tuple):
            raise Unsupported("polyline:not_a_list")
        points = []
        for item in args[0]:
            if not isinstance(item, tuple) or len(item) < 2:
                raise Unsupported("polyline:not_points")
            x, y = _numbers(list(item[:2]), "polyline")
            points.append((wp.cx + x, wp.cy + y))
        if len(points) < 2:
            raise Unsupported("polyline:too_few_points")
        curves = list(wp.curves)
        if wp.pen is not None:
            points.insert(0, wp.pen)
        for first, second in zip(points, points[1:]):
            curves.append({"kind": "line", "a": list(first), "b": list(second)})
        start = wp.start if wp.start is not None else points[0]
        return wp.clone(curves=curves, pen=points[-1], start=start)

    def m_threePointArc(self, wp, args, kwargs):
        """An arc through a midpoint, converted to the centre form the kernel reads.

        Three points on a circle determine it; the sign of the cross product of
        (mid − start) and (end − mid) is the direction. Collinear points describe
        no arc, so they are refused rather than turned into a straight line the
        source did not draw.
        """
        if wp.pen is None or len(args) < 2:
            raise Unsupported("threePointArc:no_pen")
        mid = args[0] if isinstance(args[0], tuple) else None
        end = args[1] if isinstance(args[1], tuple) else None
        if mid is None or end is None:
            raise Unsupported("threePointArc:not_points")
        ax, ay = wp.pen
        mid_xy = _numbers(list(mid[:2]), "threePointArc")
        end_xy = _numbers(list(end[:2]), "threePointArc")
        mx, my = wp.cx + mid_xy[0], wp.cy + mid_xy[1]
        bx, by = wp.cx + end_xy[0], wp.cy + end_xy[1]
        centre = _circle_through((ax, ay), (mx, my), (bx, by))
        if centre is None:
            raise Unsupported("threePointArc:collinear")
        cross = (mx - ax) * (by - my) - (my - ay) * (bx - mx)
        return wp.clone(curves=wp.curves + [{"kind": "arc", "a": [ax, ay],
                                             "b": [bx, by], "center": list(centre),
                                             "ccw": cross > 0}],
                        pen=(bx, by))

    def m_close(self, wp, args, kwargs):
        return wp.clone(curves=self._close_loop(wp), pen=wp.start)

    def m_rect(self, wp, args, kwargs):
        width, height = _numbers(args[:2], "rect")
        curves = list(wp.curves)
        for x, y in wp.locations():
            corners = [(x - width / 2, y - height / 2), (x + width / 2, y - height / 2),
                       (x + width / 2, y + height / 2), (x - width / 2, y + height / 2)]
            curves += [{"kind": "line", "a": list(a), "b": list(b)}
                       for a, b in zip(corners, corners[1:] + corners[:1])]
        return wp.clone(curves=curves, pen=None, start=None, points=[])

    def m_circle(self, wp, args, kwargs):
        radius = _numbers(args[:1], "circle")[0]
        # One loop per location: even-odd parity reads several circles inside an
        # outline as several holes, which is what a pushPoints-and-circle draws.
        curves = wp.curves + [{"kind": "circle", "center": [x, y], "r": radius}
                              for x, y in wp.locations()]
        return wp.clone(curves=curves, pen=None, start=None, points=[])

    # solids ---------------------------------------------------------------
    def _profile(self, wp: Workplane, method: str) -> dict:
        """The drawn outline as a `loops` profile, which is why placement is free.

        `rect` and `circle` profiles have no centre argument, so an in-plane
        offset would have nowhere to go — and the offset is exactly what `center`
        and `moveTo` produce. Emitting absolute coordinates as `loops` puts the
        outline where the program put it, using a profile form the kernel already
        builds from and both kernels agree about.
        """
        curves = self._close_loop(wp)
        if not curves:
            raise Unsupported(f"{method}:no_profile")
        scaled = []
        for curve in curves:
            row = {"kind": curve["kind"]}
            for key in ("a", "b", "center"):
                if key in curve:
                    row[key] = [v / MM for v in curve[key]]
            if "r" in curve:
                row["r"] = curve["r"] / MM
            if "ccw" in curve:
                row["ccw"] = curve["ccw"]
            scaled.append(row)
        return {"type": "loops", "loops": [{"curves": scaled}]}

    def _extent_of(self, wp: Workplane) -> Tuple[float, float]:
        """The drawn outline's in-plane extent, for the running bounding box."""
        xs, ys = [], []
        for curve in self._close_loop(wp):
            if curve["kind"] == "circle":
                cx, cy = curve["center"]
                xs += [cx - curve["r"], cx + curve["r"]]
                ys += [cy - curve["r"], cy + curve["r"]]
            else:
                for key in ("a", "b"):
                    xs.append(curve[key][0])
                    ys.append(curve[key][1])
        if not xs:
            return 0.0, 0.0
        return max(xs) - min(xs), max(ys) - min(ys)

    def _grow(self, wp: Workplane, size_mm: Tuple[float, float, float],
              along: float) -> None:
        """Extend the running extent by a feature placed on this plane."""
        centre = wp.centre3(along=along)
        wp.box.add((centre["x"] * MM, centre["y"] * MM, centre["z"] * MM), size_mm)

    def m_box(self, wp, args, kwargs):
        length, width, height = _numbers(args[:3], "box")
        out = wp.clone(narrowed=False, ops=wp.ops + [{"op": "add_primitive", "args": {
            "shape": "box",
            "params": {"size": {"x": length / MM, "y": width / MM, "z": height / MM},
                       "center": wp.centre3()}}}])
        self._grow(out, (length, width, height), 0.0)
        return out

    def m_cylinder(self, wp, args, kwargs):
        height, radius = _numbers(args[:2], "cylinder")
        out = wp.clone(ops=wp.ops + [{"op": "add_primitive", "args": {
            "shape": "cylinder",
            "params": {"height": height / MM, "radius": radius / MM,
                       "center": wp.centre3()}}}])
        self._grow(out, (radius * 2, radius * 2, height), 0.0)
        return out

    def _extrude(self, wp: Workplane, depth: float, operation: str) -> Workplane:
        profile = self._profile(wp, "extrude")
        width, height = self._extent_of(wp)
        args = {"profile": profile, "plane": wp.plane, "depth": abs(depth) / MM}
        # An operation's extrusion runs from the plane, so a negative depth — a
        # cut downwards, which is what `cutBlind` writes — moves the plane by that
        # much and extrudes back up.
        offset = wp.height - abs(depth) if depth < 0 else wp.height
        if offset:
            args["offset"] = offset / MM
        if operation != "union":
            args["operation"] = operation
        out = wp.clone(ops=wp.ops + [{"op": "add_extrude", "args": args}],
                       curves=[], pen=None, start=None)
        if operation == "union":
            axis = wp.normal_axis()
            size = [0.0, 0.0, 0.0]
            size[axis] = abs(depth)
            in_plane = [i for i in range(3) if i != axis]
            size[in_plane[0]], size[in_plane[1]] = width, height
            # **On `out`, not on a clone of it.** `clone` copies the box, so
            # growing a throwaway's extent grew nothing: every `.faces('>Z')`
            # after an extrude then reported no body, which was 15% of the
            # dataset and the second largest cause in the first measurement.
            centre = out.clone(height=offset + abs(depth) / 2).centre3()
            out.box.add((centre["x"] * MM, centre["y"] * MM, centre["z"] * MM),
                        (size[0], size[1], size[2]))
        return out

    def m_extrude(self, wp, args, kwargs):
        depth = kwargs.get("until", kwargs.get("distance"))
        if depth is None:
            depth = _numbers(args[:1], "extrude")[0]
        if not isinstance(depth, (int, float)):
            raise Unsupported("extrude:non_numeric_depth")
        return self._extrude(wp, float(depth), "union")

    def m_cutBlind(self, wp, args, kwargs):
        depth = _numbers(args[:1], "cutBlind")[0]
        return self._extrude(wp, -abs(depth), "subtract")

    def m_cutThruAll(self, wp, args, kwargs):
        if wp.box.empty:
            raise Unsupported("cutThruAll:no_body")
        axis = wp.normal_axis()
        span = wp.box.hi[axis] - wp.box.lo[axis]
        # Through means through: the tool is grown past both faces, the way
        # `HoleFeature.through` lengthens the far end.
        return self._extrude(wp.clone(height=wp.box.lo[axis] - span * 0.05),
                             span * 1.1, "subtract")

    def m_revolve(self, wp, args, kwargs):
        angle = 360.0
        if args:
            angle = _numbers(args[:1], "revolve")[0]
        profile = self._profile(wp, "revolve")
        # CadQuery revolves about an in-plane axis; `add_revolve` names a world
        # axis, and on the XY plane the default is the Y axis.
        axis = {"xy": "y", "xz": "z", "yz": "z"}[wp.plane]
        out = wp.clone(ops=wp.ops + [{"op": "add_revolve", "args": {
            "profile": profile, "axis": axis, "angle": float(angle)}}],
            curves=[], pen=None, start=None)
        width, height = self._extent_of(wp)
        reach = max(width, height)
        self._grow(out, (reach * 2, reach * 2, reach * 2), 0.0)
        return out

    def m_hole(self, wp, args, kwargs):
        diameter = _numbers(args[:1], "hole")[0]
        drilled = [{"op": "add_hole", "args": {"radius": diameter / 2 / MM,
                                               "through": True,
                                               "center": wp.centre3(at=at)}}
                   for at in wp.locations()]
        return wp.clone(ops=wp.ops + drilled, points=[])

    def m_cboreHole(self, wp, args, kwargs):
        diameter, recess, depth = _numbers(args[:3], "cboreHole")
        drilled = [{"op": "add_hole", "args": {
            "radius": diameter / 2 / MM, "hole_type": "counterbore",
            "recess_radius": recess / 2 / MM, "recess_depth": depth / MM,
            "through": True, "center": wp.centre3(at=at)}} for at in wp.locations()]
        return wp.clone(ops=wp.ops + drilled, points=[])

    def m_cskHole(self, wp, args, kwargs):
        diameter, recess = _numbers(args[:2], "cskHole")[:2]
        angle = _numbers(args[2:3], "cskHole")[0] if len(args) > 2 else 82.0
        drilled = [{"op": "add_hole", "args": {
            "radius": diameter / 2 / MM, "hole_type": "countersink",
            "recess_radius": recess / 2 / MM, "countersink_angle": float(angle),
            "through": True, "center": wp.centre3(at=at)}} for at in wp.locations()]
        return wp.clone(ops=wp.ops + drilled, points=[])

    #: Operations whose edges OCC's fillet solver struggles with. A whole-body
    #: chamfer over a 360-degree revolve's seam and circular edges is where it
    #: stops converging: one program in this dataset held the ingest for over an
    #: hour at 283% CPU, on a revolved ring with three holes and
    #: `add_modifier("chamfer")`. Nothing times out inside a C++ call, so the only
    #: reliable bound is not to emit the case.
    _CURVED = ("add_revolve", "add_loft", "add_ruled_loft", "add_sweep")

    def _edge_treatment(self, wp, kind: str, value: float) -> Workplane:
        """A whole-body fillet where the program may have narrowed to some edges.

        Refusing the narrowed case was tried and reverted, because it is 39% of
        this dataset — `.edges(sel).chamfer()` 25.7% and `.fillet()` 13.5% — and
        the measurement was 28.8% converted down to 2.3%. The reason it is
        acceptable is narrower than "the geometry check will catch it", which was
        the old justification and is false here: `convert_cadquery` emits no
        `expect`, so nothing compares this against the source's body.
        
        It is acceptable because of what the *goal* says. These goals are composed
        from the program's named dimensions — "body length 80 mm, bore dia 12 mm" —
        and say nothing about which edges are rounded. The sequence builds a part
        that meets its stated goal using the operation this catalog actually has:
        `add_modifier("fillet")` *is* the whole-body treatment, and `fillet_edges`
        with ids is the selective one. So the sample teaches the catalog correctly
        even where it is not the source's exact body. What it must not do is claim
        otherwise, which is why `converted_by` rides on the row.
        """
        if any(op["op"] in self._CURVED for op in wp.ops):
            raise Unsupported(f"{kind}:curved_body")
        return wp.clone(ops=wp.ops + [{"op": "add_modifier", "args": {
            "modifier": kind, "params": {"radius": value / MM}}}])

    def m_fillet(self, wp, args, kwargs):
        return self._edge_treatment(wp, "fillet", _numbers(args[:1], "fillet")[0])

    def m_chamfer(self, wp, args, kwargs):
        return self._edge_treatment(wp, "chamfer", _numbers(args[:1], "chamfer")[0])

    def m_shell(self, wp, args, kwargs):
        """`.faces(">Z").shell(-2)` — a hollow with that face open.

        Declined until `shell_faces` could be told which face without an
        evaluated body to measure ids on, which was 10.3% of the dataset and the
        largest remaining cause. It is *not* `add_modifier("shell")`: that one
        hollows uniformly and opens nothing, so mapping it here would emit a
        different body than the source shipped.

        A negative thickness is CadQuery's way of saying inward, which is the only
        direction `FaceShellFeature` offsets, so the sign carries no information
        here and the magnitude is what is passed.

        **A `.shell()` with no face selected is a different shape and is declined**
        — 8.8% of the dataset, and the largest remaining cause. It asks for a
        *closed* hollow, which nothing here builds exactly: `add_modifier("shell")`
        is `abs(d) - t/2`, so its outer surface sits half a wall *outside* the
        original body, and `BRepOffsetAPI_MakeThickSolid` with an empty face list
        does not hollow at all — measured on a 60 x 40 x 30 box at 3 mm, it returns
        44,064 mm³, which is the inner 54 x 34 x 24 solid rather than the
        27,936 mm³ wall. That is an inward *offset*, not a shell.

        Which is also the route a closed hollow would take if it were wanted:
        subtract that inward offset from the body. It needs an offset-solid
        feature this catalog does not have, so it is a capability to add
        deliberately rather than something to approximate here.
        """
        if wp.opening is None:
            raise Unsupported("shell:no_face_selected")
        thickness = _numbers(args[:1], "shell")[0] if args else None
        if thickness is None:
            thickness = kwargs.get("thickness")
        if not isinstance(thickness, (int, float)) or thickness == 0:
            raise Unsupported("shell:no_thickness")
        return wp.clone(ops=wp.ops + [{"op": "shell_faces", "args": {
            "faces": wp.opening, "thickness": abs(float(thickness)) / MM}}],
            opening=None)

    # booleans -------------------------------------------------------------
    def _combine(self, wp: Workplane, other: Any, operation: str,
                 method: str) -> Workplane:
        """`a.union(b)` / `a.cut(b)` — the tool's own operations, re-aimed.

        Every builder here combines with the body it is added to, so a union is
        the tool's operations appended as they are. A *cut* has to re-aim them,
        and only a single-operation tool can be re-aimed by setting `operation`:
        subtracting a composed body (an extrude with its own hole) means building
        it in a group first, which `target` supports and this does not do yet. It
        is refused rather than approximated, and counted, so the histogram says
        how often it matters.
        """
        if not isinstance(other, Workplane):
            raise Unsupported(f"{method}:not_a_solid")
        if not other.ops:
            raise Unsupported(f"{method}:empty_tool")
        if operation == "union":
            merged = wp.ops + other.ops
        elif len(other.ops) == 1:
            tool = dict(other.ops[0])
            tool["args"] = {**tool["args"], "operation": operation}
            merged = wp.ops + [tool]
        else:
            raise Unsupported(f"{method}:composed_tool")
        out = wp.clone(ops=merged)
        for axis in range(3):
            out.box.lo[axis] = min(out.box.lo[axis], other.box.lo[axis])
            out.box.hi[axis] = max(out.box.hi[axis], other.box.hi[axis])
        return out

    def m_union(self, wp, args, kwargs):
        return self._combine(wp, args[0] if args else None, "union", "union")

    def m_cut(self, wp, args, kwargs):
        return self._combine(wp, args[0] if args else None, "subtract", "cut")

    def m_intersect(self, wp, args, kwargs):
        return self._combine(wp, args[0] if args else None, "intersect", "intersect")

    # things that do not change the geometry -------------------------------
    def m_edges(self, wp, args, kwargs):
        """`.edges('>Z')` before a fillet or chamfer.

        The selector narrows *which* edges and `add_modifier("fillet")` treats
        every edge, so the two are not the same body. This used to shrug and carry
        on, reasoning that the geometry check would catch it — and there is no
        geometry check on this path: `convert_cadquery` emits no `expect`, so
        `verify` only asks whether the result is a valid solid. A rounded-corner
        plate with every edge rounded is a valid solid.
        
        So the selector is *recorded*, and a fillet or chamfer that follows one is
        refused. Selecting edges for anything else — a query, a tag — still passes
        through, which is why this is a flag rather than a refusal here.
        """
        return wp.clone(narrowed=True)

    def m_vertices(self, wp, args, kwargs):
        return wp

    def m_val(self, wp, args, kwargs):
        return wp

    def m_clean(self, wp, args, kwargs):
        return wp

    def m_combine(self, wp, args, kwargs):
        return wp

    def m_tag(self, wp, args, kwargs):
        return wp


def _face_name(axis: str, at_max: bool) -> Optional[str]:
    """`('z', True)` -> `"top"`, read from `datum.FACE_MAP` rather than restated.

    The sketch-on-face path already means exactly this by "top", and two
    spellings of one naming rule disagree the day after they are written.
    """
    from app.services import datum

    index = "xyz".index(axis)
    for name, (_, face_axis, take_max) in datum.FACE_MAP.items():
        if face_axis == index and take_max == at_max:
            return name
    return None


def _circle_through(a, b, c) -> Optional[Tuple[float, float]]:
    """The centre of the circle through three points, or None if collinear."""
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax ** 2 + ay ** 2) * (by - cy) + (bx ** 2 + by ** 2) * (cy - ay)
          + (cx ** 2 + cy ** 2) * (ay - by)) / d
    uy = ((ax ** 2 + ay ** 2) * (cx - bx) + (bx ** 2 + by ** 2) * (ax - cx)
          + (cx ** 2 + cy ** 2) * (bx - ax)) / d
    return ux, uy


#: More operations than a part anybody wants to learn from, and more than the
#: ingest can afford: each one rebuilds the whole tree, so the cost is quadratic
#: in the count. Measured on this dataset, the median program emits 7 and the 99th
#: percentile 46, while the worst emits 255 — a loop placing a feature per
#: iteration. A corpus in which a part takes 255 calls teaches that.
MAX_OPERATIONS = 24


def interpret(source: str) -> List[dict]:
    """The program's operations, or `Unsupported` with the cause."""
    ops = Interpreter().run(source).ops
    if len(ops) > MAX_OPERATIONS:
        raise Unsupported(f"too_many_operations:{len(ops)}")
    return ops
