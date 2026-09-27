# Driving this system end to end

Written from doing it: a fine-tuned 2B building parts and assemblies through the
operation catalog, with the numbers each practice came from. Everything here was
learned by something going wrong first, which is why it is worth writing down —
none of it is inferable from the code.

## The order that works

1. **Write the brief first.** `set_spec` before the first edit, because
   `verify_against_spec` *is* the reward function and `_unmet_requirements` is the
   gate that reads it. Both are inert when nobody wrote a spec, and before
   `BRIEF_RULE` existed "done" meant only that the body was a valid solid.
2. **Purchased parts are inserted, never modelled.** `insert_standard_part` for
   screws, nuts, bearings, motors, servos, standoffs, plate, tube and LiPo packs.
   Modelling a screw is slower and less accurate than the catalog that has one,
   and eight screws inserted from one key are one BOM line of qty 8.
3. **Printed parts are designed one at a time.** In an assembly, `build_part` runs
   a whole part copilot on a fresh document and inserts the result. The goal it
   takes has to stand alone — the part run cannot see the assembly, so "the base
   plate" builds nothing useful and the operation says so.
4. **Mate as you go, not at the end.** A component arrives at the origin, which is
   where everything else built that way already is. Until a mate holds it, it is
   not assembled, it is overlapping.
5. **Check with the thing that measures, not the thing that renders.**
   `check_interference` reads `interferences` and never `clear` — `clear` is false
   for a near miss too, and mating two parts is precisely what puts their faces in
   contact.

## Running a long build

* **In-process, not over HTTP.** `POST /copilot` is bounded by the client's
  20-minute polling deadline; `copilot.run(subject_id, goal, subject="assembly")`
  is not. An assembly defaults to 48 steps, caps at 120, and gets 18 minutes,
  because one part is a whole nested run.
* **Pin the resolution.** `with rollout.rollout(resolution=96):` — `capped_resolution`
  clamps to the account's plan, so the same document evaluates differently for
  different accounts, which destroys the one property a measurement needs.
* **Use a throwaway database.** `DATABASE_URL=sqlite:////tmp/…` per run. A build
  writes documents and trajectories; pointing it at the real store means finding
  them later.

## Measuring, and the four ways a measurement lies

* **Verify a task is satisfiable before trusting a score.** Build the part the task
  describes and check it scores 1.0. Two of twelve goal tasks were unsatisfiable
  when written — a cone asking for `radius` where the feature reads `base_radius`,
  and a sheet bracket pinning the axis the bend allowance pushes out — and both
  looked exactly like a capable model failing.
* **On a seeded task, check the seed *fails*.** A task whose starting body already
  meets the spec measures nothing.
* **Never average a task that did not run.** Five of eleven Opus tasks met
  `HTTP 402: would exceed your available credits`, took zero steps, and the suite
  reported `mean score 0.818` — the unrun tasks contributing whatever their seed
  scored. A benchmark that averages in what it could not attempt reads highest
  exactly when the model is least available.
* **Record what produced the number beside it.** Model, prompt profile, tool-calling
  mode, resolution. Three baselines were compared as though they shared their
  conditions and the artifacts could not say.

## Two suites, because they answer different questions

`goals` (18 tasks) starts from three datum planes and builds. `edits` (11 tasks)
starts from a **seeded body** and asks for a change, which is what the tool is
actually used for: the template gallery only shows while a document has no solid,
and after that the prompt bar edits the part on screen. Three of the edit tasks can
only be done by reading a feature's parameters and passing the same structure back
— `set_feature_params` had 223 labels in the corpus and no task that used one.

## What the fine-tune can and cannot drive (2026-09-27)

Measured, not estimated. On **parts** the 2B is at or above the frontier model:
0.918 against Opus 5's 0.900 over eighteen goal tasks, and 0.909 over eleven edit
tasks. On **assemblies** it does not work at all.

A full brief — a 1/10 short-course truck to Traxxas Slash dimensions, every
structural part printed and every electrical part inserted from the catalog —
ended after **8 steps and one successful operation**:

```
[1] build_part   FAIL MISSING_ARG: 'goal' is required
[2] build_part   FAIL BAD_ARG: unknown arguments 'params', 'shape'
[3] build_part   ok
[4] add_mate     FAIL MISSING_ARG: 'type' is required
[5] add_mate     FAIL MISSING_ARG: 'base' is required
[6] add_mate     FAIL MISSING_ARG: 'base' is required
[7] add_mate     FAIL MISSING_ARG: 'base' is required
[8] stopped after three identical failed actions
```

