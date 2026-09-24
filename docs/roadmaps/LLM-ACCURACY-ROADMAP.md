# LLM accuracy roadmap — three sprints

> **Status: A1, A2 and A3 are done.** Measured the same way as the numbers
> below, on the adversarial suite that ships in `evals/tasks.py`:
>
> | | before | after |
> |---|---|---|
> | Known bad calls caught (refused or flagged) | **0 / 5** | **5 / 5** |
> | Adversarial suite behaving as declared | 2 / 7 | 7 / 7 |
> | Parameters with no description | 71 / 142 | **0** |
> | Parameters with no unit and not declared unitless | 20+ | **0** |
> | What the planner is handed | 12,763 chars of parameter *names* | 45 KB of JSON Schema, or prose carrying type + unit + required |
> | Generators of the action space | 3 (prompt, corpus, tools) | **1** (`services/action_space.py`) |
> | Error codes carrying a remedy | 0 / 8 | **8 / 8** |
> | Loop reads its own reward function | never | once, on the transition to done |
>
> Every one of those five bad calls previously returned `ok: true` with a body
> nobody asked for. The largest was structural: `additionalProperties: false`
> was declared on nested objects and never enforced, so `add_primitive` with
> three invented key names dropped all three and returned a default 50 mm cube.
>
> **Opus 5 on the same suite, for comparison** (2026-09-24, $1.43 through
> OpenRouter, baseline in `backend/data/eval-goals-opus-20260924.json`):
> **solved 5/6, mean score 0.900, geometry 6/6, finished 4/6, mean 2.33 steps,
> 1 failed call.** So the action space is drivable and the 0/6 below is the
> model, not the surface. The one task Opus lost is `plate-4-holes`, where it
> built a 180 mm plate against a 100 mm requirement and was refused — the same
> task the fine-tune also failed, which makes it the first place to look at the
> *task* rather than the model.
>
> **The goal suite has now been run once**, against a local fine-tune of Qwen3
> 0.6B served through `backend/training/serve.py` (2026-09-24, baseline in
> `backend/data/eval-goals-finetuned-20260924.json`): **solved 0/6, mean score
> 0.533, geometry valid 5/6, finished 0/6, mean 3.5 steps, 9 failed calls all
> `BAD_ARG`.** It builds the first primitive correctly and then stops: no task
> reached its second feature (no holes, no boss, no fillets, no material), and no
> run called `finish_task`. That is the expected result for a 0.6B base on 2,004
> samples and it is a floor, not a verdict on the action space — what it measures
> is that the surface is drivable and where a small model falls off it. A
> frontier-model number on the same suite is still missing, and without it there
> is nothing to compare against.
>
> Running it found two real defects, both now fixed: a declared parameter
> expression bound before its parameter existed reached the kernel as a *string*
> and came back as `unsupported operand type(s) for /: 'str' and 'float'`
> (`parameters.resolve_tree` swallowed the typed error to protect enum strings;
> the two are told apart by the leading `=`), and `evals.agent` never created the
> schema, so pointing the harness at a throwaway database failed on every
> persist.
>
> **The original note, for the record:** `evals/tasks.py::GOALS` holds six
> tasks across primitives, holes, booleans, materials and the exact kernel, and
> they need an API key this environment does not have — `evals.agent --suite
> goals` exits 2 and says so rather than reporting a number it did not measure.
> So the headline "did A2 make a model better?" is still open; what is measured
> is that five specific ways of being wrong now produce an error instead of a
> part.

The operation catalog was built to be an action space for a model: typed
arguments, validation before a handler runs, typed failures, results that say
what changed, verifiers that can score a part. All of that is real. What is
missing is the other half of the sentence — **nothing measures whether a model
driving it actually gets the part right**, and the surface it is actually shown
at inference time is much weaker than the one the catalog can describe.

Same format as the other roadmaps: what to do, how you know it worked, and the
trap that will actually bite. Every number below was measured against `main` on
2026-08-03 and the measurement is quoted so it can be re-run rather than
believed.

## What is already true

