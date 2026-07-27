"""The registries that make features and operations one-place additions.

Feature types and CAD operations used to be registered in lists far from the
code they named — a new feature that reached only half of them failed at
runtime, far from the cause. These tests are the guard rail that replaced that
coupling: they fail loudly at the seam instead.
"""

import inspect

import pytest

from app.cad_kernel.core.feature_tree import (
    Feature,
    FeatureTree,
    feature_types,
    find_node_by_type,
    node_children,
    walk_nodes,
)
from app.cad_kernel.evaluator.sdf import _SDF_BUILDERS
from app.services import cad_operations as ops


# ------------------------------------------------------- feature registry --

def test_every_feature_type_self_registers():
    types = feature_types()
    assert len(types) >= 19
    for name, cls in types.items():
        assert name == cls.__name__
        assert issubclass(cls, Feature)


def test_every_registered_feature_can_be_turned_into_an_sdf():
    """A feature the evaluator can't build is a runtime failure at first use."""
    missing = set(feature_types()) - set(_SDF_BUILDERS)
    assert not missing, f"no SDF builder for: {sorted(missing)}"


def test_no_sdf_builder_points_at_a_feature_that_no_longer_exists():
    stale = set(_SDF_BUILDERS) - set(feature_types())
    assert not stale, f"SDF builders for unknown features: {sorted(stale)}"


def test_unknown_feature_type_names_what_it_knows():
    with pytest.raises(ValueError, match="known:"):
        Feature.from_dict({"type": "NoSuchFeature", "params": {}})


def test_every_feature_round_trips_through_its_registered_type():
    from app.cad_kernel.ops.primitives import BoxFeature

    tree = FeatureTree(BoxFeature(name="B")).to_dict()
    assert FeatureTree.from_dict(tree).root.name == "B"


# ---------------------------------------------------------- tree helpers --

def _nested_tree():
    return {
        "version": "brep-v1",
        "root": {
            "type": "BooleanFeature", "id": "r", "name": "Union",
            "params": {"operation": "union", "children": [
                {"type": "BoxFeature", "id": "a", "name": "A", "params": {}},
                {"type": "BooleanFeature", "id": "b", "name": "Inner", "params": {"children": [
                    {"type": "CylinderFeature", "id": "c", "name": "C", "params": {}},
                ]}},
            ]},
        },
    }


def test_children_come_from_params_not_a_top_level_key():
    """Reaching for node["children"] finds nothing below the root — it has bitten."""
    root = _nested_tree()["root"]
    assert [c["id"] for c in node_children(root)] == ["a", "b"]
    assert root.get("children") is None


def test_walk_visits_the_whole_tree_parents_first():
    assert [n["id"] for n in walk_nodes(_nested_tree())] == ["r", "a", "b", "c"]


def test_walk_accepts_a_bare_node_as_well_as_a_tree():
    root = _nested_tree()["root"]
    assert [n["id"] for n in walk_nodes(root)] == ["r", "a", "b", "c"]


def test_walk_of_something_empty_is_empty_not_an_error():
    assert list(walk_nodes({})) == []
    assert list(walk_nodes(None)) == []
    assert node_children("not a node") == []


def test_find_by_type_reaches_nested_nodes():
    assert find_node_by_type(_nested_tree(), "CylinderFeature")["id"] == "c"
    assert find_node_by_type(_nested_tree(), "CylinderFeature", "nope") is None
    assert find_node_by_type(_nested_tree(), "SphereFeature") is None


# -------------------------------------------------------- operation registry --

def test_operations_register_themselves_beside_their_handlers():
    assert len(ops.OPERATIONS) >= 41
    for name, op in ops.OPERATIONS.items():
        assert op.name == name
        assert op.kind in ("query", "mutate")
        assert op.description.strip(), f"{name} has no description"
        assert callable(op.handler)


def test_every_operation_is_documented_where_it_is_defined():
    """The decorator sits on the handler, so the two can't drift apart."""
    for name, op in ops.OPERATIONS.items():
        source = inspect.getsource(op.handler)
        assert f'"{name}"' in source, f"{name} is not declared on its own handler"


def test_catalog_matches_the_registry():
    catalog = ops.catalog()
    assert {c["name"] for c in catalog} == set(ops.OPERATIONS)
    for entry in catalog:
        assert set(entry) >= {"name", "kind", "description", "params"}


def test_registering_the_same_name_twice_is_refused():
    with pytest.raises(ValueError, match="duplicate operation"):
        ops.operation("list_features", "query", "clash", {})(lambda doc_id, args: {})


def test_dispatching_an_unknown_operation_is_a_clean_error():
    with pytest.raises(Exception) as exc:
        ops.dispatch("no_such_operation", "some-doc", {})
    assert "no_such_operation" in str(exc.value) or "unknown" in str(exc.value).lower()
