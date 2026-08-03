"""Design-document orchestration.

A DesignDocument is the single source of truth for a part: its geometry is
always a serialized FeatureTree. This service owns the in-memory document
store, disk persistence, and cached evaluation (tessellation, mass, bbox).

Manual editing (Phase 1+) and generative optimization (Phase 3) both mutate a
document's feature tree and re-evaluate through here, so downstream consumers
(simulate / DFM / CAM / machine) only ever read `document.derived`.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Any, Dict, List, NamedTuple, Optional

from app.cad_kernel.core.feature_tree import FeatureTree, walk_nodes
from app.cad_kernel.core.topology import Solid
from app.cad_kernel.geom.meshing import mesh_hash, mesh_volume
from app.cad_kernel.kernel import Kernel
from app.models.schemas import (
    BBox,
    DerivedCache,
    DesignDocument,
    DocumentSummary,
    OptimizationSetup,
    Vector3,
)
from app.services import persistence
from app.cad_kernel.io.mesh_binary import write_mesh_binary

_LOGGER = logging.getLogger(__name__)

# Default density used for mass estimates when no material is bound to a doc.
_DEFAULT_DENSITY = 2700.0  # kg/m^3 (aluminium-like)

class _LruCache(dict):
    """A dict that forgets its least-recently-used entry past `capacity`.

    Both geometry caches were plain dicts with no eviction: fine for a UI
    session holding a handful of documents, a leak for a rollout loop that
    touches thousands. Subclassing dict keeps `in`, `[]` and `.pop(k, default)`
    working for the existing call sites (and the tests that reach into them)
    rather than forcing an interface change through the codebase.

    Bounded by *count*, not bytes: an evaluated solid's memory is a mesh whose
    size depends on resolution, and an OCC shape's is not introspectable from
    Python at all, so any byte figure would be invented.
    """

    def __init__(self, capacity: int):
        super().__init__()
        self._capacity = capacity
        self._order: "OrderedDict[str, None]" = OrderedDict()

    def __getitem__(self, key):
        value = super().__getitem__(key)
        self._order.move_to_end(key)
        return value

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self._order[key] = None
        self._order.move_to_end(key)
        while len(self._order) > self._capacity:
            oldest, _ = self._order.popitem(last=False)
            super().pop(oldest, None)

    def pop(self, key, *default):
        self._order.pop(key, None)
        return super().pop(key, *default)

    def clear(self) -> None:
        self._order.clear()
        super().clear()


# Process-local stores. Documents persist to disk; evaluated solids are cached
# in memory only and re-derived lazily on restart.
DOCUMENTS: Dict[str, DesignDocument] = {}

_SOLID_CACHE_SIZE = 64
_BREP_CACHE_SIZE = 64

_SOLID_CACHE: Dict[str, Solid] = _LruCache(_SOLID_CACHE_SIZE)

# Exact bodies (an `ExactBody`: shape, entity tags, reference verdicts), keyed by
# the hash of the *resolved* tree rather than the
# document id. Resolving suppression, datums and parameters is milliseconds;
# building the OpenCascade solid is not, and every topology-addressing
# operation — list_faces, list_edges, fillet_edges, mass_properties, dfm,
# verify_against_spec, the drawing path — used to rebuild it from the root. An
# agent that lists edges, picks one and fillets it built the same body three
# times.
#
# Keying on the resolved tree rather than on `doc_id` is the part that has to be
# right: a stale exact body is far nastier than a slow one, because the geometry
# silently stops agreeing with the document.
_BREP_CACHE: Dict[str, Any] = _LruCache(_BREP_CACHE_SIZE)

# Encoded meshes, keyed by `(doc_id, mesh_hash)`.
#
# Two reasons this exists rather than re-tessellating per request. It makes a
# mesh GET a dict lookup instead of an evaluation. And it keeps a *superseded*
# mesh available: the client is handed a URL with the current hash, and by the
# time it asks, another edit may have landed — without this the viewport goes
# blank on a 404 for geometry the server had a moment ago. The hash still names
# exactly those triangles, which is the whole contract; nothing here weakens it,
# it only extends how long the answer stays reachable.
_MESH_CACHE_SIZE = 48
_MESH_CACHE: Dict[str, bytes] = _LruCache(_MESH_CACHE_SIZE)


def cache_mesh(doc_id: str, digest: str, payload: bytes) -> None:
    _MESH_CACHE[f"{doc_id}:{digest}"] = payload


def cached_mesh(doc_id: str, digest: str) -> Optional[bytes]:
    return _MESH_CACHE.get(f"{doc_id}:{digest}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash_tree(tree: dict) -> str:
    return hashlib.sha256(json.dumps(tree, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _ensure_feature_ids(tree: Optional[dict]) -> None:
    """Give every feature node a stable id, in place.

    LLM-generated trees arrive with ``id: null``; if those are persisted as-is,
    the client invents display-only ids and every id-addressed edit
    (set_feature_params etc.) 404s against the server. Ids are assigned at the
    server so both sides always speak about the same features.

    Ids come from ``feature_ops.IdAllocator`` rather than uuid4 so that the same
    tree always yields the same ids — an LLM tree that regenerates identically
    must not produce a different document each time.
    """
    from app.services.feature_ops import IdAllocator

    ids = IdAllocator(tree or {})
    for node in walk_nodes(tree or {}):
        ids.ensure(node)


def _default_tree_dict() -> dict:
    """A blank document starts as a small valid box so it is immediately visible."""
    from app.cad_kernel.core.types import Point3, Vector3 as KVector3
    from app.cad_kernel.ops.primitives import BoxFeature

    root = BoxFeature(
        id="f1",  # deterministic: every blank document starts from the same tree
        name="Base Box",
        size=KVector3(0.1, 0.1, 0.1),
        center=Point3(),
    )
    tree = FeatureTree(root).to_dict()
    # Mark the base as a placeholder so the first real feature replaces it rather
    # than being swallowed by / unioned into a stray 100 mm block.
    tree["root"]["placeholder"] = True
    return tree


def _persist(doc: DesignDocument) -> None:
    try:
        data = doc.model_dump()
        # `mesh_url` is now a short hash-keyed URL rather than megabytes of
        # base64, so it is worth persisting: the mesh behind it is re-derived
        # lazily, and keeping the link means a reload does not have to
        # re-evaluate just to learn where the triangles are. G-code and STEP are
        # still stripped — those really are payloads.
        data["derived"]["step_url"] = None
        if data["derived"].get("cam"):
            data["derived"]["cam"]["gcode_url"] = None
        persistence.save_document(doc.id, data)
    except Exception as exc:  # persistence must never break the request
        _LOGGER.warning("Failed to persist document %s: %s", doc.id, exc)
    # Announce the change to live collaborators (no-op when nobody is connected).
    from app.services import realtime

    realtime.notify_document_updated(doc.id, doc.updated_at)


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------


def _evaluation_key(doc: DesignDocument, resolution: int) -> str:
    """Everything that changes what `evaluate_document` would produce.

    Not just the feature tree: a parameter value, the active configuration and
    the resolution all move the body without touching a single node. Anything
    missing here becomes a stale mesh served under a fresh hash, which is the
    one failure a cache must not have — so this is deliberately generous, and
    excludes only what provably cannot change geometry (the material, the name,
    the spec).
    """
    def dumped(models, drop=()):
        # The whole model, minus named outputs. Listing the fields to *include*
        # is how a later schema addition silently stops being part of the key,
        # so this inverts it: everything counts unless it is provably derived.
        return [{k: v for k, v in m.model_dump().items() if k not in drop}
                for m in (models or [])]

    parts = [
        _hash_tree(doc.feature_tree),
        str(resolution),
        doc.active_configuration or "",
        # `value` is the *resolved* number this function's caller is about to
        # write back, not an input — including it would invalidate the key on
        # the very evaluation that produced it.
        json.dumps(dumped(doc.parameters, drop=("value",)), sort_keys=True, default=str),
        json.dumps(dumped(doc.configurations), sort_keys=True, default=str),
    ]
    return hashlib.sha256("|".join(parts).encode()).hexdigest()


def _refresh_mass(doc: DesignDocument) -> None:
    """Recompute mass from the cached volume — the only thing a material moves."""
    from app.services import materials

    doc.derived.mass = round(doc.derived.volume * materials.density_of(doc.material_id), 12)


def evaluate_document(doc: DesignDocument, resolution: int = 128, persist: bool = True) -> DesignDocument:
    """Evaluate the document's feature tree into a solid and refresh DerivedCache.

    Evaluation failures are captured in `derived.error` rather than raised, so a
    malformed edit never wedges the document.

    **An edit that cannot move the body does not re-evaluate one.** Assigning a
    material, writing a spec, renaming — none of them touches geometry, and all
    of them used to pay a full evaluate-and-tessellate (measured at 85-90 ms)
    plus a re-send of the whole mesh. `_evaluation_key` decides; mass is the one
    derived number a material *does* move, so it is recomputed from the cached
    volume rather than from a new solid.
    """
    _ensure_feature_ids(doc.feature_tree)
    tree_hash = _hash_tree(doc.feature_tree)
    key = _evaluation_key(doc, resolution)
    if (doc.derived.eval_key == key and doc.derived.mesh_url and not doc.derived.error
            and doc.id in _SOLID_CACHE):
        _refresh_mass(doc)
        doc.updated_at = _now()
        if persist:
            _persist(doc)
        return doc
    try:
        if doc.parameters:
            # Evaluating is also when a driving parameter's computed value is
            # refreshed on the document. The *tree* resolution is `resolved_tree`
            # and only `resolved_tree` — five copies of that sequence had grown,
            # and the sixth is how a document evaluates to one body here and a
            # different one in the slicer.
            from app.services import parameters

            overrides = {}
            if doc.active_configuration:
                cfg = next((c for c in doc.configurations if c.name == doc.active_configuration), None)
                if cfg:
                    overrides = cfg.overrides
            _, resolved = parameters.resolve_values(doc.parameters, overrides)
            for p, rd in zip(doc.parameters, resolved):
                p.value = rd.get("value")
        active = resolved_tree(doc)
        tree = FeatureTree.from_dict(active)
        sampler = Kernel.sampler_for(tree.root)
        solid = Kernel.evaluate_tree(tree, resolution=resolution)
        mesh = Kernel.tessellate(solid)

        volume = abs(float(mesh_volume(mesh.vertices, mesh.faces))) if len(mesh.faces) else 0.0
        bbox = None
        if mesh.vertices is not None and len(mesh.vertices):
            mn = mesh.vertices.min(axis=0)
            mx = mesh.vertices.max(axis=0)
            bbox = BBox(
                min=Vector3(x=float(mn[0]), y=float(mn[1]), z=float(mn[2])),
                max=Vector3(x=float(mx[0]), y=float(mx[1]), z=float(mx[2])),
            )

        from app.services import materials

        density = materials.density_of(doc.material_id)
        from app.services import picking

        digest = mesh_hash(mesh)
        # Provenance rides with the triangles rather than being re-derived on
        # demand: on the exact path it is a by-product of the tessellation that
        # just ran, and re-deriving it would mean meshing the body twice.
        cache_mesh(doc.id, digest,
                   write_mesh_binary(mesh, picking.owners_for(solid, mesh, active,
                                                              resolution)))
        doc.derived = DerivedCache(
            solid_hash=tree_hash,
            eval_key=key,
            # A URL, not a payload. See `routers/documents.get_document_mesh`.
            mesh_url=f"/api/documents/{doc.id}/mesh/{digest}",
            bbox=bbox,
            # Rounded well below anything meaningful, not to a "tidy" number of
            # decimals: these are SI (m^3 and kg), so round(v, 6) quantised
            # volume to 1 mm^3 and mass to 0.1 g. On an exact body a 75978.8 mm^3
            # plate reported as 76000.
            mass=round(volume * density, 12),
            volume=round(volume, 15),
            resolution=resolution,
            sampler=sampler,
            mesh_hash=digest,
        )
        _SOLID_CACHE[doc.id] = solid
    except Exception as exc:
        _LOGGER.warning("Document %s evaluation failed: %s", doc.id, exc)
        doc.derived = DerivedCache(solid_hash=tree_hash, error=str(exc))
        _SOLID_CACHE.pop(doc.id, None)

    doc.updated_at = _now()
    if persist:
        _persist(doc)
    return doc


def resolved_tree(doc: DesignDocument) -> dict:
    """The tree as it will actually be evaluated: suppression, datums, parameters.

    Extracted because three call sites had grown their own copy of this
    sequence, and a fourth would silently diverge from the body the rest of the
    system computes.
    """
    from app.services import feature_ops

    # Sketches are substituted before datums, so an extrude that adopted its
    # sketch's `on_face` is resolved by the one datum path rather than a second
    # copy of it.
    active = feature_ops.resolve_for_evaluation(doc.feature_tree)
    if doc.parameters:
        from app.services import parameters

        overrides = {}
        if doc.active_configuration:
            cfg = next((c for c in doc.configurations if c.name == doc.active_configuration), None)
            if cfg:
                overrides = cfg.overrides
        scope, _ = parameters.resolve_values(doc.parameters, overrides)
        active = parameters.resolve_tree(active, scope)
    return active


def get_exact_shape(doc_id: str):
    """The document's exact B-rep body, or None if it has no exact form.

    Cached on the resolved tree's hash, so the topology operations that all
    used to rebuild from the root now share one build. Returns ``None`` rather
    than raising: whether that is a precondition failure or a fallback to the
    faceted path is the caller's decision, not this function's.
    """
    body = get_exact_body(doc_id)
    return body.shape if body else None


class ExactBody(NamedTuple):
    """An exact build and everything about it that is only knowable while it runs.

    ``entities`` is the tag map from `core.history` — which face and edge of the
    finished body came from which feature — and ``references`` is what each
    topology-addressing feature's stored selections resolved to on the way up.
    Neither can be recovered from the finished shape, so they are cached with it
    rather than recomputed.
    """

    shape: Any
    entities: Any
    references: List[Dict[str, Any]]


def get_exact_body(doc_id: str) -> Optional[ExactBody]:
    """The exact build for a document — shape, entity tags and reference verdicts."""
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        return None
    doc = get_document(doc_id)
    if doc is None:
        return None

    active = resolved_tree(doc)
    key = _hash_tree(active)
    if key in _BREP_CACHE:
        return _BREP_CACHE[key]

    shape, history = brep.build_with_history(FeatureTree.from_dict(active).root)
    if shape is None:
        # Only successes are cached. A None means "no exact form", which is
        # cheap to re-derive and would otherwise pin a negative result across a
        # code change that makes the feature representable.
        return None
    body = ExactBody(shape, (history or {}).get("entities"),
                     (history or {}).get("references") or [])
    _BREP_CACHE[key] = body
    return body


def reference_verdicts(doc_id: str):
    """``(verdicts, error)`` — what each stored face/edge selection resolved to.

    Deliberately survives a failed build. A fillet whose edges an upstream edit
    destroyed leaves the document unevaluatable, and that is precisely when a
    caller most needs to be told *which* references were lost — so the cheap
    cached path is used when the body built, and a fresh build is run only when
    it did not, purely to collect the verdicts on the way to the same failure.
    """
    from app.cad_kernel.core.feature_tree import FeatureTree
    from app.cad_kernel.evaluator import brep

    if not brep.AVAILABLE:
        return [], None
    body = get_exact_body(doc_id)
    if body is not None:
        return body.references, None

    doc = get_document(doc_id)
    if doc is None:
        return [], None
    _, history = brep.build_with_history(FeatureTree.from_dict(resolved_tree(doc)).root)
    return (history or {}).get("references") or [], (history or {}).get("error")


def get_solid(doc_id: str, resolution: int = 128) -> Solid:
    """Return the evaluated Solid for a document, re-evaluating lazily if needed."""
    if doc_id in _SOLID_CACHE:
        return _SOLID_CACHE[doc_id]
    doc = get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    evaluate_document(doc, resolution=resolution)
    if doc_id not in _SOLID_CACHE:
        raise ValueError(doc.derived.error or "Document could not be evaluated")
    return _SOLID_CACHE[doc_id]


# ---------------------------------------------------------------------------
# CRUD
# ---------------------------------------------------------------------------


def access_role(doc: DesignDocument, account_id: Optional[str]) -> Optional[str]:
    """The caller's role on a document: "owner" | "editor" | "viewer" | None.

    Legacy documents (`owner_id is None`) predate ownership and grant full access
    to any authenticated account, preserving pre-multi-tenancy behavior.
    """
    if doc.owner_id is None or doc.owner_id == account_id:
        return "owner"
    for c in doc.collaborators:
        if c.account_id == account_id:
            return c.role
    return None


def can_access(doc: DesignDocument, account_id: Optional[str]) -> bool:
    """True if the caller may view the document (owner, editor, or viewer)."""
    return access_role(doc, account_id) is not None


def can_edit(doc: DesignDocument, account_id: Optional[str]) -> bool:
    """True if the caller may mutate geometry (owner or editor)."""
    return access_role(doc, account_id) in ("owner", "editor")


def can_manage(doc: DesignDocument, account_id: Optional[str]) -> bool:
    """True if the caller may delete or manage sharing (owner only)."""
    return access_role(doc, account_id) == "owner"


def create_document(
    name: Optional[str] = None,
    prompt: Optional[str] = None,
    units: str = "m",
    resolution: int = 128,
    owner_id: Optional[str] = None,
) -> DesignDocument:
    doc_id = str(uuid.uuid4())
    provenance = "manual"

    dimensions = []
    if prompt and prompt.strip():
        from app.services.text_to_cad_engine import generate_part_tree

        feature_tree, dimensions, source = generate_part_tree(prompt)
        tree = feature_tree.to_dict()
        provenance = "text_to_cad"
        _LOGGER.info("Generated part via %s engine (%d dims) for prompt: %s", source, len(dimensions), prompt[:60])
        if not name:
            stripped = prompt.strip()
            name = stripped[:57] + "..." if len(stripped) > 60 else stripped
    else:
        tree = _default_tree_dict()
        name = name or "Untitled Part"

    now = _now()
    doc = DesignDocument(
        id=doc_id,
        name=name,
        owner_id=owner_id,
        units=units,  # type: ignore[arg-type]
        feature_tree=tree,
        provenance=provenance,  # type: ignore[arg-type]
        created_at=now,
        updated_at=now,
        dimensions=dimensions,
        source_prompt=(prompt or "").strip() or None,
    )
    DOCUMENTS[doc_id] = doc
    evaluate_document(doc, resolution=resolution)
    return doc


def ai_edit_document(doc_id: str, instruction: str, resolution: int = 128) -> DesignDocument:
    """Apply a natural-language edit to a document's geometry via the LLM."""
    from app.services.text_to_cad_engine import edit_part_tree

    doc = get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)

    result = edit_part_tree(doc.feature_tree, instruction)
    if result is None:
        raise ValueError("AI edit did not produce valid geometry")

    tree, new_dims, source = result
    _LOGGER.info("AI-edited document %s via %s: %s", doc_id, source, instruction[:60])
    doc.feature_tree = tree.to_dict()
    if doc.provenance != "mixed":
        doc.provenance = "mixed"
    # Refresh model-generated dimensions; keep the user's own.
    doc.dimensions = [d for d in doc.dimensions if d.source == "user"] + new_dims
    evaluate_document(doc, resolution=resolution)
    return doc


