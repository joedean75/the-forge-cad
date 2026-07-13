"""Prompt-driven assembly generator for the Forge CAD kernel.

Produces richer, parameter-aware assemblies from natural-language prompts.
Each node is a real kernel solid, and struts describe the structural
connections between nodes.
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from app.cad_kernel.core.feature_tree import FeatureTree
from app.cad_kernel.core.types import Point3
from app.cad_kernel.ops.primitives import SphereFeature
from app.models.schemas import AssemblyResult, NodePart, Strut, Vector3
from app.services.mesh_generator import candidate_from_feature_tree
from app.services import persistence


# In-memory store of generated assemblies.
ASSEMBLY_DATA: Dict[str, AssemblyResult] = {}


@dataclass
class LayoutNode:
    id: str
    position: Tuple[float, float, float]
    radius: float = 0.05


@dataclass
class LayoutStrut:
    id: str
    start_id: str
    end_id: str
    diameter: float = 0.04


@dataclass
class AssemblyLayout:
    nodes: List[LayoutNode] = field(default_factory=list)
    struts: List[LayoutStrut] = field(default_factory=list)


@dataclass
class AssemblySpec:
    kind: str = "truss"
    span: float = 1.0
    height: float = 0.25
    width: float = 0.3
    bays: int = 3
    levels: int = 3
    complexity: int = 2  # 1 = simple, 2 = normal, 3 = heavy/detailed


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _parse_number_with_unit(text: str) -> Optional[float]:
    """Parse a length in meters from a string like '2m', '100mm', '1.5'."""
    from app.services.text_to_cad import _parse_number_with_unit as _parse

    return _parse(text)


_ALL_DIM_KEYWORDS = [
    "span",
    "long",
    "length",
    "height",
    "tall",
    "high",
    "width",
    "wide",
    "deep",
    "depth",
]

_DIM_GROUPS = {
    "span": ["span", "long", "length", "jib", "boom", "reach"],
    "height": ["height", "tall", "high"],
    "width": ["width", "wide", "deep", "depth"],
}


def _extract_all_dimensions(prompt: str) -> Dict[str, Optional[float]]:
    """Extract span/height/width by pairing keywords with nearest numbers."""
    tokens = re.findall(r"\S+", prompt.lower())
    result: Dict[str, Optional[float]] = {"span": None, "height": None, "width": None}

    def _number_at(idx: int) -> Optional[float]:
        """Parse a token like '3', '3m', '0.6' into meters, including a
        separated unit token immediately before or after it."""
        if 0 <= idx < len(tokens):
            m = re.fullmatch(r"(\d+\.?\d*)(mm|cm|m|in|inch)?", tokens[idx])
            if m:
                return _parse_number_with_unit(m.group(1) + (m.group(2) or ""))
        return None

    for i, tok in enumerate(tokens):
        for dim, kws in _DIM_GROUPS.items():
            if result[dim] is not None:
                continue
            if any(tok == kw or tok.startswith(kw + ":") for kw in kws):
                # Search forward first (e.g. "width 0.6 m").
                for j in range(i + 1, len(tokens)):
                    if any(k in tokens[j] for k in _ALL_DIM_KEYWORDS):
                        break
                    value = _number_at(j)
                    if value is not None:
                        result[dim] = value
                        break
                # If not found, look backward (e.g. "0.6 m width").
                if result[dim] is None:
                    for j in range(i - 1, -1, -1):
                        if any(k in tokens[j] for k in _ALL_DIM_KEYWORDS):
                            break
                        value = _number_at(j)
                        if value is not None:
                            result[dim] = value
                            break
                break
    return result


def _parse_dimension(prompt: str, default: float) -> float:
    """Look for the first bare dimension (e.g. '2 m', '100 mm') and return meters."""
    m = re.search(r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?", prompt)
    if m:
        value = _parse_number_with_unit(f"{m.group(1)}{m.group(2) or ''}")
        if value is not None:
            return value
    return default


_SIZE_RE = re.compile(
    r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*[xX×*]\s*"
    r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?"
    r"(?:\s*[xX×*]\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?)?"
)
_BY_RE = re.compile(
    r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?\s*(?:by|x)\s*"
    r"(\d+\.?\d*)\s*(mm|cm|m|in|inch)?"
    r"(?:\s*(?:by|x)\s*(\d+\.?\d*)\s*(mm|cm|m|in|inch)?)?"
)


def _parse_size_expression(prompt: str) -> List[float]:
    """Parse expressions like '2x1 m', '1.2x0.6x0.3 m' into meters."""
    lower = prompt.lower()
    m = _SIZE_RE.search(lower) or _BY_RE.search(lower)
    if not m:
        return []
    values: List[float] = []
    for i in range(0, 6, 2):
        val = m.group(i + 1)
        if val is None:
            break
        unit = m.group(i + 2) or ""
        parsed = _parse_number_with_unit(val + unit)
        if parsed is None:
            break
        values.append(parsed)
    return values


def _parse_count(prompt: str, keywords: List[str], default: int) -> int:
    """Look for an integer count associated with one of the keywords."""
    lower = prompt.lower()
    for keyword in keywords:
        # "3 bay", "3-bay", "three bay"
        m = re.search(r"(\d+)\s*[-]?\s*" + re.escape(keyword), lower)
        if m:
            return int(m.group(1))
        word_map = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
        for word, value in word_map.items():
            if re.search(rf"\b{word}\s*{re.escape(keyword)}", lower):
                return value
    return default


def _detect_complexity(prompt: str) -> int:
    lower = prompt.lower()
    if any(w in lower for w in ["simple", "basic", "minimal", "light"]):
        return 1
    if any(w in lower for w in ["heavy", "robust", "detailed", "complex", "dense"]):
        return 3
    return 2


_SPECIFIC_KIND_KEYWORDS = [
    "truss",
    "frame",
    "tower",
    "mast",
    "bridge",
    "space frame",
    "portal",
    "gantry",
    "door frame",
    "dome",
    "radial",
    "circular",
    "crane",
    "jib",
    "scissor",
    "lift",
    "platform",
    "shelf",
    "shelves",
    "rack",
    "table",
    "stand",
    "canopy",
    "awning",
    "roof",
]


def _has_specific_kind_keyword(prompt: str) -> bool:
    lower = prompt.lower()
    return any(kw in lower for kw in _SPECIFIC_KIND_KEYWORDS)


def _infer_kind_from_dims(span: float, height: float, width: float) -> str:
    """Pick a sensible default assembly kind from parsed dimensions."""
    if height > span * 1.3 and height > width * 1.3:
        return "tower"
    if span > height * 3.0 and span > width * 1.5:
        return "bridge"
    if width > span * 1.5 and width > height * 1.5:
        return "frame"
    if height < span * 0.15 and height < width * 0.5:
        return "platform"
    return "frame"


def _detect_kind(prompt: str) -> Optional[str]:
    """Return a concrete assembly kind keyword, or None if the prompt is too vague."""
    lower = prompt.lower()
    if "space frame" in lower:
        return "space_frame"
    if "bridge" in lower:
        return "bridge"
    if "tower" in lower or "mast" in lower:
        return "tower"
    if "portal" in lower or "door frame" in lower:
        return "portal"
    if "gantry" in lower:
        return "gantry"
    if "crane" in lower or "jib" in lower:
        return "crane"
    if "scissor" in lower or ("lift" in lower and "scissor" in lower):
        return "scissor"
    if "platform" in lower or "shelf" in lower or "shelves" in lower or "rack" in lower or "table" in lower or "stand" in lower:
        return "platform"
    if "dome" in lower or "radial" in lower or "circular" in lower:
        return "radial"
    if "arch" in lower or "vault" in lower:
        return "arch"
    if "cantilever" in lower:
        return "cantilever"
    if "truss" in lower or "canopy" in lower or "awning" in lower or "roof" in lower:
        return "truss"
    if "frame" in lower:
        return "frame"
    return None


def parse_assembly_prompt(prompt: str) -> AssemblySpec:
    """Map an assembly prompt to a concrete spec with dimensions and complexity."""
    detected_kind = _detect_kind(prompt)

    # Default scales tuned per kind.
    defaults = {
        "truss": (1.0, 0.25, 0.3),
        "frame": (0.8, 0.8, 0.8),
        "tower": (0.6, 1.2, 0.6),
        "bridge": (2.0, 0.4, 0.6),
        "space_frame": (1.2, 0.3, 0.8),
        "portal": (1.2, 0.8, 0.2),
        "gantry": (2.0, 1.0, 0.4),
        "crane": (1.5, 1.5, 0.4),
        "scissor": (1.0, 0.5, 0.1),
        "platform": (1.0, 0.05, 0.6),
        "radial": (1.0, 0.25, 1.0),
        "arch": (1.5, 0.6, 0.2),
        "cantilever": (1.0, 0.6, 0.2),
    }
    if detected_kind:
        span, height, width = defaults.get(detected_kind, (1.0, 0.25, 0.3))
    else:
        span, height, width = 1.0, 0.25, 0.3

    # Pull explicit dimensions from the prompt.
    explicit = _extract_all_dimensions(prompt)
    size_dims = _parse_size_expression(prompt)

    has_span = explicit["span"] is not None or len(size_dims) >= 1
    has_width = explicit["width"] is not None or len(size_dims) >= 2
    has_height = explicit["height"] is not None or len(size_dims) >= 3

    span = explicit["span"] or (size_dims[0] if len(size_dims) >= 1 else None) or _parse_dimension(prompt, span)
    width = explicit["width"] or (size_dims[1] if len(size_dims) >= 2 else None) or _parse_dimension(prompt, width)
    height = explicit["height"] or (size_dims[2] if len(size_dims) >= 3 else None) or _parse_dimension(prompt, height)

    # If the prompt is vague or the LLM returns "custom", infer a kind from the
    # proportions so the user doesn't always get a Warren truss.
    if detected_kind is None or detected_kind == "custom":
        if has_span or has_width or has_height:
            kind = _infer_kind_from_dims(span, height, width)
        else:
            kind = "frame"
        dspan, dheight, dwidth = defaults.get(kind, (1.0, 0.25, 0.3))
        if not has_span:
            span = dspan
        if not has_width:
            width = dwidth
        if not has_height:
            height = dheight
    else:
        kind = detected_kind

    complexity = _detect_complexity(prompt)

    # Sensible counts based on kind and scale.
    if kind == "truss":
        bays = _parse_count(prompt, ["bay", "bays", "segment", "segments"], max(2, int(round(span / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "frame":
        bays = _parse_count(prompt, ["bay", "bays"], 2)
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "tower":
        levels = _parse_count(prompt, ["level", "levels", "story", "stories"], max(2, int(round(height / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=4, levels=levels, complexity=complexity)
    if kind == "bridge":
        bays = _parse_count(prompt, ["bay", "bays", "segment", "segments"], max(3, int(round(span / 0.6))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "space_frame":
        bays = _parse_count(prompt, ["bay", "bays"], max(2, int(round(span / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=2, complexity=complexity)
    if kind == "portal":
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=2, levels=1, complexity=complexity)
    if kind == "gantry":
        bays = _parse_count(prompt, ["bay", "bays"], max(2, int(round(span / 0.8))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "crane":
        bays = _parse_count(prompt, ["bay", "bays", "segment", "segments"], max(2, int(round(span / 0.6))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "scissor":
        bays = _parse_count(prompt, ["bay", "bays", "pair", "pairs", "segment", "segments"], max(2, int(round(span / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)
    if kind == "platform":
        levels = _parse_count(prompt, ["level", "levels", "shelf", "shelves"], max(1, int(round(height / 0.4))))
        bays = _parse_count(prompt, ["bay", "bays"], max(2, int(round(span / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=levels, complexity=complexity)
    if kind == "radial":
        segments = _parse_count(prompt, ["segment", "segments", "bay", "bays"], 8)
        rings = _parse_count(prompt, ["ring", "rings", "level", "levels"], 2)
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=segments, levels=rings, complexity=complexity)
    if kind == "arch":
        segments = _parse_count(prompt, ["segment", "segments", "bay", "bays"], 8)
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=segments, levels=1, complexity=complexity)
    if kind == "cantilever":
        bays = _parse_count(prompt, ["bay", "bays", "segment", "segments"], max(2, int(round(span / 0.5))))
        return AssemblySpec(kind=kind, span=span, height=height, width=width, bays=bays, levels=1, complexity=complexity)

    return AssemblySpec(kind=kind, span=span, height=height, width=width, complexity=complexity)


# ---------------------------------------------------------------------------
# Layout generators
# ---------------------------------------------------------------------------


def _node_id(prefix: str, *parts: int | str) -> str:
    return f"{prefix}-" + "-".join(str(p) for p in parts)


def _add_struts_between(layout: AssemblyLayout, ids: List[str], closed: bool = False) -> None:
    for i in range(len(ids) - 1):
        layout.struts.append(LayoutStrut(f"s-{ids[i]}-{ids[i+1]}", ids[i], ids[i + 1]))
    if closed and len(ids) > 2:
        layout.struts.append(LayoutStrut(f"s-{ids[-1]}-{ids[0]}", ids[-1], ids[0]))


def _truss_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Planar Warren-style truss with configurable bays."""
    span = spec.span
    height = spec.height
    bays = max(2, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()
    bottom_ids = []
    for i in range(bays + 1):
        x = -span / 2 + i * dx
        nid = _node_id("b", i)
        bottom_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, 0.0, 0.0), radius=0.04 + spec.complexity * 0.01))

    top_ids = []
    for i in range(bays):
        x = -span / 2 + (i + 0.5) * dx
        nid = _node_id("t", i)
        top_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, height, 0.0), radius=0.04 + spec.complexity * 0.01))

    _add_struts_between(layout, bottom_ids)
    _add_struts_between(layout, top_ids)

    for i in range(bays):
        layout.struts.append(LayoutStrut(_node_id("post", i), bottom_ids[i], top_ids[i], diameter=0.04))
        layout.struts.append(LayoutStrut(_node_id("diag", i, "a"), bottom_ids[i], top_ids[i], diameter=0.04))
        layout.struts.append(LayoutStrut(_node_id("diag", i, "b"), bottom_ids[i + 1], top_ids[i], diameter=0.04))

    return layout


