"""Geometry caches, and the switches a training rollout needs.

Six sprints of capability each added an expensive path and none of them cached.
`_exact_shape` rebuilt the whole OpenCascade tree on every call, so an agent
that lists edges, picks one and fillets it built the same body three times.

The tests that matter here are not the speed ones — a benchmark belongs in a
commit message, not an assertion that will flake on a loaded machine. They are
the *correctness* ones: a stale exact body is far nastier than a slow one,
because the geometry silently stops agreeing with the document.
"""

import pytest

from app.cad_kernel.evaluator import brep
from conftest import body_id
from app.services import derived_cache, cad_operations as ops
from app.services import document_service as docs
from app.services import rollout


def _doc(size=0.05):
    doc = docs.create_document(name="Block", resolution=48, owner_id=None)
    ops.dispatch("add_primitive", doc.id,
                 {"shape": "box", "params": {"size": {"x": size, "y": size, "z": size}}})
    return doc.id


# ── the LRU ──
#
# It lives in `services/derived_cache` now, alongside the fingerprints that
# decide what may be reused — one file for "which edit invalidates which
# result" rather than five call sites. `document_service` still aliases the two
# geometry caches by their old names, so nothing that reaches for them broke.

def test_the_cache_forgets_its_oldest_entry_rather_than_growing():
    cache = derived_cache.LruCache(3)
    for key in "abcd":
        cache[key] = key

    assert set(cache) == {"b", "c", "d"}          # "a" evicted
    assert len(cache) == 3


def test_reading_an_entry_makes_it_recent():
    cache = derived_cache.LruCache(3)
    for key in "abc":
        cache[key] = key
    cache["a"]                                     # touch
    cache["d"] = "d"

    assert "a" in cache and "b" not in cache


def test_popping_an_entry_forgets_its_order_too():
    """Otherwise a popped key lingers in the order list and evicts a live one."""
    cache = derived_cache.LruCache(2)
    cache["a"] = "a"
    cache.pop("a", None)
    cache["b"] = "b"
    cache["c"] = "c"

    assert set(cache) == {"b", "c"}


# ── the B-rep cache ──

@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_the_same_document_is_built_once():
    doc_id = _doc()

    first = docs.get_exact_shape(doc_id)
    second = docs.get_exact_shape(doc_id)

    assert first is second          # the identical object, not an equal one


@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_editing_the_tree_invalidates_the_cached_body():
    """The bug this cache could most easily introduce. Keyed on `doc_id` alone
    it would hand back the pre-edit body forever."""
    doc_id = _doc(0.05)
    before = brep.volume(docs.get_exact_shape(doc_id))

    ops.dispatch("add_primitive", doc_id,
                 {"shape": "cylinder", "operation": "subtract",
                  "params": {"radius": 0.01, "height": 0.2}})

    assert brep.volume(docs.get_exact_shape(doc_id)) < before


@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_a_parameter_change_invalidates_it_too():
    """Parameters are not in `feature_tree`; they are resolved into it. Hashing
    the raw tree would miss this entirely."""
    doc_id = _doc()
    root = body_id(docs.get_document(doc_id).feature_tree)
    ops.dispatch("set_feature_params", doc_id,
                 {"feature_id": root, "params": {"size": {"x": "=w", "y": 0.05, "z": 0.05}}})
    ops.dispatch("set_parameters", doc_id,
                 {"parameters": [{"name": "w", "expr": "50", "unit": "mm"}]})
    before = brep.volume(docs.get_exact_shape(doc_id))

    ops.dispatch("set_parameters", doc_id,
                 {"parameters": [{"name": "w", "expr": "90", "unit": "mm"}]})

    assert brep.volume(docs.get_exact_shape(doc_id)) > before


@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_two_documents_with_the_same_geometry_share_one_build():
    """Keying on the resolved tree rather than the document id means an agent
    exploring variants of one part does not rebuild each identical branch."""
    first, second = _doc(0.04), _doc(0.04)

    assert docs.get_exact_shape(first) is docs.get_exact_shape(second)


@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_a_body_with_no_exact_form_is_not_cached_as_a_failure():
    doc_id = _doc()
    ops.dispatch("add_modifier", doc_id, {"modifier": "shell", "params": {"thickness": .004}})

    assert docs.get_exact_shape(doc_id) is None
    assert docs.get_exact_shape(doc_id) is None      # still None, still no crash


@pytest.mark.skipif(not brep.AVAILABLE, reason="needs cadquery-ocp")
def test_the_operations_that_share_the_cache_still_agree():
    """list_faces, mass_properties and the STEP export all read the same body
    now; if the cache were wrong they would disagree with each other."""
    from app.services import document_analysis

    doc_id = _doc(0.04)

    faces = ops.dispatch("list_faces", doc_id, {})["count"]
    volume = ops.dispatch("mass_properties", doc_id, {})["volume_cm3"]
    step = document_analysis.exact_step_document(doc_id)

    assert faces == 6
    assert volume == pytest.approx(64.0, rel=1e-6)   # 40 mm cube
    assert step is not None and "ADVANCED_FACE" in step


# ── rollout mode ──

def test_rollout_is_off_by_default():
    assert rollout.active() is False
    assert rollout.pinned_resolution() is None


def test_rollout_is_scoped_to_the_block():
    with rollout.rollout():
        assert rollout.active() is True
    assert rollout.active() is False


def test_a_pinned_resolution_overrides_the_plan_cap():
    """The plan deciding geometry fidelity means the same document evaluates
    differently for different accounts — fine for a product, fatal for a
    dataset whose observations have to be reproducible."""
    from app import security

    account = security._dev_account()
    capped = security.capped_resolution(account, 512)

    with rollout.rollout(resolution=256):
        pinned = security.capped_resolution(account, 512)

    assert capped <= account.plan.resolution_cap
    assert pinned == 256


def test_metering_is_bypassed_inside_a_rollout():
    from app import security

    account = security._dev_account()
    before = account.ops_used

    with rollout.rollout():
        for _ in range(5):
            security.meter_operation(account)

    assert account.ops_used == before

    security.meter_operation(account)
    assert account.ops_used == before + 1


def test_rollout_does_not_touch_access_control():
    """Metering is a billing counter. `can_access` is the security boundary and
    bypassing it would be a very different change."""
    other = docs.create_document(name="Theirs", resolution=48, owner_id="someone-else")

    with rollout.rollout():
        assert docs.can_access(other, "nobody") is False


def test_the_env_flag_enables_it_process_wide(monkeypatch):
    monkeypatch.setenv("FORGE_ROLLOUT", "1")
    monkeypatch.setenv("FORGE_ROLLOUT_RESOLUTION", "192")

    assert rollout.active() is True
    assert rollout.pinned_resolution() == 192