def set_material(doc_id: str, material_id: str, resolution: int = 128) -> Optional[DesignDocument]:
    """Bind an engineering material to the document and re-evaluate (mass changes)."""
    from app.services import materials

    doc = get_document(doc_id)
    if doc is None:
        return None
    materials.get_material(material_id)  # validates / falls back
    doc.material_id = material_id
    evaluate_document(doc, resolution=resolution)
    return doc


def set_parameters(doc_id: str, params: list, resolution: int = 128) -> Optional[DesignDocument]:
    """Replace the document's driving parameters and re-evaluate."""
    from app.models.schemas import DocumentParameter
    from app.services import parameters as param_svc

    doc = get_document(doc_id)
    if doc is None:
        return None
    parsed = [p if isinstance(p, DocumentParameter) else DocumentParameter(**p) for p in params]
    # Validate the equation chain up front so a bad expr is a clean error.
    param_svc.resolve_values(parsed)
    doc.parameters = parsed
    evaluate_document(doc, resolution=resolution)
    return doc


def set_configurations(doc_id: str, configs: list, active: Optional[str] = None,
                       resolution: int = 128) -> Optional[DesignDocument]:
    """Replace the document's configurations and optionally activate one."""
    from app.models.schemas import DocumentConfiguration

    doc = get_document(doc_id)
    if doc is None:
        return None
    doc.configurations = [c if isinstance(c, DocumentConfiguration) else DocumentConfiguration(**c) for c in configs]
    names = {c.name for c in doc.configurations}
    if active is not None:
        if active and active not in names:
            raise ValueError(f"unknown configuration '{active}'")
        doc.active_configuration = active or None
    elif doc.active_configuration not in names:
        doc.active_configuration = None
    evaluate_document(doc, resolution=resolution)
    return doc


