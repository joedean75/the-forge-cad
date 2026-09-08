"""Deterministic workloads to measure against.

A benchmark is only worth keeping if two runs of it are comparable, so every
fixture here is built the same way the product builds a part — through
`cad_operations.execute` — and pinned through `rollout.rollout`, which turns
quota metering off and fixes the resolution. Without the pin `capped_resolution`
clamps to the *account's* plan, so the same fixture evaluates differently for
different accounts and a regression is indistinguishable from a different
reviewer.

The five workloads are chosen to separate paths that behave nothing alike:

- **exact_plate** — a plate with a bore. With OCP installed this is the B-rep
  path start to finish, which is the common case and the cheap one.
- **feature_tree** — twenty-odd features over one body. This is where the node
  cache and the boolean fold prefix keys earn their keep, and where an edit to
  the *last* feature is supposed to cost one boolean rather than all of them.
- **sdf_shell** — a shelled body. `brep_from_feature` declines `ShellFeature`,
  so the whole tree falls to the SDF sampler: the one workload where the native
  Rust wheel is on the critical path.
- **optimize** — a small SIMP solve. Minutes, not milliseconds; it is here to be
  the thing that must not block a modelling request, not to be tuned.
- **library** — thirty documents, listed. `list_document_summaries` reads its
  columns with `json_extract` and must never deserialize a feature tree; the
  listing latency is what says whether that is still true.

Every fixture takes an `owner_id` and returns the ids it made, so a harness can
build once and measure many times against the same bytes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional


def _op(op: str, doc_id: str, args: dict) -> dict:
    """Run one catalog operation, failing loudly.

    A benchmark that silently measures a *refused* operation reports a very
    good latency for doing nothing, which is the single worst failure this
    harness can have.
    """
    from app.services import cad_operations

    env = cad_operations.execute(op, doc_id, args, include_state=False)
    if not env.get("ok"):
        raise RuntimeError(f"{op} failed while building a fixture: {env.get('error')}")
    return env.get("result") or {}


def _blank(owner_id: Optional[str], name: str, resolution: int) -> str:
    from app.services import document_service as docs

    return docs.create_document(name=name, resolution=resolution, owner_id=owner_id).id


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_exact_plate(owner_id: Optional[str] = None, resolution: int = 96) -> Dict[str, object]:
    """A 120x80x10 mm plate with a 12 mm through bore."""
    doc_id = _blank(owner_id, "bench exact plate", resolution)
    plate = _op("add_primitive", doc_id, {
        "shape": "box",
        "params": {"size": {"x": 0.12, "y": 0.08, "z": 0.01}},
    })["feature_id"]
    _op("add_hole", doc_id, {
        "radius": 0.006, "depth": 0.01, "through": True,
        "axis": {"x": 0.0, "y": 0.0, "z": 1.0},
    })
    return {"document_id": doc_id, "base_feature": plate, "features": 2}


def build_feature_tree(owner_id: Optional[str] = None, resolution: int = 96,
                       bosses: int = 8, holes: int = 8) -> Dict[str, object]:
    """A plate carrying eight bosses and eight bores — 17 features on one body.

    Deliberately built as a *fold* of booleans over one root rather than as a
    pattern feature: a pattern is one node, and the thing under measurement is
    what happens when an edit lands at the end of a long fold.
    """
    doc_id = _blank(owner_id, "bench feature tree", resolution)
    _op("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.2, "y": 0.12, "z": 0.012}},
    })
    last = None
    for i in range(bosses):
        x = -0.08 + 0.02 * i
        last = _op("add_primitive", doc_id, {
            "shape": "cylinder",
            "params": {"radius": 0.006, "height": 0.01,
                       "center": {"x": x, "y": 0.04, "z": 0.011}},
        })["feature_id"]
    for i in range(holes):
        x = -0.08 + 0.02 * i
        _op("add_hole", doc_id, {
            "radius": 0.0025, "depth": 0.012, "through": True,
            "center": {"x": x, "y": -0.04, "z": 0.006},
            "axis": {"x": 0.0, "y": 0.0, "z": 1.0},
        })
    return {"document_id": doc_id, "last_boss": last, "features": 1 + bosses + holes}


def build_sdf_shell(owner_id: Optional[str] = None, resolution: int = 64) -> Dict[str, object]:
    """A shelled box — the tree the exact kernel declines.

    `ShellFeature` is `abs(d) - t/2`: it hollows uniformly and cannot open a
    face, so `brep_from_feature` returns `None` for it, and one declined feature
    disqualifies the whole tree. That makes this the workload that measures the
    signed-distance sampler end to end even where OCP is installed — which is
    the only way to see the native Rust wheel on the critical path at all. (The
    other declined shape, a smooth `FilletFeature` blend between bodies, is no
    longer constructible from the catalog: `add_modifier("fillet")` builds an
    exact edge treatment now.)
    """
    doc_id = _blank(owner_id, "bench sdf shell", resolution)
    _op("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.08, "y": 0.05, "z": 0.02}},
    })
    _op("add_primitive", doc_id, {
        "shape": "cylinder",
        "params": {"radius": 0.015, "height": 0.05, "center": {"x": 0.0, "y": 0.0, "z": 0.03}},
    })
    _op("add_modifier", doc_id, {"modifier": "shell", "params": {"thickness": 0.003}})
    return {"document_id": doc_id, "features": 3}


def build_optimize(owner_id: Optional[str] = None, resolution: int = 48) -> Dict[str, object]:
    """A loaded bracket blank for a small SIMP solve.

    Low resolution on purpose: the point of this fixture is that the *solve*
    runs off the request path, not how good the topology is. A benchmark that
    takes four minutes gets run once and then skipped.
    """
    doc_id = _blank(owner_id, "bench optimize", resolution)
    _op("add_primitive", doc_id, {
        "shape": "box", "params": {"size": {"x": 0.1, "y": 0.04, "z": 0.04}},
    })
    return {"document_id": doc_id, "features": 1}


def build_library(owner_id: Optional[str] = None, count: int = 30,
                  resolution: int = 48) -> Dict[str, object]:
    """`count` small documents, so the listing has something to list."""
    ids: List[str] = []
    for i in range(count):
        doc_id = _blank(owner_id, f"bench library {i:02d}", resolution)
        _op("add_primitive", doc_id, {
            "shape": "box",
            "params": {"size": {"x": 0.02 + 0.001 * i, "y": 0.02, "z": 0.01}},
        })
        ids.append(doc_id)
    return {"document_ids": ids, "count": count}


@dataclass
class Fixture:
    """One named workload: how to build it, and at what resolution."""

    name: str
    build: Callable[..., Dict[str, object]]
    resolution: int
    description: str
    #: Excluded from the default run — minutes rather than milliseconds.
    slow: bool = False
    kwargs: dict = field(default_factory=dict)


FIXTURES: Dict[str, Fixture] = {
    f.name: f for f in [
        Fixture("exact_plate", build_exact_plate, 96,
                "a plate with a through bore — the exact-kernel common case"),
        Fixture("feature_tree", build_feature_tree, 96,
                "seventeen features folded onto one body"),
        Fixture("sdf_shell", build_sdf_shell, 64,
                "a shelled body — the SDF sampler end to end"),
        Fixture("library", build_library, 48,
                "thirty documents, listed from the datastore"),
        Fixture("optimize", build_optimize, 48,
                "a small SIMP solve", slow=True),
    ]
}