The corpus says why before the run does: `add_mate` has **2** labels,
`insert_standard_part` **2**, `list_components` **3**, and `build_part` **none**.
Step 2 is the tell — it reached for `params` and `shape`, which are
`add_primitive`'s arguments, because building a part is nearly all it has seen.
And the one `build_part` that succeeded took the whole brief as one part's goal
and produced a single 8.76 kg body called "short-course RC truck", against a
2.64 kg target for the entire vehicle.

**Everything the surface was supposed to do, it did.** Every failure was a typed
`MISSING_ARG` naming the argument, the repeat guard ended the run at three
identical failures rather than spending 120 steps, and the summary said "Goal not
confirmed complete. Ran 1 operation(s)" instead of reporting success. The loop is
not what needs fixing.

So the next corpus is an assembly corpus, and it is as specific as the last two
were: sequences that insert a purchased part, place it, mate it by connector, and
check interference. Nothing in `coverage` does any of that today.

### The same brief with the scaffolding done for it

"It cannot build a vehicle" was two claims — it cannot *plan* an assembly, and it
cannot *design the parts* — so the second experiment separated them. The assembly was
scaffolded by direct operations: one `build_part` call per printed part with a
standalone dimensioned goal and a `requirements` list, then placement, the purchased
parts and the roll-ups. Only part design was left to the model. **17 of 17 parts
built.**

On the *envelope* it is essentially perfect. Sixteen of seventeen parts came back
dimensionally exact — `err=0.0` on all three axes, at 128 voxels, across plates,
blocks, a cylinder and a 500 mm shell — and all seventeen are valid watertight solids
with consistent winding. The one miss is a battery tray built 12 mm thick against 8 mm
asked. Measured with `verify_against_spec`'s own machinery, not by eye.

On what goes *inside* the envelope it is about a third right. Judging by volume against
closed form, combined with the feature count where the feature is too small for volume
to discriminate:

| outcome | count | parts |
|---|---|---|
| feature cut correctly | 6 | chassis (4 × ⌀6), both arms (2 × ⌀4), rear tower, shock (⌀10 × 60 bore, 7,294 mm³ against 7,295 predicted), body shell (2 mm wall, 553,152 mm³ against 553,152) |
| feature cut wrong | 3 | front bulkhead (bore present, wrong size), knuckle (**5** bores where 1 was asked), hub carrier (over-cut) |
| envelope only, nothing cut | 8 | rear bulkhead, motor mount, front tower, servo tray, battery tray, both bumpers, wheel |

The body shell is worth pausing on: a 500 × 280 × 90 box hollowed to a 2 mm wall,
matching the closed form exactly. That is the hardest thing on the list and it is
right.

### `finished=True` is only as honest as the spec behind it

This is the finding that matters, and it corrects what a first look at the data
suggested. Two of the incomplete parts (motor mount, knuckle) reported
`finished=False`, so the failure looked self-reporting. It is not: **the wheel and both
bumpers reported `finished=True` with nothing cut at all** — a bare cylinder called a
wheel, and two blocks called bumpers.

The chain is mechanical. `_unmet_requirements` runs `verify_against_spec` on the claim
of done, and `verify_against_spec` has nothing to say about a document with no spec. One
part in seventeen wrote one. So for sixteen of them the done gate checked that the body
was a valid solid — which it was — and nothing else. **A missing brief does not degrade
the reward function, it removes it**, and the visible symptom is a confident
`finished=True` on a part that is missing its defining feature.

So the 2.1% `set_spec` rate below is not a cosmetic coverage gap. It is the reason
`finished` cannot be trusted as a completeness signal today, and fixing the corpus is
what makes the gate real rather than decorative.

### The brief rule is only followed when the corpus teaches it

Of the first six parts, **one** wrote a spec. The other five answer
`verify_against_spec` with `PRECONDITION: this document has no design spec`, which
means the reward function and the done gate were both inert for five parts out of six
— the run was checked for a valid solid and nothing else. The one that did comply
(`bulkhead_front`) wrote three `bounding_box` requirements and a `feature`
requirement unprompted and scored 1.0, so the capability is there.

The corpus predicts the rate. Counted over the 5,477 merged v6 labels:

| label | count | share |
|---|---|---|
| `add_primitive` | 1,493 | 27% |
| finishing turn (`{"done": true}`) | 1,118 | 20% |
| `add_extrude` | 920 | 17% |
| `add_hole` | 900 | 16% |
| `set_feature_params` | 243 | 4.4% |
| **`set_spec`** | **116** | **2.1%** |
| `set_material` | 22 | 0.4% |
| `add_mate` / `insert_standard_part` | 2 / 2 | 0.04% |
| `build_part` | 0 | — |

`set_spec` is meant to be the *first* call of every part run and is 2.1% of labels;
one part in six is about what 2.1% buys. Finishing, at 20%, is reliable — which is the
same relationship the other way round and the reason to trust the number. Count the
label before blaming the prompt: the rule is in the prompt for every run, and the
prompt is not what decides whether it is followed.

One brief of mine was wrong in a way nothing caught, and it is worth naming because it
is an agent-driving lesson rather than a model one: I specified a 95 mm suspension arm
and a 240 mm track independently, and a 95 mm arm does not fit between a 90 mm chassis
and a knuckle at that track. **Nothing in the pipeline checks one part's dimensions
against another's** — each `build_part` run sees its own goal and nothing else. That
is what `build_part`'s `component_ids` argument is for, and not using it is the
mistake; a per-part spec cannot catch a relationship between two parts.

### The roll-ups caught every consequence, including mine

* **Mass found the envelope-only failures without being asked.** All-up came to
  11,006 g against a 2,640 g target. Not a units bug: four wheels left as solid
  cylinders are 2.6 kg by themselves, and the un-bored bulkhead, mount and trays carry
  the rest. The parts that were *dimensionally* perfect and internally empty are
  exactly the parts that made the budget 4.2x over, which is what a mass budget is for.
* **Interference found 31 real clashes, and every one was mine.** 3,741 pairs, and the
  split is the whole story: 3,554 hardware-on-hardware and 156 hardware-on-part are the
  96 fasteners and bearings I left stacked at the origin, because a screw in this
  assembly is a BOM row and not a placement. The remaining **31 part-on-part** are
  genuine, and each names the dimension to change — `steering knuckle ∩ wheel` at
  −26.83 mm is the track arithmetic I got wrong, `motor mount ∩ rear bulkhead` at
  −17.50 mm is two parts I placed into the same 40 mm of chassis. The model's parts were
  right and my layout was wrong, and the check said so.
* **There is no way to scope the check to a subset**, so realistic fastener counts
  drown it. Reading it required bucketing `pairs` by hand. The counts reconcile exactly
  against the operation's own total, which is how you know the bucketing is honest.
* **No mates, deliberately.** The four bounding-box mates align one world axis between
  two extremes, so a `fasten` would have moved the front bulkhead to the middle of the
  chassis top face rather than the front axle line — and `place_component` then refuses
  to correct it, because a mate owns the placement. Rotation is impossible for the same
  structural reason. One mate that visibly misplaces a part is worse than none and an
  honest note.
* **A per-part spec cannot catch a relationship between two parts.** I specified a
  95 mm arm and a 240 mm track independently, and a 95 mm arm does not fit between a
  90 mm chassis and a knuckle at that track. Nothing checked it, because each
  `build_part` run sees only its own goal — which is what `component_ids` is for, and
  not using it was the mistake.
* **A volume check only discriminates where the feature is big enough.** Two ⌀4 holes
  are 0.4% of a shock tower, inside any sane tolerance, so volume-against-closed-form
  silently passed parts with no holes at all. Volume decides the bores and pockets;
  the feature count decides the small holes. Either alone gets a third of the table
  wrong.

## Corpus practices

* **One generator for the prompt and the corpus.** Every sample is built by
  `copilot.planner_request`, the same pure function a live turn calls, so the corpus
  cannot describe an API that does not exist.
* **Thread the prompt profile everywhere.** A corpus built `--profile finetuned`
  got `full` samples for the imported source alone, because `samples_from_file`
  never took a profile: tool schemas and a `tool_calls` label against the short
  prompt everything else used. It also made every sample untrainable — a median
  5,490 tokens against a 3,072 cap, 3,092 of them the operation directory that
  profile does not send.
* **Measure the cap with the tokenizer that will read it.** Dividing characters by
  four is fair on prose and far too generous on argument JSON: it predicted 92%
  of samples surviving where the run reported 67%.
* **A cap is a sample count, not a saving.** The cap falls on the longest samples,
  and the longest sample is the one with the most steps behind it — which is
  exactly a recovery or a finishing turn. Trimming 406 tokens of inapplicable prose
  moved the surviving *finishing* turns from 32% to 54%.