def activate_configuration(doc_id: str, name: Optional[str], resolution: int = 128) -> Optional[DesignDocument]:
    """Switch the live variant (None = base/default parameters)."""
    doc = get_document(doc_id)
    if doc is None:
        return None
    if name and name not in {c.name for c in doc.configurations}:
        raise ValueError(f"unknown configuration '{name}'")
    doc.active_configuration = name or None
    evaluate_document(doc, resolution=resolution)
    return doc


def set_tolerances(doc_id: str, general: Optional[str], tolerances: list, gdt: list) -> Optional[DesignDocument]:
    """Set the document's general-tolerance class, fit/± callouts and GD&T frames."""
    from app.models.schemas import FeatureControlFrame, ToleranceCallout

    doc = get_document(doc_id)
    if doc is None:
        return None
    doc.general_tolerance = general or None
    doc.tolerances = [t if isinstance(t, ToleranceCallout) else ToleranceCallout(**t) for t in tolerances]
    doc.gdt = [g if isinstance(g, FeatureControlFrame) else FeatureControlFrame(**g) for g in gdt]
    doc.updated_at = _now()
    _persist(doc)
    return doc


def set_spec(doc_id: str, spec: Optional[dict]) -> Optional[DesignDocument]:
    """Record what the part is supposed to be (see services/design_spec)."""
    from app.services import design_spec

    doc = get_document(doc_id)
    if doc is None:
        return None
    validated = design_spec.validate_spec(spec)
    doc.spec = validated if validated.get("requirements") else None
    doc.updated_at = _now()
    _persist(doc)
    return doc


