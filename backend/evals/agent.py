"""The agent benchmark: run a goal, score the body it produced.

The scoring is not new and deliberately so. A task carries a `PartSpec` — the
same structure `set_spec` takes — and the answer comes from
`design_spec.verify_against_spec`, which is already the reward function and
already refuses to pass a requirement it cannot measure. Inventing a second
scorer would mean two definitions of "correct", and the weaker one always wins
an argument.

Three things this harness has to get right, all of them mistakes that were
cheap to make:

* **Pin the conditions.** `capped_resolution` clamps to the account's plan, so
  the same document scores differently depending on who ran it. A benchmark
  whose number moves with the runner is not a benchmark. `rollout.rollout`
  pins it and turns metering off in the same breath — a few hundred edits into
  a sweep, a 402 arrives mid-run and the sweep is scrap.
* **Report more than a pass rate.** `MISSING_ARG` dominating and `PRECONDITION`
  dominating are different problems with different fixes, and only the
  error-code histogram tells them apart.
* **Never score with a model.** Everything else in this system measures the
  body. An LLM judge would be the one component that cannot itself be checked,
  in the one place where being wrong is invisible — the same reason
  `_approximate_simulation` was taken out of the catalog.
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

# The default is what the copilot itself defaults to. Raising it to make the
# score go up tells you the loop is inefficient, not that it is better.
DEFAULT_MAX_STEPS = 8
# Pinned, not "whatever the plan allows". Coarse on purpose: the benchmark
# scores dimensions and hole counts, not surface finish, and every task pays it.
DEFAULT_RESOLUTION = 96


@dataclass(frozen=True)
class Task:
    """A goal, and a machine-checkable statement of what satisfying it means."""

    id: str
    goal: str
    requirements: Sequence[Dict[str, Any]]
    max_steps: int = DEFAULT_MAX_STEPS
    tags: Sequence[str] = ()
    #: A fixed op sequence standing in for the planner. Present on the tasks
    #: that must run without an API key — see `scripted_planner`.
    script: Optional[Sequence[Dict[str, Any]]] = None
    #: What the *API* should do with this script:
    #:
    #: ``"succeed"`` — every call is legitimate and the part should come out right.
    #: ``"refuse"``  — the script replays a known mistake that must be rejected.
    #: ``"warn"``    — the call is *possible* but almost certainly not meant. A
    #:                 100 m box is a legal body and a 2 m sheet is a real part,
    #:                 so refusing on magnitude would block real work; naming the
    #:                 suspicion in a warning the planner reads is the honest
    #:                 answer, and this asserts the warning arrives.
    #:
    #: A call that builds the wrong body and reports nothing is the failure being
    #: measured, and it is the same failure in all three cases.
    expect: str = "succeed"

    @property
    def scripted(self) -> bool:
        return self.script is not None


@dataclass
class TaskResult:
    task_id: str
    score: float                       # fraction of requirements met
    met: int
    total: int
    geometry_ok: bool
    steps: int
    max_steps: int
    #: True when the planner said `done` rather than running out of budget.
    finished: bool
    ok_steps: int
    error_codes: Dict[str, int] = field(default_factory=dict)
    unmet: List[str] = field(default_factory=list)
    warnings: int = 0
    seconds: float = 0.0
    note: Optional[str] = None
    #: Did the API reject at least one call in this run.
    refused: bool = False
    expect: str = "succeed"

    @property
    def flagged(self) -> bool:
        """Rejected *or* warned about — the call did not pass silently."""
        return self.refused or self.warnings > 0

    @property
    def solved(self) -> bool:
        return self.score >= 1.0 and self.geometry_ok

    @property
    def as_expected(self) -> bool:
        """Refusal tasks want a refusal; the rest want a correct part.

        Kept separate from `solved` on purpose. A task that replays a wrong call
        cannot produce a right part, so scoring it by the spec alone would mark
        the surface working perfectly and the surface doing nothing identically.
        """
        if self.expect == "refuse":
            return self.refused
        if self.expect == "warn":
            return self.flagged
        return self.solved


def scripted_planner(script: Sequence[Dict[str, Any]]) -> Callable[..., dict]:
    """A planner that replays a fixed sequence and then declares done.

    The point is not to measure a model — it cannot — but to keep the harness
    itself alive. Scoring, budgets, the histogram and the report are exercised
    on every commit this way, so they cannot rot between the sprints where an
    API key is available. It is also how a *deliberately wrong* sequence is
    turned into an assertion: see `evals.tasks.ADVERSARIAL`.
    """
    remaining = list(script)

    def plan(goal: str, transcript: list, summary: dict) -> dict:
        if not remaining:
            return {"done": True, "summary": "script exhausted"}
        step = dict(remaining.pop(0))
        step.setdefault("rationale", "scripted")
        return step

    return plan


def run_task(task: Task, planner: Optional[Callable[..., dict]] = None, *,
             resolution: int = DEFAULT_RESOLUTION,
             owner_id: Optional[str] = None) -> TaskResult:
    """Build a document from the goal and score it against the task's spec."""
    from app.services import cad_operations, copilot, document_service, rollout

    if planner is None and task.script is not None:
        planner = scripted_planner(task.script)

    started = time.perf_counter()
    with rollout.rollout(resolution=resolution):
        doc = document_service.create_document(name=f"eval:{task.id}", owner_id=owner_id)
        spec = cad_operations.execute("set_spec", doc.id, {
            "requirements": list(task.requirements), "goal": task.goal})
        if not spec["ok"]:
            return TaskResult(task.id, 0.0, 0, len(task.requirements), False, 0,
                              task.max_steps, False, 0,
                              note=f"spec rejected: {spec['error']['message']}",
                              seconds=round(time.perf_counter() - started, 2))

        run = copilot.run(doc.id, task.goal, max_steps=task.max_steps, planner=planner)
        report = cad_operations.execute("verify_against_spec", doc.id, {})
        geometry = cad_operations.execute("check_geometry", doc.id, {})

    steps = [s for s in run["steps"] if s.get("kind") == "op"]
    codes: Dict[str, int] = {}
    for step in steps:
        message = step.get("error")
        if message:
            codes[str(message).split(":", 1)[0]] = codes.get(str(message).split(":", 1)[0], 0) + 1

    checks = (report["result"] or {}).get("checks") or []
    met = sum(1 for c in checks if c.get("status") == "pass")
    unmet = [f"{c.get('id') or c.get('label')}: {c.get('detail')}"
             for c in checks if c.get("status") != "pass"]
    score = float((report["result"] or {}).get("score") or 0.0)

    geometry_rows = (geometry["result"] or {}).get("checks") or []
    geometry_ok = geometry["ok"] and all(r.get("status") != "fail" for r in geometry_rows)

    # Reported by the loop rather than inferred from the step count: a run that
    # spent its last step declaring done and one that was cut off there look
    # identical from outside, and they are not the same outcome.
    finished = bool(run.get("finished"))

    return TaskResult(
        task_id=task.id,
        expect=task.expect,
        refused=any(not s.get("ok") for s in steps),
        score=round(score, 4),
        met=met,
        total=len(checks),
        geometry_ok=geometry_ok,
        steps=len(steps),
        max_steps=task.max_steps,
        finished=finished,
        ok_steps=sum(1 for s in steps if s.get("ok")),
        error_codes=codes,
        unmet=unmet,
        warnings=sum(len(s.get("warnings") or []) for s in steps),
        seconds=round(time.perf_counter() - started, 2),
    )


