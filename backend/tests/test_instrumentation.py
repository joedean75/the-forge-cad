"""The measurement layer, and the fixtures it measures (PERF-01/02).

Two things are worth a test here and the rest is arithmetic. **Off must cost
nothing** — a recorder that stayed on after its block would tax every served
request and quietly accumulate a process-lifetime profile — and **the spans have
to name the stages that actually ran**, because the whole reason for the sprint
is that a request latency alone cannot say whether the kernel, the tessellation
or the encode owns it.

The fixtures are covered here rather than in a benchmark script nobody runs in
CI: a fixture builder that stops matching the operation catalog is exactly the
kind of rot that is only discovered when someone needs a baseline.
"""

from __future__ import annotations

import pytest

from app.services import document_service as docs, instrumentation
from benchmarks import fixtures


def test_nothing_is_recorded_outside_a_recording_block():
    instrumentation.count("test.counter")
    with instrumentation.span("test.span"):
        pass
    assert instrumentation.active() is None


def test_a_recorder_collects_spans_counters_and_gauges():
    with instrumentation.recording("unit") as rec:
        with instrumentation.span("stage"):
            pass
        instrumentation.count("thing", 2)
        instrumentation.count("thing")
        instrumentation.gauge("size", 10)
        instrumentation.gauge("size", 4)
    report = rec.report()
    assert report["spans"]["stage"]["count"] == 1
    assert report["counters"]["thing"] == 3
    # Last value wins; the peak is remembered separately, because a budget is
    # written against the worst case and a single run against the latest.
    assert report["gauges"]["size"] == 4
    assert report["gauges"]["size.max"] == 10
    assert instrumentation.active() is None


def test_the_recorder_is_restored_even_when_the_block_raises():
    with pytest.raises(RuntimeError):
        with instrumentation.recording("boom"):
            raise RuntimeError("boom")
    assert instrumentation.active() is None


def test_a_nested_region_reuses_the_outer_recorder():
    """Otherwise a service opening its own hides every span from the run."""
    with instrumentation.recording("outer") as outer:
        with instrumentation.recording("inner") as inner:
            instrumentation.count("seen")
        assert inner is outer
    assert outer.report()["counters"]["seen"] == 1


def test_an_evaluation_reports_its_stages_and_which_sampler_ran():
    doc = docs.create_document(name="instrumented", resolution=32)
    docs.get_document(doc.id)
    fixtures._op("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.04, "z": 0.01}}})

    with instrumentation.recording("evaluate") as rec:
        fixtures._op("set_feature_params", doc.id, {
            "feature_id": _box_id(doc.id),
            "params": {"size": {"x": 0.06, "y": 0.04, "z": 0.01}}})
    report = rec.report()

    assert report["spans"]["eval.kernel"]["count"] == 1
    assert report["spans"]["eval.tessellate"]["count"] == 1
    assert report["spans"]["eval.mesh_encode"]["count"] == 1
    assert report["counters"]["eval.cache_miss"] == 1
    # Exactly one sampler ran, and it said which. Without this a latency number
    # describes an arbitrary branch: the same tree is a B-rep build or a voxel
    # sample depending on whether OCP is installed.
    samplers = [k for k in report["counters"] if k.startswith("sampler.")]
    assert len(samplers) == 1
    assert report["gauges"]["mesh.bytes"] > 0


def test_an_edit_that_cannot_move_the_body_is_a_cache_hit():
    doc = docs.create_document(name="cache hit", resolution=32)
    fixtures._op("add_primitive", doc.id, {
        "shape": "box", "params": {"size": {"x": 0.05, "y": 0.04, "z": 0.01}}})
    # The same resolution the operation evaluated at: resolution is part of the
    # key, so re-evaluating at another one is a real rebuild, not a stale hit.
    resolution = docs.get_document(doc.id).derived.resolution
    with instrumentation.recording("noop") as rec:
        docs.evaluate_document(docs.get_document(doc.id), resolution=resolution)
    assert rec.report()["counters"].get("eval.cache_hit") == 1
    assert "eval.kernel" not in rec.report()["spans"]


def test_percentiles_are_nearest_rank_over_the_samples_given():
    samples = [0.001, 0.002, 0.003, 0.004, 0.010]
    summary = instrumentation.summarize(samples)
    assert summary["n"] == 5
    # Nearest-rank: every reported figure is a sample that was actually taken,
    # not a number interpolated between two of them.
    assert summary["p50_ms"] == 3.0
    assert summary["p95_ms"] == 10.0
    assert summary["min_ms"] == 1.0


def test_the_environment_block_says_which_kernels_are_present():
    env = instrumentation.environment()
    assert set(env) >= {"python", "platform", "cpu_count", "exact_kernel", "native_sampler"}
    assert isinstance(env["exact_kernel"], bool)


@pytest.mark.parametrize("name", ["exact_plate", "feature_tree", "sdf_shell", "library"])
def test_every_fast_fixture_still_builds_through_the_catalog(name):
    """A fixture builder that drifts from the catalog is a baseline nobody can take."""
    fixture = fixtures.FIXTURES[name]
    state = fixture.build(owner_id=None, resolution=32)
    if name == "library":
        assert len(state["document_ids"]) == state["count"]
        return
    doc = docs.get_document(str(state["document_id"]))
    assert doc is not None
    assert doc.derived.error is None
    assert doc.derived.volume > 0


def test_the_sdf_fixture_really_does_avoid_the_exact_kernel():
    """The point of that fixture is the sampler it forces, so assert on it."""
    state = fixtures.build_sdf_shell(resolution=32)
    doc = docs.get_document(str(state["document_id"]))
    assert doc.derived.sampler in ("native", "numpy")


def _box_id(doc_id: str) -> str:
    from app.cad_kernel.core.feature_tree import walk_nodes

    for node in walk_nodes(docs.get_document(doc_id).feature_tree):
        if node.get("type") == "BoxFeature":
            return node["id"]
    raise AssertionError("no box in that tree")
