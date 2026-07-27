"""Generative optimization must not silently delete features the part needs.

Two behaviours: a feature flagged must-keep survives the solve, and anything the
solve does eat through is reported rather than quietly missing.
"""

import pytest

from app.models.schemas import Constraint, LoadCase, OptimizationSetup, Vector3
from app.cad_kernel.core.feature_tree import FeatureTree
from app.services import cad_operations as ops
from app.services import document_service as ds
from app.services import feature_ops
from app.services import simp_optimizer as simp


def _plate_with_boss():
    """A plate carrying a tip load, plus an unloaded boss the solve wants to eat."""
    doc = ds.create_document(name="bracket", units="mm", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": {"x": 0.12, "y": 0.05, "z": 0.012}}})
    boss = ops.dispatch("add_primitive", doc.id, {
        "shape": "cylinder", "operation": "union",
        "params": {"radius": 0.006, "height": 0.014, "center": {"x": 0.045, "y": 0.0, "z": 0.012}},
    })
    return doc.id, boss["feature_id"]


def _setup():
    return OptimizationSetup(
        volume_fraction=0.3,
        load_cases=[LoadCase(id="l1", name="tip", point=Vector3(x=-0.055, y=0, z=0.006),
                             force=Vector3(x=0, y=0, z=-800), load_type="static")],
        constraints=[Constraint(id="c1", name="fix", type="fixed", point=Vector3(x=0.055, y=0, z=0.006))],
    )


def _solve(doc_id):
    # Coarse grid / few iterations: this asserts *which* features survive, not
    # the quality of the topology, and the full-resolution solve is far too slow
    # to sit in the unit suite.
    tree = ds.get_document(doc_id).feature_tree
    body = FeatureTree.from_dict(tree).root
    keep, audit = ds._optimize_feature_scope(tree)
    return simp.run_simp_from_solid(
        body, _setup(), keep_features=keep, audit_features=audit,
        max_iter=12, target_elements=900,
    )["info"]


# ------------------------------------------------------------- the keep flag --

def test_set_keep_marks_and_unmarks_a_feature():
    doc_id, boss = _plate_with_boss()
    ops.dispatch("set_feature_keep", doc_id, {"feature_id": boss, "keep": True})
    kept = ops.dispatch("list_kept_features", doc_id, {})["features"]
    assert [f["id"] for f in kept] == [boss]

    ops.dispatch("set_feature_keep", doc_id, {"feature_id": boss, "keep": False})
    assert ops.dispatch("list_kept_features", doc_id, {})["features"] == []


def test_keeping_a_feature_does_not_change_the_geometry():
    doc_id, boss = _plate_with_boss()
    before = ops.dispatch("bounding_box", doc_id, {})
    ops.dispatch("set_feature_keep", doc_id, {"feature_id": boss, "keep": True})
    assert ops.dispatch("bounding_box", doc_id, {}) == before


def test_keep_survives_serialization():
    doc_id, boss = _plate_with_boss()
    ops.dispatch("set_feature_keep", doc_id, {"feature_id": boss, "keep": True})
    reloaded = ds.get_document(doc_id).feature_tree
    assert [n["id"] for n in feature_ops.kept_features(reloaded)] == [boss]


def test_set_keep_on_a_missing_feature_is_an_error():
    doc_id, _ = _plate_with_boss()
    with pytest.raises(KeyError):
        ops.dispatch("set_feature_keep", doc_id, {"feature_id": "nope", "keep": True})


def test_operations_are_published():
    names = {o["name"] for o in ops.catalog()}
    assert {"set_feature_keep", "list_kept_features"} <= names


# ------------------------------------------------------------------ the solve --

@pytest.mark.slow
def test_an_unloaded_feature_is_removed_and_reported():
    doc_id, _ = _plate_with_boss()
    info = _solve(doc_id)
    removed = {r["name"] for r in info["removed_features"]}
    assert "Cylinder" in removed, "the unloaded boss should be eaten and called out"
    entry = next(r for r in info["removed_features"] if r["name"] == "Cylinder")
    assert 0.0 <= entry["remaining_fraction"] < 0.5
    assert entry["kept"] is False


@pytest.mark.slow
def test_a_kept_feature_survives_the_same_solve():
    doc_id, boss = _plate_with_boss()
    assert any(r["name"] == "Cylinder" for r in _solve(doc_id)["removed_features"])

    ops.dispatch("set_feature_keep", doc_id, {"feature_id": boss, "keep": True})
    info = _solve(doc_id)
    assert info["kept_feature_count"] == 1
    assert not any(r["name"] == "Cylinder" for r in info["removed_features"]), \
        "a must-keep feature must not be removed"


@pytest.mark.slow
def test_the_job_reports_removals_through_the_document():
    doc_id, _ = _plate_with_boss()
    doc = ds.run_document_optimize(doc_id, _setup(), resolution=48)
    setup = doc.feature_tree["root"]["params"]["setup"]
    assert "removed_features" in setup
    assert any(r["name"] == "Cylinder" for r in setup["removed_features"])


# ------------------------------------------------------------ the mask itself --

def test_preserve_mask_rejects_a_wrong_sized_grid():
    import numpy as np
    from app.models.schemas import DesignSpec

    spec = DesignSpec(
        name="x",
        design_space={"size": {"x": 0.1, "y": 0.1, "z": 0.1}, "center": {"x": 0, "y": 0, "z": 0}},
        preserve_regions=[], obstacle_regions=[], load_cases=[], constraints=[],
        material_id="al-6061", objective={"target": "minimize_mass", "weight": 1.0},
    )
    with pytest.raises(ValueError, match="preserve_mask"):
        simp.run_simp(spec, max_iter=1, preserve_mask=np.ones(3, dtype=bool))


def test_audit_flags_only_features_below_the_intact_threshold():
    import numpy as np

    rho = np.array([1.0, 1.0, 0.0, 0.0])
    masks = {
        "intact": np.array([True, True, False, False]),
        "gone": np.array([False, False, True, True]),
    }
    entries = [
        {"feature": "intact", "id": "a", "name": "intact", "type": "Box", "kept": False},
        {"feature": "gone", "id": "b", "name": "gone", "type": "Box", "kept": False},
    ]
    report = simp._audit_removed_features(entries, lambda f: masks[f], rho)
    assert [r["name"] for r in report] == ["gone"]
    assert report[0]["remaining_fraction"] == 0.0


def test_audit_skips_features_outside_the_grid():
    import numpy as np

    entries = [{"feature": "x", "id": "a", "name": "x", "type": "Box", "kept": False}]
    report = simp._audit_removed_features(
        entries, lambda f: np.zeros(4, dtype=bool), np.ones(4)
    )
    assert report == [], "a feature with no elements has nothing to report"