* **Absolute exposure decides whether a model knows an operation exists; share does
  not.** `coverage` fell from 16.9% of the corpus to 8.7% and every operation it
  exists for held at 1.00, because every sample is still seen once in an epoch. I
  predicted the opposite twice.
* **Drop each scratch document as soon as its samples exist.** The ingest kept them
  and grew about a gigabyte a minute, dying at `MemoryMax` with no traceback.

## Infrastructure, all of it learned the hard way

* **`nohup … &` does not survive.** It ignores SIGHUP, not SIGTERM, so a long job
  dies with its caller's process group. Use the harness-tracked background mode.
* **A waiter that greps for a process name matches its own command line.**
  `pgrep -f "datasets.build"` never notices the death it is waiting for; the bracket
  form `datasets[.]build` does.
* **Capture the exit code.** Three builds died silently and I diagnosed it wrong
  twice — an OOM, then a signal — before `exit=139` said
  `Segmentation fault (core dumped)`. A crash inside OpenCascade takes the
  interpreter with it, leaves no traceback, and counts no cause.
* **Isolate untrusted geometry in a subprocess.** Nothing times out inside a C++
  call, so a hang and a segfault are the same problem: run chunks in children and
  bisect a chunk that dies down to the row responsible. One program held a build
  for 68 minutes at 283% CPU.
* **Do not pipe a long job's output to `tail`.** It buffers until the pipe closes,
  so a running job looks silent and a dead one looks the same. Redirect to a file.
* **The 6 GB card is the whole story, and it presents as three different bugs.**
  Qwen3.5-2B in bf16 nearly fills a 3050's VRAM. As a prompt grows the allocation
  does too, and on Windows the driver *spills VRAM to host RAM* rather than
  failing — so the first symptom is **slowness**: per-step latency went 19 s ->
  81 s -> 300 s, and the process's host working set grew to 8.5 GB and then
  11.7 GB, which is the spilled VRAM and not a leak. The second symptom is
  **timeouts**, once a turn drifts past whatever the client allows. The third is
  `RuntimeError: CUDA error: out of memory`, after which the context is dead and
  every later request fails identically. One cause, three faces; diagnosing any of
  them on its own sends you somewhere useless. Measure VRAM headroom first.
* **`RuntimeError: CUDA error: out of memory` is not `torch.cuda.OutOfMemoryError`.**
  The second is PyTorch's allocator declining a request, and the next shorter
  prompt succeeds. The first is a failed kernel launch reported by the driver, and
  it **poisons the CUDA context** — nothing in-process recovers it. `serve.py`
  caught only the recoverable one, so the fatal one fell to the generic handler and
  the server answered 500 for every subsequent request while `/v1/models` kept
  returning 200. Eight parts died against a server that looked healthy. It is now
  matched on the message (there is no distinct type to catch), latched, and
  answered as a 503 naming the restart.
* **`/v1/models` answering is not a health check.** It touches no CUDA. A probe that
  does not run the model cannot tell you the model is dead, and this is what made
  the first diagnosis wrong twice.
* **A hardcoded client timeout is indistinguishable from a model that cannot answer.**
  The planner turn was fixed at 60 s with two attempts, so a local 2B that needs 80 s
  reported `planner_error: provider connection timed out` and `Ran 0 operation(s)` —
  reading exactly like a model failure, on a server that was answering. Ten of
  seventeen parts were lost to it across two passes. `LLM_TIMEOUT` now overrides it,
  because the right value is a property of the hardware and not of the code.
* **Relaunching that server is not the command line it is running.** The Windows Store
  `python.exe` alias in the recorded command line re-execs an interpreter with no
  `transformers`; the one that works is the `.venv-qwen` virtualenv. `Win32_Process`
  reports the alias, so the command line a process shows is not always one that starts
  it again.
* **Read the envelope's `ok` before its `result`.** A probe of `list_standard_parts`
  passing `query` (the parameter is `search`) printed an empty catalog, and I spent a
  step believing the catalog was empty. The API was right — a typed `BAD_ARG` naming
  every valid parameter — and the harness threw it away by reading `result` through
  `.get`. A script that does that converts every typed refusal into a plausible empty
  answer, which is the exact failure the envelope exists to prevent.
* **A "stale" log may be a timezone.** Windows here runs UTC−4; a watcher log that
  looked four hours dead was nine minutes old.
