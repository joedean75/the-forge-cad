"""Where the time and the bytes actually go.

The performance work in front of this repository is a list of *hypotheses* —
"parameter resolution is cheap", "the mesh encode is the tail", "the B-rep node
cache hits on an edit". None of them is worth acting on unmeasured, and none of
them can be measured from the outside: a request that takes 300 ms says nothing
about whether the kernel, the tessellation or the encode owns it.

So this is a recorder for the stages a document evaluation actually has, plus
the counters that say which *path* ran. Three properties are deliberate:

- **Off costs one global read.** A span with nothing recording does not allocate,
  does not take a lock and does not read the clock. Every instrumented call site
  is on the normal edit path, and instrumentation that taxes the thing it
  measures is how a profile ends up describing itself.
- **A module global, not a ContextVar.** Everything else scoped in this codebase
  (`rollout`, `trajectories`, `caller`) is a ContextVar, and this is the one that
  cannot be: the benchmark harness drives the app through `TestClient`, whose
  request handling runs on an anyio portal thread that does not inherit the
  caller's context. A recorder that silently recorded nothing under the harness
  would be worse than no recorder. The cost is that `recording()` is
  process-wide — which is what a benchmark wants and what a request handler must
  never turn on for itself.
- **Counters distinguish the path, not just the cost.** `sampler.brep` vs
  `sampler.numpy`, `eval.cache_hit` vs `eval.cache_miss`, `brep_node.hit` vs
  `brep_node.miss`. A latency number without them describes one arbitrary
  branch: the same document is a 12 ms cache hit or a 400 ms exact rebuild, and
  averaging the two measures the ratio of the fixture, not the system.
"""

from __future__ import annotations

import math
import os
import platform
import sys
import threading
import time
from contextlib import contextmanager
from typing import Dict, Iterator, List, Optional


class Stat:
    """Wall time for one named stage: how often, how long, and the worst case."""

    __slots__ = ("count", "total_s", "max_s")

    def __init__(self) -> None:
        self.count = 0
        self.total_s = 0.0
        self.max_s = 0.0

    def add(self, seconds: float) -> None:
        self.count += 1
        self.total_s += seconds
        if seconds > self.max_s:
            self.max_s = seconds

    def as_dict(self) -> dict:
        return {
            "count": self.count,
            "total_ms": round(self.total_s * 1000.0, 3),
            "mean_ms": round(self.total_s * 1000.0 / self.count, 3) if self.count else 0.0,
            "max_ms": round(self.max_s * 1000.0, 3),
        }


class Recorder:
    """Spans, counters and gauges collected over one measured region.

    The lock is only ever contended while recording is on, which is a benchmark
    run and never a served request. Without it a background job's span and the
    request thread's span race on the same `Stat` and the counts come out short —
    a measurement error that looks exactly like a fast path.
    """

    def __init__(self, label: str = "") -> None:
        self.label = label
        self.spans: Dict[str, Stat] = {}
        self.counters: Dict[str, int] = {}
        self.gauges: Dict[str, float] = {}
        self._lock = threading.Lock()

    def add_span(self, name: str, seconds: float) -> None:
        with self._lock:
            stat = self.spans.get(name)
            if stat is None:
                stat = self.spans[name] = Stat()
            stat.add(seconds)

    def add_count(self, name: str, n: int = 1) -> None:
        with self._lock:
            self.counters[name] = self.counters.get(name, 0) + n

    def set_gauge(self, name: str, value: float) -> None:
        """Last value wins, and `<name>.max` remembers the peak.

        A gauge is a size rather than a duration — the bytes of a mesh, the
        triangle count, resident memory. The peak is the half that matters for a
        budget; the last value is the half that matters for a single run.
        """
        with self._lock:
            self.gauges[name] = float(value)
            peak = self.gauges.get(name + ".max")
            if peak is None or value > peak:
                self.gauges[name + ".max"] = float(value)

    def report(self) -> dict:
        with self._lock:
            return {
                "label": self.label,
                "spans": {k: v.as_dict() for k, v in sorted(self.spans.items())},
                "counters": dict(sorted(self.counters.items())),
                "gauges": {k: round(v, 3) for k, v in sorted(self.gauges.items())},
            }


# The one active recorder, or None. Read on every instrumented call site, so it
# is a plain module global rather than anything with a lookup cost.
_ACTIVE: Optional[Recorder] = None


def active() -> Optional[Recorder]:
    return _ACTIVE