def _frame_layout(spec: AssemblySpec) -> AssemblyLayout:
    """3-D box frame with optional internal bracing."""
    sx, sy, sz = spec.span, spec.height, spec.width
    hx, hy, hz = sx / 2, sy / 2, sz / 2
    corners = [
        LayoutNode(_node_id("c", 0), (-hx, -hy, -hz)),
        LayoutNode(_node_id("c", 1), (hx, -hy, -hz)),
        LayoutNode(_node_id("c", 2), (hx, hy, -hz)),
        LayoutNode(_node_id("c", 3), (-hx, hy, -hz)),
        LayoutNode(_node_id("c", 4), (-hx, -hy, hz)),
        LayoutNode(_node_id("c", 5), (hx, -hy, hz)),
        LayoutNode(_node_id("c", 6), (hx, hy, hz)),
        LayoutNode(_node_id("c", 7), (-hx, hy, hz)),
    ]
    layout = AssemblyLayout(nodes=corners)
    edges = [
        (0, 1), (1, 2), (2, 3), (3, 0),
        (4, 5), (5, 6), (6, 7), (7, 4),
        (0, 4), (1, 5), (2, 6), (3, 7),
    ]
    for a, b in edges:
        layout.struts.append(LayoutStrut(_node_id("edge", a, b), corners[a].id, corners[b].id))

    if spec.complexity >= 2:
        # Face diagonals on front and back faces.
        for face in [(0, 1, 2, 3), (4, 5, 6, 7)]:
            layout.struts.append(LayoutStrut(_node_id("face-diag", face[0], face[2]), corners[face[0]].id, corners[face[2]].id))
            layout.struts.append(LayoutStrut(_node_id("face-diag", face[1], face[3]), corners[face[1]].id, corners[face[3]].id))
    if spec.complexity >= 3:
        # Space diagonals.
        for a, b in [(0, 6), (1, 7), (2, 4), (3, 5)]:
            layout.struts.append(LayoutStrut(_node_id("space-diag", a, b), corners[a].id, corners[b].id))

    return layout


