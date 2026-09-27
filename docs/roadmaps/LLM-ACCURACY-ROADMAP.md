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
> **A 2B fine-tune matches Opus 5 on the score** (2026-09-25, Qwen3.5-2B,
> rank-16 LoRA, 2,800 samples in 8.25 h on a 6 GB laptop 3050, baseline in
> `backend/data/eval-goals-2b-20260925-0134.json`): **solved 4/6, mean score
> 0.900, geometry 6/6, finished 4/6, mean 3.33 steps, 7 failed calls.** Against
> Opus 5's 5/6 and 0.900, and the same corpus's 0.6B at 0/6 and 0.533. It also
> **solved `plate-4-holes`**, which both the 0.6B and Opus 5 had failed — so that
> task was neither impossible nor mis-specified.
>
> **A second suite, and editing works** (2026-09-27, `evals.tasks.EDITS`, eleven
> tasks that start from a seeded body and ask for a change). **v6: mean 0.909,
> solved 8/11, finished 8/11, 3.18 steps, 10 failed calls all `PRECONDITION`.**
>
> The three tasks that can only be done by reading a feature's parameters and
> passing the same structure back — lengthen, thicken, enlarge-hole — all pass, in
> one or two steps. That is the first measurement of `set_feature_params` working
> at all: 223 labels in the corpus and, until this suite, no task that used one.
> `edit-material` and `edit-hollow` pass too, so `set_material` and `shell_faces`
> are chosen on an existing body and not only on a fresh one.
>
> **The Opus comparison is incomplete and the reason is recorded rather than
> smoothed.** Five of its eleven tasks met `HTTP 402: would exceed your available
> credits` and ran zero steps. On the six both models reached, they **tie at 1.00**
> — and Opus needs **1.0 steps against v6's 1.67**. The efficiency gap the goal
> suite showed (2.33 against 4.39) is the same gap, and it is the honest remaining
> difference: not what the local model can do, but how many turns it spends doing
> it.
>
> The three v6 losses are all *additive edits to an existing body*: `edit-add-boss`
> 0.60 with an invalid solid, `edit-add-rib` 0.60 having spent all eight steps, and
> `edit-remove-boss` 0.80 never deleting the feature. Ten `PRECONDITION` failures
> across the suite say the same thing: it is reaching for operations whose
> preconditions it has not established. Adding material *onto* something that is
> already there, and removing a feature by id, are the two shapes the corpus has
> almost nothing of — `delete_feature` has no labels at all.
>
> **v6 passes Opus 5: 0.918** (2026-09-27, v5's corpus plus 2,408 Zero-To-CAD
> samples, 3,377 trained, cosine): **solved 12/18, mean score 0.918, geometry
> 18/18, finished 12/18, mean 4.39 steps, 5 failed calls.** Opus 5 is 0.900 on the
> same suite. A 2B on a 6 GB laptop card now scores above the frontier model on the
> reward function this system measures parts by.
>
> The number that says *why* is the failed calls: **20 -> 5, and `BAD_ARG` 13 -> 1.**
> Malformed arguments have essentially stopped, which is what 814 `add_hole` labels
> and 846 `add_extrude` labels buy — those were 4 and 43 in v5. `shelled-tray`
> 0.80 -> 1.00 means `shell_faces` is finally chosen, `bushing` 0.80 -> 1.00 and
> `stepped-shaft` 0.40 -> 0.80.
>
> **The dilution worry did not materialise.** `coverage` fell from 16.9% of the
> corpus to 8.7% and every one of its operations held: steel, aluminium, titanium,
> filleted, chamfered and rib all stayed at 1.00. Absolute exposure is what decides
> whether a model knows an operation exists — every coverage sample is still seen
> once in an epoch — and the share it occupies is not.
>
> **Efficiency is where Opus is still ahead**: 2.33 steps against 4.39, and 1 failed
> call against 5. The remaining four losses are two kinds. Three are *counting* —
> `plate-4-holes`, `bolt-circle` and `counterbored-plate` each drilled **one** hole
> where four or six were asked, which is worse than v5's three-of-four even though
> `add_hole` grew 200-fold: Zero-To-CAD's programs mostly drill one hole per
> operation, and an array of them converts to one `add_hole` each rather than to a
> pattern, so the corpus now demonstrates single holes emphatically. And
> `sheet-bracket` 0.75 -> 0.67 never reaches `add_sheet_metal`, which has 6 labels.
>
> That makes the next corpus pass as specific as the last two: multi-instance
> sequences, and the `pushPoints`/`rarray`/`polarArray` conversions routed to a
> `PatternFeature` rather than to N holes.
>
> **The corpus fix worked: 0.764 -> 0.879** (2026-09-26, v5 corpus, Qwen3.5-2B,
> one epoch with cosine decay): **solved 10/18, mean score 0.879, geometry 18/18,
> finished 10/18, 20 failed calls.** Opus 5 is 0.900 on the same suite, so the gap
> is 0.021.
>
> Every one of the five operations that had **zero labels** is now used: steel
> 0.00 -> 1.00, aluminium 0.80 -> 1.00, titanium 0.60 -> 1.00 (`set_material`),
> filleted 0.80 -> 1.00 (`fillet_edges`), chamfered 0.80 -> 1.00
> (`chamfer_edges`), rib 0.80 -> 1.00 (`add_rib`). Two-cubes 0.50 -> 1.00 and
> sheet-bracket 0.33 -> 0.75 came along with them. That is the prediction the
> 18-task suite made — "7 of 18 tasks, needing no new capability" — coming back as
> the measurement.
>
> **Three tasks went backwards and the reason is worth more than the gain.**
> `plate-4-holes` 1.00 -> 0.80 (three holes where four were asked), `bolt-circle`
> four where six were asked, `counterbored-plate` two where four were asked,
> `bushing` lost its bore. The failure mode has *changed*: the model now reaches
> for the right operation and miscounts the repetitions, where before it never
> reached at all. And `stepped-shaft` 0.80 -> 0.40 is a plain units error — 24,000
> mm, which is 24 written where 0.024 was meant, the mistake
> `adv:metres-for-millimetres` exists for.
>
> Both are countable corpus properties rather than mysteries: the coverage source
> teaches one feature per sequence, so nothing in it demonstrates "four of these,
> not three", and its dimensions are all metres-correct literals with no failure
> to recover from. The next corpus pass writes multi-instance sequences and keeps
> the recovery injection pointed at unit mistakes.
>
> **Eighteen tasks, and the failures are one sentence** (2026-09-25, step 2,800
> serving): **mean score 0.767, solved 6/18, geometry valid 17/18, finished 5/18,
> 25 failed calls.** The original six scored 0.900 within this run — identical to
> their own baseline — so the widening is consistent rather than a new ruler; it
> adds twelve signals to a number one task used to move by 0.167.
>
> What it says, which six tasks could not: **the model uses the five operations
> it was trained on and almost nothing else.** Four tasks fail *only* their
> feature requirement, with the right bounding box built — 0 × EdgeChamferFeature,
> 0 × FaceShellFeature, 0 × RibFeature, 0 × EdgeFilletFeature — and all three
> material tasks fail with the material unset. Those five operations
> (`chamfer_edges`, `shell_faces`, `add_rib`, `fillet_edges`, `set_material`) have
> **zero labels between them** in a corpus where `add_primitive`,
> `set_feature_params`, `finish_task`, `add_modifier` and `set_parameters` are 97%
> of 6,173. The remaining losses are the not-stopping pattern again: 11 holes
> where 6 were asked for, 1 counterbore where 4 were.
>
> So "cover the catalog in the labels" stops being a guess. It is 7 of 18 tasks,
> worth up to 0.14 of the headline, and it needs no new capability — every one of
> those operations works and is tested.
>
> **The suite found three holes in the surface on its way in**, all the same
> family and all previously `ok: true`: a `params` key belonging to a *different*
> shape (`radius` on a cone — declared, because the schema lists the union, then
> silently defaulted to 30 mm while honouring the height), a 3-vector missing a
> component (`size` with no `y` built a 0.0 mm body; a modifier's vector raised a
> bare `KeyError` that reached the caller as `evaluation failed: 'y'`), and an
> extent given as zero (`exclusiveMinimum: 0` is *published* on every length and
> the runtime validator enforces only `enum` and `type`, so a constrained decoder
> could not send it and the prose fine-tune could). The adversarial suite is 9 of
> 9 caught. The `cone` task scoring 1.00 in this run is the fix working: the model
> tried `radius`, was told the shape reads `base_radius`, and corrected.
>
> **Finishing the epoch made it worse, and the loss curve said otherwise**
> (2026-09-25). The run above stopped at 2,800 of 4,155 samples; resumed to the
> end, validation loss fell 0.025 -> 0.0094 and the suite went **0.900 -> 0.867,
> solved 4 -> 3, finished 4 -> 2, mean steps 3.33 -> 5.33**. Every task took more
> steps, and both regressions are the same failure: `plate-4-holes` cut **8 holes
> where 4 were required**, and `boss-on-plate` spent 7 steps without reaching the
> boss. What degraded is knowing when to stop — while the metric being watched
> improved. Train loss at the end was 3.8e-05, which is the tell: with a constant
> learning rate and no decay the last third of the epoch was memorising.
>
> **The checkpoint is reproducible; the ranking between checkpoints is not.** The
> planner runs at temperature 0.2, so the suite is not deterministic and a 0.033
> gap on six tasks could be noise. Re-running step 2,800 independently gave
> **0.900, solved 4, finished 4, 3.17 steps, identical error codes** — so
> within-checkpoint variance is small and that regression is real. But per-task
> outcomes move between checkpoints in a way the aggregate hides:
>
> | task | s2800 A | s2800 B | s3400 | s4155 |
> |---|---|---|---|---|
> | plate | 1.00 | 1.00 | 1.00 | 1.00 |
> | plate-4-holes | 1.00 | 1.00 | 1.00 | 0.80 |
> | boss-on-plate | 1.00 | 1.00 | **0.00** | 0.80 |
> | bushing | 1.00 | 1.00 | 1.00 | 1.00 |
> | steel-plate | 0.60 | 0.60 | **1.00** | 0.60 |
> | filleted-block | 0.80 | 0.80 | 0.80 | **1.00** |
> | **mean** | 0.900 | 0.900 | 0.800 | 0.867 |
>
> `steel-plate` is met only at 3,400 and `filleted-block` only at 4,155 — the two
> requirements with **zero labels in the corpus** are the two that flip, which
> says those operations are reachable but unreliable rather than absent. And
> `boss-on-plate` at 3,400 is not a low score but an *invalid solid*, a worse
> failure than any number here.
>
> So the honest conclusion is narrower than "2,800 is the best checkpoint": it is
> the best of the three measured and the only one measured twice, and **six tasks
> cannot rank checkpoints** — each task is one Bernoulli draw, and picking a
> checkpoint on this suite is tuning on noise. That is A1 reasserting itself: the
> next investment is more tasks, not more training, and until there are more the
> suite's job is telling 0.533 from 0.900, not 0.900 from 0.867.
>
> **What is shared and what is not**: the same six tasks, the same reward
> function (`verify_against_spec` on the built body) and the same pinned
> resolution 96 — but each model runs the prompt it is built for, Opus on `full`
> with tool schemas and the fine-tunes on `finetuned` prose with no schemas at
> all. That is the right comparison and not a like-for-like prompt, and the
> baselines could not previously say which was which: `model`,
> `prompt_profile` and `tool_calling` are now recorded in the artifact beside
> `resolution`, because three numbers were being compared on conditions that
> lived only in whoever ran them.
>
> **Level on the score is not level on reliability.** Opus reached it in 2.33
> steps with 1 failed call; the 2B took 3.33 with 7 (`BAD_ARG` 5,
> `MISSING_ARG` 2). The mean score is the reward function, and on that they tie;
> the failed-call count is what a user would feel.
>
> Two things made the difference and only one of them is the parameter count. The
> corpus was rebuilt with the trimmed `finetuned` prompt (406 tokens out), which
> at the 3,072-token cap this card imposes took the surviving set from 48% to 67%
> — and, the part that matters, the finishing turns from 32% to 54% and the
> recovery turns from 31% to 40%. The 0.6B run's defining failure was that no run
> ever called `finish_task`; it had been trained on a set where two thirds of the
> finishing turns did not fit.
>
> **Both remaining misses are corpus gaps, and they are countable.** `steel-plate`
> scored 0.60 on two unmet requirements that are really one missed call — the
> material is unset, so the mass came out 67.5 g against a required 191.75–201.75
> — and `set_material` appears **0 times in the 6,173 labels**. `filleted-block`
> scored 0.80 on `0 × EdgeFilletFeature`; `fillet_edges` is also 0, and the only
> route in the corpus is `add_modifier` (645). A model will not call an operation
> it has never seen chosen, however clearly the prompt lists its name — which
> makes "cover the catalog in the labels" a more specific next step than more
> data. The distribution is five ops doing 97% of the work: `add_primitive`
> 2,533, `set_feature_params` 1,466, `finish_task` 995, `add_modifier` 645,
> `set_parameters` 402.
>
> **The previous run, for the record** — against a local fine-tune of Qwen3
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
