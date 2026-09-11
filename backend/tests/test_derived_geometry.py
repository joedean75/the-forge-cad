"""In-context design: one document reading another's geometry, live.

Everything else here is bottom-up. A document is one tree with one body set, and
an assembly references *finished* documents — so an enclosure's outline gets
typed into the lid as well as the box, and the second copy is wrong from the
first edit onwards. A `DerivedFeature` names another document and is followed at
evaluation time, which makes the source's next edit the child's next body.

The three ways that can go wrong are what most of this file is about. A **cycle**
must be impossible rather than unlikely, so the check that counts is the one at
resolution and not the one at the call that writes the reference. **Staleness**
is the evaluation key, which hashes what is *in* a document and a derived
document's geometry is not. And **access** is a boundary a reference crosses: it
reads the source's geometry straight back out, exactly as a component in an
assembly does, so it is the same rule.
"""

import math

import pytest

from app.services import cad_operations as ops
from app.services import derived_service
from app.services import document_service as docs
from app.services import feature_ops

V = lambda x, y, z: {"x": x, "y": y, "z": z}  # noqa: E731

CIRCLE = {"points": [{"id": "c", "x": 0.0, "y": 0.0}],
          "circles": [{"id": "k", "center": "c", "radius": 0.030}],
          "constraints": []}


def _plate(size=(.100, .060, .008), name="parent", owner="dev"):
    doc = docs.create_document(name=name, owner_id=owner)
    result = ops.dispatch("add_primitive", doc.id,
                          {"shape": "box",
                           "params": {"size": V(*size), "center": V(0, 0, 0)}})
    return doc.id, result["feature_id"]


def _volume(doc_id):
    doc = docs.get_document(doc_id)
    docs.evaluate_document(doc)
    return doc.derived.volume


def _error(doc_id):
    doc = docs.get_document(doc_id)
    docs.evaluate_document(doc)
    return doc.derived.error or ""


def _derive(source_id, owner="dev", name="child", **args):
    doc = docs.create_document(name=name, owner_id=owner)
    result = ops.dispatch("add_derived", doc.id, {"document_id": source_id, **args})
    return doc.id, result


# --------------------------------------------------------------- the graft --

def test_a_derived_body_is_the_source_document_s_body():
    parent, _ = _plate()
    child, _ = _derive(parent)
    assert _volume(child) == pytest.approx(_volume(parent))


def test_nothing_of_the_source_is_stored_but_its_id():
    """A copy passes every assertion on the day it is made and drifts on the
    next edit. What is stored has to be the reference and only the reference."""
    parent, _ = _plate()
    child, result = _derive(parent)
    node = feature_ops.find_node(docs.get_document(child).feature_tree,
                                 result["feature_id"])
    assert node["type"] == "DerivedFeature"
    assert node["params"]["document_id"] == parent
    assert set(node["params"]) == {"document_id", "revision", "entities", "kind"}


def test_editing_the_source_moves_the_derived_body():
    """The exit criterion, in its simplest form."""
    parent, box = _plate()
    child, _ = _derive(parent)
    before = _volume(child)
    ops.dispatch("set_feature_params", parent,
                 {"feature_id": box, "params": {"size": V(.200, .060, .008)}})
    assert _volume(child) == pytest.approx(2 * before)


def test_an_enclosure_top_and_bottom_share_one_profile():
    """The sprint's exit criterion. Two parts, one outline, different
    thicknesses — and editing the outline in the parent moves both."""
    parent = docs.create_document(name="enclosure", owner_id="dev")
    sketch = ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})["feature_id"]

    lids = []
    for depth in (.003, .010):
        lid, result = _derive(parent.id, name=f"lid{depth}", kind="sketch",
                              entities=[sketch])
        ops.dispatch("add_extrude", lid,
                     {"sketch": result["feature_id"], "depth": depth})
        assert _volume(lid) == pytest.approx(math.pi * .030 ** 2 * depth, rel=1e-3)
        lids.append((lid, depth))

    widened = {**CIRCLE, "circles": [{"id": "k", "center": "c", "radius": 0.040}]}
    ops.dispatch("update_sketch", parent.id, {"feature_id": sketch, "sketch": widened})
    for lid, depth in lids:
        assert _volume(lid) == pytest.approx(math.pi * .040 ** 2 * depth, rel=1e-3)


def test_a_derived_body_can_be_cut_from_the_part_rather_than_joined():
    parent, _ = _plate(size=(.020, .020, .020))
    child, _ = _plate(size=(.040, .040, .020), name="block")
    ops.dispatch("add_derived", child, {"document_id": parent, "operation": "subtract"})
    assert _volume(child) == pytest.approx(.040 * .040 * .020 - .020 * .020 * .020)