| Landed | Where |
|---|---|
| 58 operations (33 mutate, 25 query), strict JSON Schema per operation | `services/op_schema.py`, `GET /api/operations` |
| Arguments validated before a handler runs; a malformed call is refused, not half-applied | `op_schema.validate` |
| Failures carry a stable code, not just prose | `services/op_errors.py` (8 codes) |
| Every result reports what changed, geometry-only | `services/observation.delta` |
| A reward function that fails loudly rather than skipping | `services/design_spec.verify_against_spec` |
| Is it a valid solid at all | `services/geometry_check.py` |
| Every operation recorded when a session is open | `services/trajectories.py` |
| Whole-tree generation recovered as an op sequence and verified by replay | `services/decompiler.py` — **1.00** hit rate over the 17-document coverage corpus |
| An agentic loop over the same op path the UI uses | `services/copilot.py` |

That is a system a model *can* drive. What follows is whether it drives it well.

---

## Sprint A1 — Know whether it works

**Why now.** There is no answer to "is the agent getting better?" — not a weak
answer, none. `decompiler.corpus_report` measures the **data** side (can a tree
be recovered and replayed: 1.00) and says nothing about whether a model asked
for a bracket gets a bracket. Every change in A2 and A3 is otherwise a guess,
and the two most tempting changes — a bigger prompt, more steps — are exactly
the ones that can make things worse while feeling better.

**What to do.**

1. **A frozen task set, scored by the verifier that already exists.** A task is
   `{goal, spec}` where `spec` is a `PartSpec` — the same thing `set_spec` takes
   and `verify_against_spec` scores. Run the copilot against the goal, then
   score the resulting document. Reusing the reward function rather than
   inventing a second one is the whole point: `design_spec` already refuses to
   pass a requirement it cannot measure, so a task cannot quietly score 1.0
   because half of it was skipped.
2. **Report more than a pass rate.** Per task: spec score, `check_geometry`
   verdict, steps used against the budget, whether it terminated by saying
   `done` or by running out, wall time, tokens. Aggregate: mean score, fraction
   scoring 1.0, and **the error-code histogram** — `MISSING_ARG` dominating and
   `PRECONDITION` dominating are different problems with different fixes, and
   the histogram is what tells them apart.
3. **Pin the conditions.** `rollout.rollout(resolution=…)` exists precisely for
   this: `capped_resolution` clamps to the account's plan, so the same document
   scores differently for different accounts, and a benchmark whose number moves
   with who ran it is not a benchmark. Metering off for the same reason — a few
   hundred edits into a sweep, a 402 arrives mid-run.
4. **A scripted subset that runs with no key.** The planner is already
   injectable (`copilot.run(planner=…)`). A handful of tasks with a recorded
   op sequence as the "planner" turns the harness itself — scoring, budgets,
   reporting — into something pytest defends on every commit, so the harness
   cannot rot between the sprints where a key is available.

**How you know it worked.** `python -m evals.agent --tasks evals/tasks.json`
prints a table and writes a baseline. Re-running it on an unchanged tree
reproduces the same numbers for the scripted tasks, and the LLM tasks vary only
by sampling. And you can answer, in one number, whether A2 helped.

**Traps.**

- **Do not score with a model.** Every verifier in this system measures the
  body; an LLM judge would be the one unmeasurable thing in a pipeline whose
  whole design rule is that an unmeasurable requirement fails loudly. It is also
  the exact failure `_approximate_simulation` was deleted for: a verifiable
  signal that means nothing is worse than no signal, because a model will
  optimise it.
- **A single sample is noise.** Report pass@1 over *n* seeds with the spread, or
  say plainly that the number is one draw. A 3-task, 1-sample "benchmark" moving
  from 0.66 to 1.00 means nothing at all.
- **A benchmark you can pass by memorising is worse than none.** Keep a held-out
  split, and never let a task's exact wording into a prompt, a default, or a
  parser shortcut. The `"rc" in prompt` substring hack in the old text-to-CAD
  parser is what this looks like when it happens.
- Tasks must cover the feature space, not the easy corner — the same lesson
  `corpus_report` learned when a corpus of plain boxes scored 100%.