def _tower_layout(spec: AssemblySpec) -> AssemblyLayout:
    """3-D tower with square levels and cross bracing."""
    size = spec.width
    height = spec.height
    levels = max(2, spec.levels)
    half = size / 2
    dz = height / levels

    layout = AssemblyLayout()
    prev_ids: List[str] = []

    for level in range(levels + 1):
        z = level * dz
        suffix = level
        corners = [
            LayoutNode(_node_id("n", suffix, 0), (-half, -half, z)),
            LayoutNode(_node_id("n", suffix, 1), (half, -half, z)),
            LayoutNode(_node_id("n", suffix, 2), (half, half, z)),
            LayoutNode(_node_id("n", suffix, 3), (-half, half, z)),
        ]
        layout.nodes.extend(corners)
        cids = [c.id for c in corners]
        _add_struts_between(layout, cids, closed=True)

        if prev_ids:
            for a, b in zip(prev_ids, cids):
                layout.struts.append(LayoutStrut(_node_id("col", level, a, b), a, b, diameter=0.05))
            if spec.complexity >= 2:
                # X-bracing on each face between this level and the previous.
                for i in range(4):
                    a = prev_ids[i]
                    b = cids[(i + 1) % 4]
                    c = prev_ids[(i + 1) % 4]
                    d = cids[i]
                    layout.struts.append(LayoutStrut(_node_id("brace", level, i, 0), a, b, diameter=0.035))
                    layout.struts.append(LayoutStrut(_node_id("brace", level, i, 1), c, d, diameter=0.035))

        prev_ids = cids

    return layout