def add_dimension(doc_id: str, req) -> Optional["Dimension"]:
    """Append a point-to-point dimension to a document."""
    from app.models.schemas import Dimension

    doc = get_document(doc_id)
    if doc is None:
        return None
    dim = Dimension(id=str(uuid.uuid4()), a=req.a, b=req.b, label=req.label, source=req.source)
    doc.dimensions.append(dim)
    doc.updated_at = _now()
    _persist(doc)
    return dim


def remove_dimension(doc_id: str, dim_id: str) -> bool:
    doc = get_document(doc_id)
    if doc is None:
        return False
    before = len(doc.dimensions)
    doc.dimensions = [d for d in doc.dimensions if d.id != dim_id]
    if len(doc.dimensions) == before:
        return False
    doc.updated_at = _now()
    _persist(doc)
    return True


def get_document(doc_id: str) -> Optional[DesignDocument]:
    if doc_id in DOCUMENTS:
        return DOCUMENTS[doc_id]
    data = persistence.load_document(doc_id)
    if not data:
        return None
    try:
        doc = DesignDocument(**data)
        # Heal documents persisted before server-side id assignment: without
        # ids here, the first edit against this doc 404s (see _ensure_feature_ids).
        _ensure_feature_ids(doc.feature_tree)
        DOCUMENTS[doc_id] = doc
        return doc
    except Exception as exc:
        _LOGGER.warning("Could not load document %s: %s", doc_id, exc)
        return None


