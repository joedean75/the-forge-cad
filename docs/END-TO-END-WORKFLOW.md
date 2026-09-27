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

The experiment worth running next is the one that separates the two failures, because
"it cannot build a vehicle" was two claims: it cannot *plan* an assembly, and it
cannot *design the parts*. So the assembly was scaffolded by hand — one
`build_part` call per printed part, with a standalone dimensioned goal and a
`requirements` list — and only the part design left to the model.

It designs the parts. Of the parts that got a planner that answered, **every one came
back dimensionally exact**: a 380 x 90 x 4 chassis plate to 0.0 mm, an 88 x 45 x 40
bulkhead, a 95 x 38 x 10 arm, a 30 x 22 x 40 knuckle, all watertight solids with
consistent winding. It also wrote its own brief first — the bulkhead's `set_spec`
recorded three `bounding_box` requirements and a `feature` requirement without being
asked for them specifically, and scored 1.0 against it.

What it gets wrong is *inside* the envelope, and `finished` is the field that says so:

* `motor_mount` returned `ok` with `finished=False` after one step — a 45 x 40 x 30
  box with **no 36 mm bore**. The right outside, none of the inside.
* `knuckle` returned `ok` with `finished=False` after six — the right block with
  **five** bores where one was asked for.

Both are the shape of failure to expect from here: the primitive is reliable and the
feature on it is not. Neither is silent, which is the part that matters — a run that
stops short reports `finished=False` and a caller that reads only `ok` will place a
box and call it a motor mount.

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