def _bridge_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Through-truss bridge with deck, top chords, verticals and diagonals."""
    span = spec.span
    height = spec.height
    width = spec.width
    bays = max(3, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()

    def make_row(prefix: str, y: float, z: float) -> List[str]:
        ids = []
        for i in range(bays + 1):
            x = -span / 2 + i * dx
            nid = _node_id(prefix, i)
            ids.append(nid)
            layout.nodes.append(LayoutNode(nid, (x, y, z), radius=0.04 + spec.complexity * 0.01))
        return ids

    deck_left = make_row("dl", 0.0, -width / 2)
    deck_right = make_row("dr", 0.0, width / 2)
    top_left = make_row("tl", height, -width / 2)
    top_right = make_row("tr", height, width / 2)

    _add_struts_between(layout, deck_left)
    _add_struts_between(layout, deck_right)
    _add_struts_between(layout, top_left)
    _add_struts_between(layout, top_right)

    for i in range(bays + 1):
        layout.struts.append(LayoutStrut(_node_id("v-l", i), deck_left[i], top_left[i]))
        layout.struts.append(LayoutStrut(_node_id("v-r", i), deck_right[i], top_right[i]))
        layout.struts.append(LayoutStrut(_node_id("deck-x", i), deck_left[i], deck_right[i], diameter=0.05))
        if i < bays:
            layout.struts.append(LayoutStrut(_node_id("diag-l", i), deck_left[i], top_left[i + 1]))
            layout.struts.append(LayoutStrut(_node_id("diag-r", i), deck_right[i], top_right[i + 1]))
        if spec.complexity >= 2:
            layout.struts.append(LayoutStrut(_node_id("top-x", i), top_left[i], top_right[i]))

    return layout


def _space_frame_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Two-layer space frame grid."""
    span_x = spec.span
    span_y = spec.width
    height = spec.height
    bays_x = max(2, spec.bays)
    bays_y = max(2, int(round(span_y / (span_x / bays_x))))
    dx = span_x / bays_x
    dy = span_y / bays_y

    layout = AssemblyLayout()
    bottom: Dict[Tuple[int, int], str] = {}
    top: Dict[Tuple[int, int], str] = {}

    for i in range(bays_x + 1):
        for j in range(bays_y + 1):
            x = -span_x / 2 + i * dx
            y = -span_y / 2 + j * dy
            bid = _node_id("b", i, j)
            tid = _node_id("t", i, j)
            bottom[(i, j)] = bid
            top[(i, j)] = tid
            layout.nodes.append(LayoutNode(bid, (x, y, 0.0), radius=0.04))
            layout.nodes.append(LayoutNode(tid, (x, y, height), radius=0.04))

    for i in range(bays_x + 1):
        for j in range(bays_y):
            layout.struts.append(LayoutStrut(_node_id("by", i, j), bottom[(i, j)], bottom[(i, j + 1)]))
            layout.struts.append(LayoutStrut(_node_id("ty", i, j), top[(i, j)], top[(i, j + 1)]))
    for i in range(bays_x):
        for j in range(bays_y + 1):
            layout.struts.append(LayoutStrut(_node_id("bx", i, j), bottom[(i, j)], bottom[(i + 1, j)]))
            layout.struts.append(LayoutStrut(_node_id("tx", i, j), top[(i, j)], top[(i + 1, j)]))
    for i in range(bays_x + 1):
        for j in range(bays_y + 1):
            layout.struts.append(LayoutStrut(_node_id("post", i, j), bottom[(i, j)], top[(i, j)]))
            if spec.complexity >= 2 and i < bays_x and j < bays_y:
                layout.struts.append(LayoutStrut(_node_id("diag", i, j, 0), bottom[(i, j)], top[(i + 1, j + 1)]))
                layout.struts.append(LayoutStrut(_node_id("diag", i, j, 1), bottom[(i + 1, j)], top[(i, j + 1)]))

    return layout