def ensure_derived(doc: DesignDocument, resolution: int = 128) -> DesignDocument:
    """Re-evaluate a document whose derived mesh is missing (meshes are not
    persisted). Called where a caller actually needs geometry — not on every
    load — so a restart stays cheap."""
    if doc.derived.mesh_url is None and doc.derived.error is None:
        evaluate_document(doc, resolution=resolution)
    return doc


def _summary_role(owner_id: Optional[str], collaborators: list, account_id: Optional[str]) -> Optional[str]:
    """access_role() over raw summary fields (no full document load)."""
    if owner_id is None or owner_id == account_id:
        return "owner"
    for c in collaborators or []:
        if c.get("account_id") == account_id:
            return c.get("role")
    return None


def list_documents(owner_id: Optional[str] = None) -> List[DocumentSummary]:
    # Listing fields come straight from SQL — full documents (feature trees,
    # meshes, version history) are never deserialized here. In-memory documents
    # overlay the stored rows: they are at least as fresh.
    rows = {r["id"]: r for r in persistence.list_document_summaries()}
    for doc_id, doc in DOCUMENTS.items():
        rows[doc_id] = {
            "id": doc.id,
            "owner_id": doc.owner_id,
            "name": doc.name,
            "provenance": doc.provenance,
            "updated_at": doc.updated_at,
            "collaborators": [c.model_dump() for c in doc.collaborators],
        }
    summaries: List[DocumentSummary] = []
    for r in rows.values():
        role = _summary_role(r["owner_id"], r["collaborators"], owner_id) if owner_id is not None else "owner"
        if owner_id is not None and role is None:
            continue
        summaries.append(
            DocumentSummary(
                id=r["id"],
                name=r["name"],
                provenance=r["provenance"],
                updated_at=r["updated_at"],
                role=role or "owner",
                shared=bool(owner_id is not None and r["owner_id"] not in (None, owner_id)),
            )
        )
    summaries.sort(key=lambda s: s.updated_at, reverse=True)
    return summaries


