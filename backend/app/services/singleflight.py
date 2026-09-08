"""One computation per key, however many callers ask for it at once.

A cache turns the *second* request for the same thing into a lookup. It does
nothing at all for the second request that arrives while the first is still
running — and that is the shape the expensive paths here actually have. Open a
document in two tabs after a restart and both viewports fetch the same mesh
under the same hash; neither finds it cached, so the body is evaluated twice, in
parallel, on a box whose whole problem is that one evaluation is slow.

So callers with the same key **wait for the first one** instead of racing it.
Three properties this has to have, and the third is the one that is easy to get
wrong:

- **The key is what makes two calls the same call.** For a mesh that is
  `(doc_id, mesh_hash)` — the hash names the triangles, so two requests for it
  are asking the identical question.
- **A failure is not shared as a cached failure.** The waiters see the same
  exception, because they asked the same question and it has the same answer
  right now, but nothing is remembered: the next caller tries again. A
  singleflight that memoised errors would turn one transient failure into a
  permanent one.
- **The slot is cleared even when the leader raises**, in a `finally`. Without
  that, one exception leaves every future caller for that key waiting on a
  future that will never complete — the whole system deadlocked on a key nobody
  can name, which is strictly worse than the duplicate work this removes.
"""

from __future__ import annotations

import threading
from typing import Any, Callable, Dict, Tuple, TypeVar

T = TypeVar("T")


class _Call:
    __slots__ = ("done", "value", "error", "waiters")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.value: Any = None
        self.error: BaseException | None = None
        self.waiters = 0


class Group:
    """A namespace of in-flight calls."""

    def __init__(self, name: str = ""):
        self.name = name
        self._lock = threading.Lock()
        self._calls: Dict[str, _Call] = {}
        self.leaders = 0
        self.joined = 0

    def do(self, key: str, fn: Callable[[], T]) -> T:
        with self._lock:
            call = self._calls.get(key)
            if call is not None:
                call.waiters += 1
                self.joined += 1
                leader = False
            else:
                call = self._calls[key] = _Call()
                self.leaders += 1
                leader = True

        if not leader:
            call.done.wait()
            if call.error is not None:
                raise call.error
            return call.value

        try:
            call.value = fn()
        except BaseException as exc:  # noqa: BLE001 — re-raised to every waiter
            call.error = exc
            raise
        finally:
            # Before the event is set, so a caller arriving between the set and
            # the pop joins nothing and starts its own call rather than
            # attaching to a slot that is about to disappear.
            with self._lock:
                self._calls.pop(key, None)
            call.done.set()
        return call.value

    def stats(self) -> Dict[str, int]:
        with self._lock:
            return {"in_flight": len(self._calls), "leaders": self.leaders,
                    "joined": self.joined}


#: The groups that exist, so a benchmark can report how much duplicate work is
#: actually being collapsed. `joined` is the number of evaluations *not* done.
GROUPS: Dict[str, Group] = {}


def group(name: str) -> Group:
    existing = GROUPS.get(name)
    if existing is None:
        existing = GROUPS[name] = Group(name)
    return existing


def stats() -> Dict[str, Dict[str, int]]:
    return {name: g.stats() for name, g in sorted(GROUPS.items())}


def keyed(*parts: Any) -> str:
    return "\x00".join(str(p) for p in parts)


def reset() -> None:
    """Drop the counters. For tests; never called by a request path."""
    for g in GROUPS.values():
        g.leaders = g.joined = 0