def _portal_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Portal frame with two columns and a beam, plus optional knee braces."""
    span = spec.span
    height = spec.height
    layout = AssemblyLayout()
    nodes = [
        LayoutNode(_node_id("base", "l"), (-span / 2, 0.0, 0.0)),
        LayoutNode(_node_id("top", "l"), (-span / 2, height, 0.0)),
        LayoutNode(_node_id("top", "r"), (span / 2, height, 0.0)),
        LayoutNode(_node_id("base", "r"), (span / 2, 0.0, 0.0)),
    ]
    layout.nodes.extend(nodes)
    layout.struts.extend([
        LayoutStrut(_node_id("col", "l"), nodes[0].id, nodes[1].id, diameter=0.06),
        LayoutStrut(_node_id("col", "r"), nodes[3].id, nodes[2].id, diameter=0.06),
        LayoutStrut(_node_id("beam"), nodes[1].id, nodes[2].id, diameter=0.06),
    ])
    if spec.complexity >= 2:
        layout.struts.append(LayoutStrut(_node_id("knee", "l"), nodes[0].id, nodes[2].id, diameter=0.04))
        layout.struts.append(LayoutStrut(_node_id("knee", "r"), nodes[3].id, nodes[1].id, diameter=0.04))
    return layout


def _radial_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Circular radial grid / dome."""
    radius = spec.span / 2
    rings = max(1, spec.levels)
    segments = max(4, spec.bays)
    height = spec.height

    layout = AssemblyLayout()
    prev_ring: List[str] = []

    for ring in range(rings + 1):
        r = radius * (ring + 1) / (rings + 1)
        z = height * math.sin((ring / rings) * math.pi / 2) if rings > 0 else 0.0
        ring_ids = []
        for seg in range(segments):
            angle = 2 * math.pi * seg / segments
            x = r * math.cos(angle)
            y = r * math.sin(angle)
            nid = _node_id("ring", ring, seg)
            ring_ids.append(nid)
            layout.nodes.append(LayoutNode(nid, (x, y, z), radius=0.04 + spec.complexity * 0.01))
        _add_struts_between(layout, ring_ids, closed=True)
        if prev_ring:
            for a, b in zip(prev_ring, ring_ids):
                layout.struts.append(LayoutStrut(_node_id("rad", ring, a, b), a, b))
            for a, b in zip(prev_ring, ring_ids[1:] + ring_ids[:1]):
                if spec.complexity >= 2:
                    layout.struts.append(LayoutStrut(_node_id("diag", ring, a, b), a, b, diameter=0.035))
        prev_ring = ring_ids

    return layout