**Size.** Medium. The harness is small; choosing tasks that are both realistic
and machine-scorable is the work.

---

## Sprint A2 — A surface that is hard to get wrong

**Why now.** The catalog can describe itself precisely, and at inference time it
does not. Measured:

```
copilot system prompt              14,001 chars
  of which the catalog             12,763 chars
  form: "- add_extrude (mutate): <desc> · params: sketch, profile, plane, offset, depth, operation"
GET /api/operations/tools          35,734 bytes of real JSON Schema — unused by the copilot
```

The model is handed **parameter names and nothing else**: no types, no
nesting, no required list, no units. The strict schema those names came from is
already generated, already served, and already the thing a constrained decoder
would need. Three more measurements from the same catalog:

```
142 parameters
  71  have no description at all
  30  are marked required
  20  length parameters never state a unit anywhere in their description
```

And the consequences are not theoretical. Every one of these returns `ok: true`
with no warning:

```
add_primitive  box 100 x 60 x 20   ->  a 100-metre part   (mm read as m)
add_extrude    {"depth": 0.01}     ->  a 50 x 30 mm profile nobody asked for
add_revolve    {}                  ->  a body, from defaults
add_sheet_metal{}                  ->  a 100 x 60 plate
```

The units one is the worst, and it is structural rather than careless: the
**observation is in millimetres** (`bbox_size_mm`) and the **arguments are in
metres**. A model reads `100.0` and writes `0.1`, every single call, with
nothing but a line of prose in the system prompt holding the line.

**What to do.**

1. **Give the model the schema, not a list of names.** `llm_client` already
   speaks the OpenAI-style chat protocol, which carries `tools` — pass
   `/api/operations/tools` and let the provider constrain the call. Keep the
   prose catalog as the fallback for providers that do not support it, and make
   which one ran observable, the way `AnalysisReport.solver` marks an estimate.
2. **Put the unit in the schema, not in the prompt.** Declare `"unit": "m"` on
   every length descriptor and emit it into the description *generated*, so it
   cannot drift from the code. Then emit `minimum`/`maximum` from a declared
   plausible band, which a constrained decoder enforces before a token is
   spent.
3. **Warn on the mm/m confusion by name.** Bounds cannot be tight — a 2 m sheet
   is a real part — so the honest form is a warning that names the suspicion:
   `"0.1 m = 100 mm; a 100 m box is unusual — did you mean millimetres?"` A
   value that is *impossible* (zero, negative) is a `BAD_ARG` refusal instead.
4. **Make required-ness honest.** 30 of 142 is not a description of this API. An
   argument the operation cannot do sensible work without is `required`; a
   default is only legitimate when it is the answer a caller would have chosen.
   `add_revolve {}` building a body is not a default, it is an invention, and
   the model is told it succeeded.
5. **Describe the other 71 parameters.** `tests/test_registries.py` already
   fails when a feature type is half-registered; the same shape of test should
   fail when a parameter has no description or a length has no unit.

**How you know it worked.** A1's number goes up, and its error histogram
changes shape — fewer `BAD_ARG`, fewer silent successes that score zero against
the spec. The unit guard is testable directly: `add_primitive` with a 100 m box
warns, and the warning names millimetres.

**Traps.**

- **A warning nobody reads is not a fix.** `execute` returns `warnings` and the
  copilot forwards them to the planner; check that path end to end, or the guard
  is decoration.
- **Do not "fix" units by accepting millimetres.** Two accepted units is two
  ways for a document to mean two things, and the kernel is metres throughout
  (`cad_kernel` docstring). State the unit and refuse the ambiguity.
- **Tightening `required` is a breaking change to a recorded corpus.** Existing
  trajectories contain calls that omit arguments now becoming mandatory. Decide
  whether they are re-emitted or dropped *before* running it, not after.
- The tools payload is 35 KB against a 12.7 KB prose list. That is more prompt,
  not less — worth it if A1 says so, and only then. Measure the token cost as
  part of the same run.