def run_suite(tasks: Sequence[Task], *, planner_factory: Optional[Callable[[Task], Any]] = None,
              resolution: int = DEFAULT_RESOLUTION,
              owner_id: Optional[str] = None) -> Dict[str, Any]:
    """Run every task and aggregate. ``planner_factory(task)`` overrides the planner."""
    results = [run_task(task,
                        planner_factory(task) if planner_factory else None,
                        resolution=resolution, owner_id=owner_id)
               for task in tasks]
    return report(results, resolution=resolution)


def report(results: Sequence[TaskResult], *, resolution: int = DEFAULT_RESOLUTION
           ) -> Dict[str, Any]:
    """The aggregate. One sample per task — the spread is the caller's problem."""
    codes: Dict[str, int] = {}
    for result in results:
        for code, n in result.error_codes.items():
            codes[code] = codes.get(code, 0) + n

    count = len(results) or 1
    adversarial = [r for r in results if r.expect in ("refuse", "warn")]
    return {
        "tasks": len(results),
        "resolution": resolution,
        # **What produced this number, recorded beside it.** Three baselines were
        # compared as though they shared their conditions, and the artifacts could
        # not say: resolution was pinned and written down, while the model and the
        # prompt profile — the two things that differ most between a fine-tune and
        # a frontier model — were in the environment and then gone. A benchmark
        # whose output cannot name its subject is one whose comparisons rest on
        # whoever remembers how it was run.
        "model": _llm_model(),
        "prompt_profile": _prompt_profile(),
        "tool_calling": _supports_tools(),
        # The adversarial half's headline: of the calls that *should* have been
        # rejected, how many were. Every one that was not returned `ok: true`
        # and a body nobody asked for.
        "expected_flags": len(adversarial),
        "caught": sum(1 for r in adversarial if r.flagged),
        "as_expected": sum(1 for r in results if r.as_expected),
        # The headline: how many parts were actually right, not how many runs
        # completed without raising.
        "solved": sum(1 for r in results if r.solved),
        "solved_rate": round(sum(1 for r in results if r.solved) / count, 4),
        "mean_score": round(sum(r.score for r in results) / count, 4),
        "geometry_ok": sum(1 for r in results if r.geometry_ok),
        "finished": sum(1 for r in results if r.finished),
        "mean_steps": round(sum(r.steps for r in results) / count, 2),
        "failed_calls": sum(sum(r.error_codes.values()) for r in results),
        "by_error_code": dict(sorted(codes.items(), key=lambda kv: -kv[1])),
        "seconds": round(sum(r.seconds for r in results), 2),
        "results": [asdict(r) for r in results],
    }