def _gantry_layout(spec: AssemblySpec) -> AssemblyLayout:
    """3-D gantry with four legs, an overhead beam and a trolley."""
    span = spec.span
    height = spec.height
    width = spec.width
    bays = max(2, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()
    leg_positions = [
        (-span / 2, -width / 2),
        (-span / 2, width / 2),
        (span / 2, -width / 2),
        (span / 2, width / 2),
    ]
    leg_nodes: List[Tuple[str, str]] = []
    for i, (x, z) in enumerate(leg_positions):
        base = _node_id("leg", i, "base")
        top = _node_id("leg", i, "top")
        leg_nodes.append((base, top))
        layout.nodes.append(LayoutNode(base, (x, 0.0, z)))
        layout.nodes.append(LayoutNode(top, (x, height, z)))
        layout.struts.append(LayoutStrut(_node_id("col", i), base, top, diameter=0.06))

    beam_ids: List[str] = []
    for i in range(bays + 1):
        x = -span / 2 + i * dx
        nid = _node_id("beam", i)
        beam_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, height, 0.0)))
    _add_struts_between(layout, beam_ids)

    for i, (_, top) in enumerate(leg_nodes):
        beam_nid = beam_ids[0] if i < 2 else beam_ids[-1]
        layout.struts.append(LayoutStrut(_node_id("leg-conn", i), top, beam_nid, diameter=0.05))

    trolley = _node_id("trolley")
    layout.nodes.append(LayoutNode(trolley, (0.0, height - min(height * 0.1, 0.1), 0.0), radius=0.05))
    layout.struts.append(LayoutStrut(_node_id("trolley-hanger"), beam_ids[bays // 2], trolley, diameter=0.04))

    if spec.complexity >= 2:
        for i, (base, _) in enumerate(leg_nodes):
            far_beam = beam_ids[-1] if i < 2 else beam_ids[0]
            layout.struts.append(LayoutStrut(_node_id("knee", i), base, far_beam, diameter=0.04))
    if spec.complexity >= 3:
        layout.struts.append(LayoutStrut(_node_id("base-x", 0), leg_nodes[0][0], leg_nodes[1][0]))
        layout.struts.append(LayoutStrut(_node_id("base-x", 1), leg_nodes[2][0], leg_nodes[3][0]))
        layout.struts.append(LayoutStrut(_node_id("top-x", 0), leg_nodes[0][1], leg_nodes[1][1]))
        layout.struts.append(LayoutStrut(_node_id("top-x", 1), leg_nodes[2][1], leg_nodes[3][1]))

    return layout


def _crane_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Tower crane with vertical mast, jib, tieback and counter-jib."""
    span = spec.span
    height = spec.height
    bays = max(2, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()
    base = _node_id("tower", "base")
    tower_top = _node_id("tower", "top")
    layout.nodes.append(LayoutNode(base, (0.0, 0.0, 0.0)))
    layout.nodes.append(LayoutNode(tower_top, (0.0, height, 0.0)))
    layout.struts.append(LayoutStrut(_node_id("tower"), base, tower_top, diameter=0.07))

    jib_ids: List[str] = []
    for i in range(bays + 1):
        x = i * dx
        nid = _node_id("jib", i)
        jib_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, height, 0.0)))
    _add_struts_between(layout, jib_ids)
    layout.struts.append(LayoutStrut(_node_id("jib-root"), tower_top, jib_ids[0], diameter=0.06))

    tie_top = _node_id("tower", "tie")
    layout.nodes.append(LayoutNode(tie_top, (0.0, height * 1.15, 0.0), radius=0.05))
    layout.struts.append(LayoutStrut(_node_id("tieback"), tie_top, jib_ids[-1], diameter=0.04))
    layout.struts.append(LayoutStrut(_node_id("tie-mast"), tower_top, tie_top, diameter=0.04))

    if spec.complexity >= 2:
        counter_len = span * 0.4
        counter_end = _node_id("counter", "end")
        layout.nodes.append(LayoutNode(counter_end, (-counter_len, height, 0.0)))
        layout.struts.append(LayoutStrut(_node_id("counter-jib"), tower_top, counter_end, diameter=0.05))
        layout.struts.append(LayoutStrut(_node_id("counter-tie"), tie_top, counter_end, diameter=0.04))

    return layout


def _scissor_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Scissor lift linkage made of repeated X braces."""
    span = spec.span
    height = spec.height
    bays = max(1, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()
    bottom_ids: List[str] = []
    top_ids: List[str] = []
    for i in range(bays + 1):
        x = -span / 2 + i * dx
        bid = _node_id("b", i)
        tid = _node_id("t", i)
        bottom_ids.append(bid)
        top_ids.append(tid)
        layout.nodes.append(LayoutNode(bid, (x, 0.0, 0.0)))
        layout.nodes.append(LayoutNode(tid, (x, height, 0.0)))

    _add_struts_between(layout, bottom_ids)
    _add_struts_between(layout, top_ids)

    for i in range(bays):
        layout.struts.append(LayoutStrut(_node_id("x", i, "a"), bottom_ids[i], top_ids[i + 1], diameter=0.04))
        layout.struts.append(LayoutStrut(_node_id("x", i, "b"), bottom_ids[i + 1], top_ids[i], diameter=0.04))

    layout.struts.append(LayoutStrut(_node_id("end-l"), bottom_ids[0], top_ids[0], diameter=0.05))
    layout.struts.append(LayoutStrut(_node_id("end-r"), bottom_ids[-1], top_ids[-1], diameter=0.05))
    return layout


def _platform_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Multi-shelf platform / rack with corner posts and grid shelves."""
    span_x = spec.span
    span_y = spec.width
    height = spec.height
    levels = max(1, spec.levels)
    bays_x = max(2, spec.bays)
    bays_y = max(2, int(round(span_y / (span_x / bays_x))))
    dx = span_x / bays_x
    dy = span_y / bays_y

    layout = AssemblyLayout()
    corner_indices = [(0, 0), (bays_x, 0), (bays_x, bays_y), (0, bays_y)]

    for level in range(levels + 1):
        y = level * (height / max(levels, 1))
        shelf_ids: List[str] = []
        for ix in range(bays_x + 1):
            for iy in range(bays_y + 1):
                x = -span_x / 2 + ix * dx
                z = -span_y / 2 + iy * dy
                nid = _node_id("shelf", level, ix, iy)
                shelf_ids.append(nid)
                layout.nodes.append(LayoutNode(nid, (x, y, z), radius=0.03))

        for ix in range(bays_x + 1):
            for iy in range(bays_y):
                a = shelf_ids[ix * (bays_y + 1) + iy]
                b = shelf_ids[ix * (bays_y + 1) + iy + 1]
                layout.struts.append(LayoutStrut(_node_id("sg", level, ix, iy, "y"), a, b, diameter=0.03))
        for ix in range(bays_x):
            for iy in range(bays_y + 1):
                a = shelf_ids[ix * (bays_y + 1) + iy]
                b = shelf_ids[(ix + 1) * (bays_y + 1) + iy]
                layout.struts.append(LayoutStrut(_node_id("sg", level, ix, iy, "x"), a, b, diameter=0.03))

        if level > 0:
            for (ix, iy) in corner_indices:
                a = _node_id("shelf", level - 1, ix, iy)
                b = _node_id("shelf", level, ix, iy)
                layout.struts.append(LayoutStrut(_node_id("post", level, ix, iy), a, b, diameter=0.05))

    return layout


def _arch_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Parabolic/arch structure with columns and arc nodes."""
    span = spec.span
    height = spec.height
    segments = max(4, spec.bays)

    layout = AssemblyLayout()
    base_l = _node_id("base", "l")
    base_r = _node_id("base", "r")
    layout.nodes.append(LayoutNode(base_l, (-span / 2, 0.0, 0.0)))
    layout.nodes.append(LayoutNode(base_r, (span / 2, 0.0, 0.0)))

    arc_ids: List[str] = []
    for i in range(segments + 1):
        t = i / segments
        angle = math.pi * t
        x = -span / 2 + span * (1 - math.cos(angle)) / 2
        y = height * math.sin(angle)
        nid = _node_id("arc", i)
        arc_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, y, 0.0), radius=0.04 + spec.complexity * 0.01))

    _add_struts_between(layout, arc_ids)
    layout.struts.append(LayoutStrut(_node_id("col", "l"), base_l, arc_ids[0], diameter=0.05))
    layout.struts.append(LayoutStrut(_node_id("col", "r"), base_r, arc_ids[-1], diameter=0.05))

    if spec.complexity >= 2:
        layout.struts.append(LayoutStrut(_node_id("tie", "l"), base_l, arc_ids[-2], diameter=0.04))
        layout.struts.append(LayoutStrut(_node_id("tie", "r"), base_r, arc_ids[1], diameter=0.04))

    return layout


def _cantilever_layout(spec: AssemblySpec) -> AssemblyLayout:
    """Cantilever beam fixed to a wall and supported by a diagonal brace."""
    span = spec.span
    height = spec.height
    bays = max(2, spec.bays)
    dx = span / bays

    layout = AssemblyLayout()
    base = _node_id("base")
    wall_top = _node_id("wall", "top")
    layout.nodes.append(LayoutNode(base, (0.0, 0.0, 0.0)))
    layout.nodes.append(LayoutNode(wall_top, (0.0, height, 0.0)))
    layout.struts.append(LayoutStrut(_node_id("wall"), base, wall_top, diameter=0.06))

    beam_ids: List[str] = []
    for i in range(bays + 1):
        x = i * dx
        nid = _node_id("beam", i)
        beam_ids.append(nid)
        layout.nodes.append(LayoutNode(nid, (x, height, 0.0)))
    _add_struts_between(layout, beam_ids)

    layout.struts.append(LayoutStrut(_node_id("brace"), base, beam_ids[-1], diameter=0.05))
    if spec.complexity >= 2:
        layout.struts.append(LayoutStrut(_node_id("backstay"), wall_top, beam_ids[-1], diameter=0.04))

    return layout


_LAYOUTS = {
    "truss": _truss_layout,
    "frame": _frame_layout,
    "tower": _tower_layout,
    "bridge": _bridge_layout,
    "space_frame": _space_frame_layout,
    "portal": _portal_layout,
    "radial": _radial_layout,
    "gantry": _gantry_layout,
    "crane": _crane_layout,
    "scissor": _scissor_layout,
    "platform": _platform_layout,
    "arch": _arch_layout,
    "cantilever": _cantilever_layout,
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def generate_assembly(prompt: str, resolution: int = 128) -> AssemblyResult:
    """Generate an AssemblyResult plus kernel solids for every node."""
    spec = parse_assembly_prompt(prompt)
    layout_fn = _LAYOUTS.get(spec.kind, _truss_layout)
    layout = layout_fn(spec)

    node_map: Dict[str, LayoutNode] = {n.id: n for n in layout.nodes}
    assembly_id = str(uuid.uuid4())

    # Scale node radius by connected strut count for a more realistic look.
    connection_counts: Dict[str, int] = {n.id: 0 for n in layout.nodes}
    for s in layout.struts:
        connection_counts[s.start_id] = connection_counts.get(s.start_id, 0) + 1
        connection_counts[s.end_id] = connection_counts.get(s.end_id, 0) + 1

    schema_nodes: List[NodePart] = []
    for node in layout.nodes:
        radius = node.radius * (1.0 + 0.15 * (connection_counts[node.id] - 2))
        tree = FeatureTree(
            SphereFeature(
                radius=radius,
                center=Point3(x=node.position[0], y=node.position[1], z=node.position[2]),
                name=node.id,
            )
        )
        candidate = candidate_from_feature_tree(tree, name=f"Node {node.id}", resolution=resolution)
        schema_nodes.append(
            NodePart(
                id=node.id,
                name=f"Node {node.id}",
                material="Aluminum 7075",
                quantity=1,
                print_time_minutes=30,
                position=Vector3(
                    x=node.position[0],
                    y=node.position[1],
                    z=node.position[2],
                ),
                candidate_id=candidate.id,
                mesh_url=candidate.mesh_url,
            )
        )

    schema_struts: List[Strut] = []
    for s in layout.struts:
        start = node_map[s.start_id].position
        end = node_map[s.end_id].position
        schema_struts.append(
            Strut(
                id=s.id,
                start=Vector3(x=start[0], y=start[1], z=start[2]),
                end=Vector3(x=end[0], y=end[1], z=end[2]),
                diameter=s.diameter,
            )
        )

    result = AssemblyResult(id=assembly_id, kind=spec.kind, nodes=schema_nodes, struts=schema_struts)
    ASSEMBLY_DATA[assembly_id] = result
    persistence.save_assembly(assembly_id, result.model_dump())
    return result