**Size.** Medium-large. Tool calling and the units work are each a couple of
days; the descriptions and the required-ness audit are 58 operations of careful
reading.

---

## Sprint A3 — Close the loop

**Why now.** With a number (A1) and a surface worth driving (A2), the remaining
accuracy is in the loop itself and in what the loop leaves behind.

Today the copilot plans, dispatches and stops. It never checks its own work
against the spec — `verify_against_spec` exists, returns the fraction of
requirements met, and is not part of the loop. It gets a typed error code back
and is told, in prose, to "fix the arguments or try a different approach". And
the trajectory it writes is exported with the **same name-only catalog** as the
system prompt (`trajectories._catalog_text`), so the corpus teaches a model to
guess argument names from a list — exactly the surface A2 replaces.

**What to do.**

1. **Verify before declaring done.** When the document has a spec, run it and
   feed the failing requirements back rather than accepting `{"done": true}`.
   The score is the reward function; a loop that never reads it is optimising
   nothing.
2. **Attach the remedy to the error, not to the prose.** Each of the eight codes
   has a canonical next move — `MISSING_ARG` → the argument's schema,
   `NO_SUCH_FEATURE` → `list_features`, `BAD_ARG` on a reference →
   `list_edges`, `PRECONDITION` → a different *previous* step. `fillet_edges`
   already does this well ("no such edge(s): e999. Call list_edges for the 12
   available on this body") and it is the exception; make it the rule and
   measure the repair rate.
3. **Export what the model will actually see.** One generator for the system
   prompt, the tools payload and the training corpus, so inference and training
   cannot describe different action spaces. Include the *failures*: a corpus of
   only successes never shows a model what recovering from a `PRECONDITION`
   looks like, and recovery is most of what an agent does.
4. **Add `by_error_code` to the corpus report** alongside `by_cause` and
   `by_feature`, and gate the scripted subset of A1 in pytest so the numbers
   are defended rather than merely recorded.

**How you know it worked.** Mean spec score and the fraction of tasks scoring
1.0 both move on A1's set; the fraction of runs that end by *saying* done rather
than exhausting the budget goes up; and a task that fails on the first attempt
is recovered within the budget more often than not.

**Traps.**

- **Self-verification can spin.** A loop that re-checks after every edit will
  spend its whole budget checking. Check on the *transition to done*, and once
  more at most, then stop and report the unmet requirements honestly rather than
  looping to the cap.
- **Recording every retry teaches retrying.** The trajectory corpus is training
  data; a corpus in which the median task takes eleven calls because eight were
  repairs teaches an eleven-call habit. Keep the failures — they are the
  valuable part — but keep the *outcome* on the record too, so a training run
  can weight them.
- **A stale reward is worse than none.** `verify_against_spec` meshes the body;
  after P2 that is cheap, but the score still belongs to a specific geometry.
  Score the body you are about to call done on, not one from three edits ago.
- Do not raise `MAX_STEPS` to make the number go up. A budget increase that
  improves the score tells you the loop is inefficient, not that it is better.

**Size.** Large. The verification loop and the unified generator are each
substantial; the error remedies are mechanical.

---

## Suggested order

**A1 → A2 → A3**, and here the dependency is not tidy at all — it is the whole
point. A2 and A3 are both plausible-sounding changes to a system that currently
cannot tell you whether a plausible-sounding change helped. Native tool calling
triples the prompt; self-verification spends steps; both could make things
worse, and without A1 nobody would know.

A2 before A3 because the loop's failures are dominated by the surface it is
given. There is no point teaching a model to recover from `BAD_ARG` when the
reason it emits `BAD_ARG` is that it was handed a list of parameter names with
no types and no units.

**Deferred, and worth saying so:** fine-tuning against the corpus, RL on the
spec score, asking the user a clarifying question mid-goal, and multi-part
assemblies. All of them want A1's number first — a fine-tune with no eval is
indistinguishable from a regression.

**Explicitly rejected:** an LLM judge, anywhere in the scoring path. Everything
this system measures, it measures on the body. A judge would be the one
component that cannot be checked, in the one place where being wrong is
invisible.