def test_named_entities_select_part_of_the_source():
    parent, box = _plate(size=(.100, .060, .008))
    ops.dispatch("add_primitive", parent,
                 {"shape": "box",
                  "params": {"size": V(.010, .010, .010), "center": V(.2, 0, 0)}})
    child, _ = _derive(parent, entities=[box])
    assert _volume(child) == pytest.approx(.100 * .060 * .008)


def test_several_named_entities_come_across_as_one_body():
    parent, first = _plate(size=(.020, .020, .020))
    second = ops.dispatch("add_primitive", parent,
                          {"shape": "box",
                           "params": {"size": V(.020, .020, .020),
                                      "center": V(.5, 0, 0)}})["feature_id"]
    child, _ = _derive(parent, entities=[first, second])
    assert _volume(child) == pytest.approx(2 * .020 ** 3)


# ------------------------------------------------------------ grafted ids --

def test_grafted_ids_are_prefixed_and_deterministic():
    """Two documents mint ids from their own allocators, so `f3` means a
    different feature in each. Deterministic because replay and the
    decompiler's mesh comparison both rest on rebuilding byte for byte."""
    parent, box = _plate()
    child, result = _derive(parent)
    doc = docs.get_document(child)
    ids = {n.get("id") for n in feature_ops.flatten(docs.resolved_tree(doc))}
    grafted = f"{result['feature_id']}~{box}"
    assert grafted in ids
    assert ids == {n.get("id") for n in feature_ops.flatten(docs.resolved_tree(doc))}


def test_a_grafted_id_reads_back_to_the_reference_that_brought_it_in():
    """A triangle honestly owned by another document's feature names an id this
    tree does not contain, and a selection that finds nothing is worse than a
    coarser one that finds the feature a user can edit."""
    assert derived_service.source_feature("f5~f3") == "f5"
    assert derived_service.source_feature("f3") is None
    assert derived_service.source_feature(None) is None


def test_a_reference_inside_the_graft_is_renamed_with_it():
    """A reference left pointing at the source's id resolves against *this*
    tree, where it either means nothing or means somebody else's feature."""
    parent = docs.create_document(name="src", owner_id="dev")
    sketch = ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})["feature_id"]
    ops.dispatch("add_extrude", parent.id, {"sketch": sketch, "depth": .005})
    child, result = _derive(parent.id)

    tree = docs.resolved_tree(docs.get_document(child))
    prefix = result["feature_id"]
    extrudes = [n for n in feature_ops.flatten(tree) if n["type"] == "ExtrudeFeature"]
    assert extrudes, "the source's extrude should have come across"
    assert _volume(child) == pytest.approx(math.pi * .030 ** 2 * .005, rel=1e-3)
    for node in feature_ops.flatten(tree):
        assert node["id"].startswith(prefix + "~")


# ------------------------------------------------------------------ cycles --

def test_a_reference_to_itself_is_refused_and_names_the_loop():
    parent, _ = _plate()
    envelope = ops.execute("add_derived", parent, {"document_id": parent})
    assert not envelope["ok"]
    assert envelope["error"]["code"] == "PRECONDITION"
    assert "circular" in envelope["error"]["message"]


def test_a_loop_three_documents_long_is_refused():
    a, _ = _plate(name="a")
    b, _ = _plate(name="b")
    c, _ = _plate(name="c")
    ops.dispatch("add_derived", b, {"document_id": a})
    ops.dispatch("add_derived", c, {"document_id": b})
    envelope = ops.execute("add_derived", a, {"document_id": c})
    assert not envelope["ok"]
    message = envelope["error"]["message"]
    assert all(part in message for part in (a, b, c))


def test_a_cycle_made_by_editing_the_other_document_is_caught_at_resolution():
    """The up-front check is defeated by editing the *other* document
    afterwards, which is why it is not the guarantee. This is."""
    a, _ = _plate(name="a")
    b, _ = _plate(name="b")
    ops.dispatch("add_derived", b, {"document_id": a})
    # Written straight into the tree, bypassing the operation that would refuse.
    node = {"type": "DerivedFeature", "name": "loop",
            "params": {"document_id": b, "revision": None, "entities": [],
                       "kind": "body"}}
    doc = docs.get_document(a)
    docs.update_document(a, feature_tree=feature_ops.add_primitive(
        doc.feature_tree, node, "union"))

    assert "circular" in _error(a) or "already being read" in _error(a)
    assert "already being read" in _error(b)


