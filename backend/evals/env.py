"""A reset/step environment over the operation catalog.

`evals.agent` runs a whole task and scores the result. A training loop needs the
other shape: hand it one action, get back the new state and what that action was
worth. Everything underneath already exists — the catalog is the action space,
`observation.summarize` is the state an agent reads, `design_spec.verify` is the
reward, `rollout` pins the conditions and `trajectories` records the episode —
so this is an adapter over them rather than a second implementation of any.

It lives in `evals/` and not in `services/` for the reason `agent.py` does: it
is a harness, not a product path.

**It is deliberately not a `gymnasium.Env` subclass.** Inheriting would add a
dependency to the whole backend for one interface. `reset()` and `step()` return
the shapes that interface expects — `(obs, info)` and
`(obs, reward, terminated, truncated, info)` — so a wrapper is five lines for
anyone who wants the real base class.

Three things about the reward are load-bearing:

- **It never raises.** `verify_against_spec` refuses a document with no
  evaluated geometry, and `design_spec` fails loudly on a requirement it cannot
  measure. Both are correct and neither may escape `step()`: an environment that
  throws halfway through an episode is one every training loop wraps in a bare
  `except` that then hides real failures. An unscoreable state is `0.0` with the
  reason in `info["reward_note"]`.
- **A body that fails `check_geometry` is never terminal**, whatever it scores.
  The spec can be fully met by a body that is not a valid solid — that is the
  "passes every check, wrong shape" failure the snapshot review exists for, and
  rewarding it here would teach exactly that.
- **The score is recomputed every step**, which costs an evaluation per action.
  That is the price of a dense reward, and it is the right trade for a harness:
  a sparse terminal-only reward makes the credit assignment the environment's
  problem rather than the algorithm's.
"""

from __future__ import annotations

from contextlib import ExitStack
from typing import Any, Dict, Optional, Tuple

from evals.agent import DEFAULT_RESOLUTION, Task

Observation = Dict[str, Any]


class ForgeEnv:
    """One document, one spec, one action per step."""

    def __init__(self, task: Task, *, resolution: int = DEFAULT_RESOLUTION,
                 owner_id: Optional[str] = None):
        self.task = task
        self.resolution = resolution
        self.owner_id = owner_id
        self.doc_id: Optional[str] = None
        self.trajectory_id: Optional[str] = None
        self.steps = 0
        self._stack: Optional[ExitStack] = None

    # -- lifecycle ----------------------------------------------------------

    def reset(self) -> Tuple[Observation, Dict[str, Any]]:
        """A fresh document with the task's spec recorded on it."""
        from app.services import cad_operations, document_service, rollout, trajectories

        self.close()
        self.steps = 0
        stack = ExitStack()
        # Metering off and resolution pinned for the whole episode: the plan's
        # cap would otherwise decide geometry fidelity, and two runs of one
        # trajectory on different accounts would not be comparable.
        stack.enter_context(rollout.rollout(resolution=self.resolution))
        doc = document_service.create_document(name=f"env:{self.task.id}",
                                               owner_id=self.owner_id)
        self.doc_id = doc.id
        spec = cad_operations.execute("set_spec", doc.id, {
            "requirements": list(self.task.requirements), "goal": self.task.goal},
            include_state=False)
        # Every episode is a recorded trajectory for free, which is the whole
        # reason this sits over `trajectories` rather than beside it.
        self.trajectory_id = stack.enter_context(
            trajectories.recording(doc.id, goal=self.task.goal, kind="env", actor="env"))
        self._stack = stack

        info: Dict[str, Any] = {"doc_id": doc.id, "goal": self.task.goal,
                                "trajectory_id": self.trajectory_id,
                                "max_steps": self.task.max_steps,
                                "requirements": len(self.task.requirements)}
        if not spec["ok"]:
            info["spec_error"] = (spec.get("error") or {}).get("message")
        return self._observe(), info

    def close(self) -> None:
        """End the episode, closing the trajectory and the rollout scope."""
        if self._stack is not None:
            self._stack.close()
            self._stack = None

    def __enter__(self) -> "ForgeEnv":
        self.reset()
        return self

    def __exit__(self, *_exc) -> None:
        self.close()

    # -- the step -----------------------------------------------------------

    def step(self, action: Dict[str, Any]
             ) -> Tuple[Observation, float, bool, bool, Dict[str, Any]]:
        """Run one operation. ``action`` is ``{"op": name, "args": {...}}``."""
        from app.services import cad_operations

        if self.doc_id is None:
            raise RuntimeError("call reset() before step()")
        op = str((action or {}).get("op") or "")
        args = (action or {}).get("args") or {}

        envelope = cad_operations.execute(op, self.doc_id, args, include_state=False)
        self.steps += 1

        reward, note = self._reward()
        geometry_ok = self._geometry_ok()
        info: Dict[str, Any] = {
            "ok": bool(envelope.get("ok")),
            "op": op,
            "geometry_ok": geometry_ok,
            "steps": self.steps,
            "warnings": envelope.get("warnings") or [],
        }
        if envelope.get("error"):
            # The typed code, which is what an agent branches on — the same
            # thing the copilot's transcript carries.
            info["error"] = envelope["error"].get("code")
            info["error_message"] = envelope["error"].get("message")
            info["remedy"] = envelope["error"].get("remedy")
        if note:
            info["reward_note"] = note

        # A spec fully met by a body that is not a valid solid is the failure
        # this whole surface exists to catch, so it does not end the episode.
        terminated = reward >= 1.0 and geometry_ok
        truncated = (not terminated) and self.steps >= self.task.max_steps
        return self._observe(), reward, terminated, truncated, info

    # -- what the agent sees, and what it was worth -------------------------

    def _observe(self) -> Observation:
        from app.services import document_service, observation

        doc = document_service.get_document(self.doc_id) if self.doc_id else None
        return observation.summarize(doc) if doc is not None else {}

    def _reward(self) -> Tuple[float, Optional[str]]:
        """The fraction of the spec met, or 0.0 and why it could not be scored."""
        from app.services import cad_operations

        report = cad_operations.execute("verify_against_spec", self.doc_id, {},
                                        include_state=False)
        if not report["ok"]:
            return 0.0, (report.get("error") or {}).get("message")
        return float((report.get("result") or {}).get("score") or 0.0), None

    def _geometry_ok(self) -> bool:
        from app.services import cad_operations

        env = cad_operations.execute("check_geometry", self.doc_id, {},
                                     include_state=False)
        rows = (env.get("result") or {}).get("checks") or []
        return bool(env.get("ok")) and all(r.get("status") != "fail" for r in rows)