def _tree_needs_rebuild(doc: DesignDocument, feature_tree: dict) -> bool:
    """Whether `feature_tree` differs from what the derived cache was built from.

    Comparing the incoming tree against ``doc.feature_tree`` is not enough: a
    caller that edits the stored tree **in place** and hands the same object back
    compares equal to itself, so the edit silently never re-evaluates and the
    document keeps stale geometry. Hashing the tree's current contents against
    the hash recorded at the last evaluation catches that, because the hash moves
    with the content regardless of object identity.

    Ids are assigned server-side before hashing, so a tree that arrives without
    them (LLM output) is normalized the same way the stored one was.
    """
    _ensure_feature_ids(feature_tree)
    last_built = doc.derived.solid_hash if doc.derived else None
    if last_built is None:
        # Never successfully evaluated — fall back to a value comparison, and
        # rebuild whenever the object is the very same one we already hold.
        return feature_tree is doc.feature_tree or feature_tree != doc.feature_tree
    return _hash_tree(feature_tree) != last_built


def update_document(
    doc_id: str,
    name: Optional[str] = None,
    feature_tree: Optional[dict] = None,
    optimization: Optional[OptimizationSetup] = None,
    resolution: int = 128,
) -> Optional[DesignDocument]:
    doc = get_document(doc_id)
    if doc is None:
        return None

    tree_changed = False
    if name is not None:
        doc.name = name
    if optimization is not None:
        doc.optimization = optimization
    if feature_tree is not None and _tree_needs_rebuild(doc, feature_tree):
        doc.feature_tree = feature_tree
        tree_changed = True
        if doc.provenance == "text_to_cad":
            doc.provenance = "mixed"

    if tree_changed:
        evaluate_document(doc, resolution=resolution)
    else:
        doc.updated_at = _now()
        _persist(doc)
    return doc


def delete_document(doc_id: str) -> bool:
    DOCUMENTS.pop(doc_id, None)
    _SOLID_CACHE.pop(doc_id, None)
    return persistence.delete_document(doc_id)