def test_a_cycle_does_not_make_the_evaluation_key_recurse_forever():
    """The key follows the same references the resolver does, so it needs the
    same guard — a cache lookup that never returns is not better than a wrong
    body, it is worse."""
    a, _ = _plate(name="a")
    b, _ = _plate(name="b")
    ops.dispatch("add_derived", b, {"document_id": a})
    node = {"type": "DerivedFeature", "name": "loop",
            "params": {"document_id": b, "revision": None, "entities": [],
                       "kind": "body"}}
    doc = docs.get_document(a)
    docs.update_document(a, feature_tree=feature_ops.add_primitive(
        doc.feature_tree, node, "union"))
    assert docs._evaluation_key(docs.get_document(a), 128)


# ------------------------------------------------------------------ access --

def test_a_document_the_owner_cannot_open_is_refused():
    """The rule `assembly_service.add_component` applies to a part, for the
    identical reason: the reference reads the source's geometry."""
    theirs, _ = _plate(name="theirs", owner="someone-else")
    mine, _ = _plate(name="mine", owner="dev")
    envelope = ops.execute("add_derived", mine, {"document_id": theirs})
    assert not envelope["ok"]
    assert "can open" in envelope["error"]["message"]


def test_access_taken_away_afterwards_stops_the_reference_being_followed():
    source, box = _plate(name="shared")
    child, _ = _derive(source)
    assert _volume(child) > 0

    doc = docs.get_document(source)
    doc.owner_id = "someone-else"
    docs._persist(doc)
    docs.DOCUMENTS[source] = doc
    # Any rebuild re-resolves, and re-resolving is where the check lives.
    ops.dispatch("set_feature_params", source,
                 {"feature_id": box, "params": {"size": V(.2, .06, .008)}})
    assert "can no longer open" in _error(child)


def test_list_derived_reports_a_broken_reference_without_waiting_for_a_rebuild():
    """`_evaluation_key` is a geometry cache: it does not move when a share is
    revoked, so a document goes on serving the mesh it already had. The
    question is asked directly here rather than read off `derived.error`."""
    source, _ = _plate(name="shared")
    child, _ = _derive(source)
    _volume(child)
    doc = docs.get_document(source)
    doc.owner_id = "someone-else"
    docs._persist(doc)
    docs.DOCUMENTS[source] = doc

    row = ops.dispatch("list_derived", child, {})["derives_from"][0]
    assert row["resolves"] is False
    assert "can no longer open" in row["reason"]


# ---------------------------------------------------------------- refusals --

def test_deriving_a_body_from_a_document_with_no_body_is_refused():
    """The evaluator's 1 mm stand-in would otherwise appear in the viewport as
    the part — the confident little cube three datum planes replaced."""
    blank = docs.create_document(name="blank", owner_id="dev").id
    mine, _ = _plate(name="mine")
    envelope = ops.execute("add_derived", mine, {"document_id": blank})
    assert not envelope["ok"]
    assert "no body yet" in envelope["error"]["message"]


def test_naming_a_feature_the_source_does_not_have_is_refused():
    parent, _ = _plate()
    mine, _ = _plate(name="mine")
    envelope = ops.execute("add_derived", mine,
                           {"document_id": parent, "entities": ["f99"]})
    assert not envelope["ok"]
    assert "f99" in envelope["error"]["message"]


def test_a_document_that_does_not_exist_is_refused():
    mine, _ = _plate(name="mine")
    envelope = ops.execute("add_derived", mine, {"document_id": "nope"})
    assert not envelope["ok"]
    assert envelope["error"]["code"] == "NO_SUCH_DOCUMENT"


def test_deriving_a_body_from_only_bodyless_features_is_refused():
    """It would be spliced in, pruned before evaluation, and the boolean above
    it would collapse — a reference that reports success and contributes
    nothing."""
    parent = docs.create_document(name="src", owner_id="dev")
    sketch = ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})["feature_id"]
    ops.dispatch("add_extrude", parent.id, {"sketch": sketch, "depth": .005})
    mine, _ = _plate(name="mine")
    envelope = ops.execute("add_derived", mine,
                           {"document_id": parent.id, "entities": [sketch]})
    assert not envelope["ok"]
    assert "kind='sketch'" in envelope["error"]["message"]


def test_a_sketch_reference_must_say_which_sketch_when_there_are_several():
    """Guessing would be right most of the time, which is the worst possible
    accuracy for a reference that decides what the part is."""
    parent = docs.create_document(name="src", owner_id="dev")
    ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})
    ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})
    mine = docs.create_document(name="mine", owner_id="dev").id
    envelope = ops.execute("add_derived", mine,
                           {"document_id": parent.id, "kind": "sketch"})
    assert not envelope["ok"]
    assert "which sketch" in envelope["error"]["message"]


def test_a_revision_the_source_does_not_have_is_refused():
    parent, _ = _plate()
    mine, _ = _plate(name="mine")
    envelope = ops.execute("add_derived", mine,
                           {"document_id": parent, "revision": "Z"})
    assert not envelope["ok"]
    assert envelope["error"]["arg"] == "revision"