def _llm_model() -> Optional[str]:
    """The model the suite actually talked to, or None when nothing configured one."""
    try:
        from app.services import llm_client
        # `pinned_model` is the context manager that *sets* the pin, not a getter:
        # calling it here returned a `_GeneratorContextManager` into the artifact.
        pin = (llm_client.CALL_CONTEXT.get() or {}).get("model")
        return pin or llm_client.LLM_MODEL or None
    except Exception:                       # a run with no provider configured
        return None


def _supports_tools() -> Optional[bool]:
    """Whether this run *sent* tools, not whether the provider accepts them.

    The `finetuned` profile returns `tools=None` however capable the provider is,
    so reporting the provider's capability here would label a prose run as
    schema-constrained — the one distinction `_via` exists to keep.
    """
    try:
        from app.services import copilot, llm_client
        if copilot.PROMPT_PROFILE == "finetuned":
            return False
        return bool(llm_client.supports_tools())
    except Exception:
        return None


def _prompt_profile() -> Optional[str]:
    try:
        from app.services import copilot
        return copilot.PROMPT_PROFILE
    except Exception:
        return None


def format_report(data: Dict[str, Any]) -> str:
    lines = [
        f"{'task':<30} {'want':>7} {'got':>9} {'score':>6} {'geom':>5} {'steps':>7}  unmet",
        "-" * 104,
    ]
    for row in data["results"]:
        unmet = "; ".join(row["unmet"])[:38] or ("—" if row["score"] >= 1 else "")
        want = row["expect"]
        flagged = row["refused"] or row["warnings"] > 0
        got = ("refused" if row["refused"]
               else "warned" if row["warnings"]
               else "solved" if row["score"] >= 1 and row["geometry_ok"]
               else "built-wrong")
        expected = (row["refused"] if want == "refuse"
                    else flagged if want == "warn"
                    else row["score"] >= 1 and row["geometry_ok"])
        mark = " " if expected else "!"
        lines.append(
            f"{row['task_id']:<30} {want:>7} {got:>9}{mark}{row['score']:>5.2f} "
            f"{'ok' if row['geometry_ok'] else 'FAIL':>5} "
            f"{row['steps']:>3}/{row['max_steps']:<3}  {unmet}")
    lines += [
        "-" * 104,
        f"as expected {data['as_expected']}/{data['tasks']}  "
        f"caught {data['caught']}/{data['expected_flags']} bad calls  "
        f"solved {data['solved']}/{data['tasks']}  "
        f"mean score {data['mean_score']:.3f}  "
        f"geometry ok {data['geometry_ok']}/{data['tasks']}  "
        f"finished {data['finished']}/{data['tasks']}  "
        f"mean steps {data['mean_steps']}",
        f"failed calls {data['failed_calls']}  by code: {data['by_error_code'] or '—'}",
        f"resolution {data['resolution']} (pinned)  {data['seconds']}s",
    ]
    return "\n".join(lines)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    from evals import tasks as task_module

    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", default="scripted",
                        choices=sorted(task_module.SUITES),
                        help="which task set to run")
    parser.add_argument("--resolution", type=int, default=DEFAULT_RESOLUTION)
    parser.add_argument("--baseline", help="write the aggregate here as JSON")
    parser.add_argument("--compare", help="read a baseline and print the movement")
    args = parser.parse_args(argv)

    from app.services import llm_client, persistence

    # A benchmark is routinely pointed at a throwaway database (a sweep must not
    # write into somebody's library), and nothing else on this path creates the
    # schema — `main.py` does it for the server and `conftest` for the tests, so
    # running the harness against a fresh file failed on every persist.
    persistence.init_db()

    suite = task_module.SUITES[args.suite]
    if not all(t.scripted for t in suite) and not llm_client.is_configured():
        print(f"suite {args.suite!r} needs an LLM key (LLM_API_KEY); "
              f"only the scripted suites run without one.")
        return 2

    data = run_suite(suite, resolution=args.resolution)
    print(format_report(data))

    if args.compare:
        with open(args.compare) as handle:
            old = json.load(handle)
        print(f"\nvs {args.compare}: solved {old['solved']} -> {data['solved']}, "
              f"mean score {old['mean_score']:.3f} -> {data['mean_score']:.3f}, "
              f"mean steps {old['mean_steps']} -> {data['mean_steps']}")
    if args.baseline:
        with open(args.baseline, "w") as handle:
            json.dump(data, handle, indent=2)
        print(f"\nbaseline written to {args.baseline}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