@contextmanager
def recording(label: str = "", recorder: Optional[Recorder] = None) -> Iterator[Recorder]:
    """Collect into `recorder` for the duration of the block.

    Nested regions reuse the outer recorder rather than shadowing it: a
    benchmark measures a whole scenario, and a service inside it that opened its
    own would hide every span from the run that asked for them.
    """
    global _ACTIVE
    if _ACTIVE is not None:
        yield _ACTIVE
        return
    rec = recorder or Recorder(label)
    _ACTIVE = rec
    try:
        yield rec
    finally:
        _ACTIVE = None


@contextmanager
def span(name: str) -> Iterator[None]:
    """Time a stage, when something is recording. A no-op otherwise."""
    rec = _ACTIVE
    if rec is None:
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        rec.add_span(name, time.perf_counter() - started)


def count(name: str, n: int = 1) -> None:
    rec = _ACTIVE
    if rec is not None:
        rec.add_count(name, n)


def gauge(name: str, value: float) -> None:
    rec = _ACTIVE
    if rec is not None:
        rec.set_gauge(name, value)


# ---------------------------------------------------------------------------
# Memory
# ---------------------------------------------------------------------------

def rss_bytes() -> int:
    """Resident set size right now, or 0 where the platform will not say.

    `/proc` first because it is the *current* figure; `ru_maxrss` is a
    high-water mark that never comes down, so it cannot show a cache being
    evicted. Both are reported by the benchmark harness, which needs each for a
    different question: "how much does this workload hold" and "how much did it
    ever hold".
    """
    try:
        with open("/proc/self/statm", "r", encoding="ascii") as fh:
            pages = int(fh.read().split()[1])
        return pages * os.sysconf("SC_PAGE_SIZE")
    except Exception:  # noqa: BLE001 — not every platform has /proc
        pass
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        # Linux reports kilobytes, macOS bytes.
        return int(peak) * (1 if sys.platform == "darwin" else 1024)
    except Exception:  # noqa: BLE001
        return 0


def peak_rss_bytes() -> int:
    """The high-water mark for this process, or 0 where unavailable."""
    try:
        import resource

        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return int(peak) * (1 if sys.platform == "darwin" else 1024)
    except Exception:  # noqa: BLE001
        return 0


def environment() -> dict:
    """What the numbers were measured on.

    A latency budget without this is a number somebody else cannot reproduce or
    refute — which is the failure mode the whole measurement sprint exists to
    avoid. Kernel availability is in here because the exact and SDF paths differ
    by an order of magnitude on the same document.
    """
    try:
        from app.cad_kernel.evaluator import brep

        exact = bool(getattr(brep, "AVAILABLE", False))
    except Exception:  # noqa: BLE001
        exact = False
    try:
        from app.cad_kernel.evaluator import sdf_native

        native = bool(getattr(sdf_native, "AVAILABLE", False))
    except Exception:  # noqa: BLE001
        native = False
    return {
        "python": platform.python_version(),
        "platform": platform.platform(),
        "machine": platform.machine(),
        "cpu_count": os.cpu_count(),
        "exact_kernel": exact,
        "native_sampler": native,
    }


# ---------------------------------------------------------------------------
# Latency samples
# ---------------------------------------------------------------------------

def percentile(samples: List[float], pct: float) -> float:
    """Nearest-rank percentile over a small sample.

    Nearest-rank rather than interpolated on purpose: these are wall-clock
    timings with a handful of repeats, and an interpolated p95 over 10 samples
    invents a value between two real ones and reports it to three decimals.

    `ceil`, not `round`: the rank is the *smallest* index whose value at least
    `pct` percent of the samples fall at or below, which is the textbook
    definition. Rounding would take the p50 of five samples to the second one —
    below the median — because Python rounds 2.5 to 2.
    """
    if not samples:
        return 0.0
    ordered = sorted(samples)
    rank = max(1, math.ceil(pct / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def summarize(samples: List[float]) -> dict:
    """p50/p95 in milliseconds, plus the range and the repeat count."""
    return {
        "n": len(samples),
        "p50_ms": round(percentile(samples, 50) * 1000.0, 3),
        "p95_ms": round(percentile(samples, 95) * 1000.0, 3),
        "min_ms": round(min(samples) * 1000.0, 3) if samples else 0.0,
        "max_ms": round(max(samples) * 1000.0, 3) if samples else 0.0,
    }