# ---------------------------------------------------------------------------
# Version history (snapshots, restore, branch)
# ---------------------------------------------------------------------------

_MAX_VERSIONS = 100  # bound history growth per document


def snapshot_version(doc_id: str, label: str, author_id: Optional[str] = None) -> Optional["DocumentVersion"]:
    """Capture the document's current feature tree as a labelled version."""
    from app.models.schemas import DocumentVersion

    doc = get_document(doc_id)
    if doc is None:
        return None
    parent = doc.version_history[-1].id if doc.version_history else None
    version = DocumentVersion(
        id=str(uuid.uuid4()),
        label=label or f"Version {len(doc.version_history) + 1}",
        feature_tree=json.loads(json.dumps(doc.feature_tree)),  # deep copy
        parent_id=parent,
        author_id=author_id,
        created_at=_now(),
    )
    doc.version_history.append(version)
    # Bound growth: drop the oldest once past the cap.
    if len(doc.version_history) > _MAX_VERSIONS:
        doc.version_history = doc.version_history[-_MAX_VERSIONS:]
    doc.updated_at = _now()
    _persist(doc)
    return version


_LIFECYCLE = ("draft", "in_review", "released", "obsolete")


def _next_revision_letter(existing: list) -> str:
    """A, B, C … then AA, AB … The sequence omits I, O, Q per drawing convention."""
    seq = "ABCDEFGHJKLMNPRSTUVWXYZ"  # I, O, Q already excluded
    n = len(existing)
    if n < len(seq):
        return seq[n]
    return seq[(n // len(seq)) - 1] + seq[n % len(seq)]


def set_lifecycle_state(doc_id: str, state: str) -> Optional[DesignDocument]:
    """Move a document through draft → in_review → released → obsolete."""
    doc = get_document(doc_id)
    if doc is None:
        return None
    if state not in _LIFECYCLE:
        raise ValueError(f"state must be one of {_LIFECYCLE}")
    doc.lifecycle_state = state  # type: ignore[assignment]
    doc.updated_at = _now()
    _persist(doc)
    return doc


def release_revision(doc_id: str, notes: str = "", author_id: Optional[str] = None,
                     label: str = "") -> Optional["DocumentRevision"]:
    """Cut an immutable released revision (letter) from the current geometry."""
    from app.models.schemas import DocumentRevision

    doc = get_document(doc_id)
    if doc is None:
        return None
    letter = _next_revision_letter(doc.revisions)
    rev = DocumentRevision(
        id=str(uuid.uuid4()),
        letter=letter,
        label=label or f"Revision {letter}",
        feature_tree=json.loads(json.dumps(doc.feature_tree)),  # immutable snapshot
        released_by=author_id,
        released_at=_now(),
        notes=notes[:2000],
    )
    doc.revisions.append(rev)
    doc.lifecycle_state = "released"
    doc.updated_at = _now()
    _persist(doc)
    return rev


def restore_version(doc_id: str, version_id: str, resolution: int = 128) -> Optional[DesignDocument]:
    """Roll the document's geometry back to a stored version.

    The current state is auto-snapshotted first, so a restore is itself undoable.
    """
    doc = get_document(doc_id)
    if doc is None:
        return None
    target = next((v for v in doc.version_history if v.id == version_id), None)
    if target is None:
        raise ValueError("version not found")
    snapshot_version(doc_id, "Auto-save before restore", author_id=doc.owner_id)
    doc.feature_tree = json.loads(json.dumps(target.feature_tree))
    doc.provenance = "mixed"
    evaluate_document(doc, resolution=resolution)
    return doc


def branch_from_version(
    doc_id: str, version_id: str, owner_id: Optional[str], name: Optional[str] = None, resolution: int = 128
) -> Optional[DesignDocument]:
    """Fork a new, independently-owned document from a stored version."""
    doc = get_document(doc_id)
    if doc is None:
        return None
    target = next((v for v in doc.version_history if v.id == version_id), None)
    if target is None:
        raise ValueError("version not found")

    now = _now()
    new_id = str(uuid.uuid4())
    branch = DesignDocument(
        id=new_id,
        name=name or f"{doc.name} (branch)",
        owner_id=owner_id,
        units=doc.units,
        feature_tree=json.loads(json.dumps(target.feature_tree)),
        provenance="mixed",
        created_at=now,
        updated_at=now,
        dimensions=[d.model_copy() for d in doc.dimensions],
    )
    DOCUMENTS[new_id] = branch
    evaluate_document(branch, resolution=resolution)
    return branch


# ---------------------------------------------------------------------------
# Collaboration (sharing)
# ---------------------------------------------------------------------------


def add_collaborator(doc_id: str, email: str, role: str = "viewer") -> DesignDocument:
    """Share a document with another account by email (owner action)."""
    from app.models.schemas import Collaborator
    from app.services import accounts

    doc = get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    if role not in ("viewer", "editor"):
        raise ValueError("role must be 'viewer' or 'editor'")
    target = accounts.account_by_email(email)
    if target is None:
        raise ValueError(f"no account found for {email}")
    if target.id == doc.owner_id:
        raise ValueError("owner already has full access")

    existing = next((c for c in doc.collaborators if c.account_id == target.id), None)
    if existing is not None:
        existing.role = role  # type: ignore[assignment]
    else:
        doc.collaborators.append(
            Collaborator(account_id=target.id, email=target.email, role=role, added_at=_now())  # type: ignore[arg-type]
        )
    doc.updated_at = _now()
    _persist(doc)
    return doc


def remove_collaborator(doc_id: str, account_id: str) -> DesignDocument:
    doc = get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)
    doc.collaborators = [c for c in doc.collaborators if c.account_id != account_id]
    doc.updated_at = _now()
    _persist(doc)
    return doc


# ---------------------------------------------------------------------------
# Generative optimization (Phase 3)
# ---------------------------------------------------------------------------

from app.models.schemas import OptimizeJob  # noqa: E402

_OPTIMIZE_JOBS: Dict[str, OptimizeJob] = {}


def get_optimize_job(job_id: str) -> Optional[OptimizeJob]:
    return _OPTIMIZE_JOBS.get(job_id)


def _optimize_feature_scope(tree: dict):
    """Split a tree into ``(keep_features, audit_features)`` for an optimize run.

    Keeps are the nodes flagged must-keep — their material is pinned solid.
    Audited features are every *leaf* body in the tree: they are what a user
    would recognise as "the boss" or "that hole", and after the solve each is
    checked for how much of it survived. Interior boolean/modifier nodes are
    skipped because their extent is the whole body, which is never informative.
    """
    from app.cad_kernel.core.feature_tree import Feature
    from app.services import feature_ops

    keep_nodes = feature_ops.kept_features(tree)
    keep_ids = {n.get("id") for n in keep_nodes}

    keep_features = []
    for node in keep_nodes:
        try:
            keep_features.append(Feature.from_dict(node))
        except Exception:  # a keep we can't rebuild must not break the solve
            _LOGGER.warning("Ignoring un-evaluable keep feature %s", node.get("id"))

    audit: list = []

    def walk(node: dict) -> None:
        if not isinstance(node, dict):
            return
        kids = feature_ops.children_of(node)
        for c in kids:
            walk(c)
        if kids:
            return  # only leaves carry a meaningful extent
        try:
            feature = Feature.from_dict(node)
        except Exception:
            return
        audit.append({
            "feature": feature,
            "id": node.get("id"),
            "name": node.get("name") or node.get("type"),
            "type": node.get("type"),
            "kept": node.get("id") in keep_ids,
        })

    if tree.get("root"):
        walk(tree["root"])
    return keep_features, audit


def run_document_optimize(doc_id: str, setup, resolution: int = 128, progress=None) -> DesignDocument:
    """Run SIMP scoped to the document body and fold the result in as an OptimizeFeature."""
    import numpy as np

    from app.cad_kernel.ops.generative import OptimizeFeature
    from app.services.simp_optimizer import run_simp_from_solid

    doc = get_document(doc_id)
    if doc is None:
        raise KeyError(doc_id)

    tree = FeatureTree.from_dict(doc.feature_tree)
    root = tree.root
    # Re-optimize from the original body, not a prior generative result.
    body = root.children[0] if isinstance(root, OptimizeFeature) and root.children else root

    keep, audit = _optimize_feature_scope(doc.feature_tree)
    result = run_simp_from_solid(
        body, setup, progress=progress, keep_features=keep, audit_features=audit
    )
    density = np.asarray(result["density"], dtype=float).round(3).tolist()

    opt_feature = OptimizeFeature(
        name="Generative",
        children=[body],
        cache={
            "density": density,
            "origin": result["origin"],
            "size": result["size"],
            "threshold": result["threshold"],
        },
        setup={
            "volume_fraction": setup.volume_fraction,
            "objective": setup.objective.target,
            "material_id": setup.material_id,
            # Persisted with the tree so the warning survives a reload and is
            # visible to agents through get_feature, not just to the poller.
            "removed_features": result["info"].get("removed_features", []),
            "kept_feature_count": result["info"].get("kept_feature_count", 0),
        },
        inputs_hash=_hash_tree(body.to_dict()),
    )

    doc.feature_tree = FeatureTree(opt_feature).to_dict()
    doc.optimization = setup
    doc.provenance = "generative"
    evaluate_document(doc, resolution=resolution)
    return doc