# ------------------------------------------------------------- the pinning --

def test_a_pinned_revision_does_not_follow_the_source():
    """Pinning is the opt-out from the whole point of the feature, and a child
    that must not move when its parent does is a real requirement."""
    parent, box = _plate()
    docs.release_revision(parent, author_id="dev")
    child, _ = _derive(parent, revision="A")
    before = _volume(child)
    ops.dispatch("set_feature_params", parent,
                 {"feature_id": box, "params": {"size": V(.200, .060, .008)}})
    assert _volume(child) == pytest.approx(before)


def test_a_pinned_reference_is_not_invalidated_by_an_unrelated_parent_edit():
    """A released revision is immutable, so reading the live source's key there
    would rebuild the child on every edit that cannot reach it."""
    parent, box = _plate()
    docs.release_revision(parent, author_id="dev")
    child, _ = _derive(parent, revision="A")
    doc = docs.get_document(child)
    docs.evaluate_document(doc)
    key = doc.derived.eval_key
    ops.dispatch("set_feature_params", parent,
                 {"feature_id": box, "params": {"size": V(.200, .060, .008)}})
    assert docs._evaluation_key(docs.get_document(child), doc.derived.resolution) == key


# ------------------------------------------------------------- the reports --

def test_list_derived_reports_both_directions():
    parent, _ = _plate()
    first, _ = _derive(parent, name="a")
    second, _ = _derive(parent, name="b")

    downstream = ops.dispatch("list_derived", parent, {})
    assert downstream["derives_from"] == []
    assert {row["document_id"] for row in downstream["used_by"]} == {first, second}

    upstream = ops.dispatch("list_derived", first, {})
    assert [row["document_id"] for row in upstream["derives_from"]] == [parent]
    assert upstream["derives_from"][0]["resolves"] is True


def test_list_derived_says_why_a_source_that_was_deleted_no_longer_resolves():
    parent, _ = _plate()
    child, _ = _derive(parent)
    docs.delete_document(parent)
    row = ops.dispatch("list_derived", child, {})["derives_from"][0]
    assert row["resolves"] is False
    assert "no longer exists" in row["reason"]


def test_the_dependents_scan_confirms_every_candidate_by_reading_its_tree():
    """The SQL pass is a substring filter and over-approximates by
    construction, so a row that merely mentions the id is not a dependent."""
    parent, _ = _plate()
    unrelated = docs.create_document(name=f"mentions {parent}", owner_id="dev").id
    child, _ = _derive(parent)
    found = {row["document_id"] for row in derived_service.dependents(parent)}
    assert found == {child}
    assert unrelated not in found


# ------------------------------------------------------- the rest of the API --

def test_a_document_holding_only_a_derived_sketch_reports_no_body():
    parent = docs.create_document(name="src", owner_id="dev")
    sketch = ops.dispatch("create_sketch", parent.id, {"sketch": CIRCLE})["feature_id"]
    child, _ = _derive(parent.id, kind="sketch", entities=[sketch])
    doc = docs.get_document(child)
    assert feature_ops.has_solid(doc.feature_tree) is False
    docs.evaluate_document(doc)
    assert doc.derived.mesh_url in (None, "")
    assert doc.derived.volume == 0.0


def test_the_evaluator_names_an_unresolved_reference_rather_than_building_one():
    """The `_SDF_BUILDERS` entry exists to say the resolution was skipped. A
    builder that invented a body would hide that behind a part which is quietly
    not the one that was asked for."""
    from app.cad_kernel.core.feature_tree import Feature
    from app.cad_kernel.ops.derived import DerivedNotResolved

    feature = Feature.from_dict({"type": "DerivedFeature", "id": "f1",
                                 "params": {"document_id": "x"}})
    with pytest.raises(DerivedNotResolved):
        feature.evaluate()


def test_the_decompiler_recovers_the_reference_rather_than_the_geometry():
    """Emitting the grafted features would turn a live reference into the copy
    the feature exists to avoid."""
    from app.services import decompiler

    parent, _ = _plate()
    child, _ = _derive(parent)
    report = decompiler.decompile_document(child)
    assert report["ok"], report.get("reason")
    steps = [step for step in report["steps"] if step["op"] == "add_derived"]
    assert len(steps) == 1
    assert steps[0]["args"]["document_id"] == parent
    assert not any(step["op"] == "add_primitive" for step in report["steps"])


def test_the_observation_shows_what_a_document_reads():
    from app.services import observation

    parent, _ = _plate()
    child, result = _derive(parent)
    state = observation.summarize(docs.get_document(child))
    row = next(f for f in state["features"] if f["id"] == result["feature_id"])
    assert row["params"]["document_id"] == parent
