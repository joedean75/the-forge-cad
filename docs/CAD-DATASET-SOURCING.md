# CAD dataset sourcing — what can be trained on, and why

The copilot fine-tuning set is built from this system's own work first
(`backend/datasets/`, `python -m datasets.build`). This file is about the public
CAD datasets that could be *added* to it: what each one is, what licence it
carries, whether that licence permits **this** use, and — the question that
decides most of them — whether its format says anything about this API.

Same posture as `PURCHASED-PARTS-SOURCING.md`, and the same two rules, because
they pull in different directions:

* **Copyright obligations trigger on distributing a copy.** Nothing here is
  distributed, so a copyleft or share-alike data licence is a lien rather than a
  bar: usable, and it makes this repository undistributable until the derived
  files come out, which is why every sample records its `license`.
* **Contract terms trigger on use.** A dataset licence that says
  "non-commercial research purposes" binds an internal tool at a commercial
  company exactly as hard as it binds anyone else. Training a copilot that the
  business runs is not non-commercial research, and no amount of "we never
  redistribute it" changes that.

A third rule is this codebase's own: **a sample must be verifiable.** Every
converted sequence is replayed through `cad_operations.execute` and kept only if
it builds a valid solid and matches the source's own geometry where the source
ships any (`datasets/external.verify`). A converter that emitted plausible
operations for a body it never checked would teach a model to drive this API into
shapes it does not make — worse than having no external data at all.

And nothing is fetched by the server. The user downloads a dataset under
whatever terms they accepted, exactly as `supplier_parts` has them fetch a
supplier's CAD themselves.

## The table

| Dataset | What it is | Licence | Usable here | Maps onto the catalog |
|---|---|---|---|---|
| **Zero-To-CAD-1m** (Autodesk AI Lab) | ~1M synthesised CAD programs: CadQuery source, an operation trace, STEP + STL, 8 renders | **Apache-2.0** (HF dataset card) | **Yes** | Partly — `datasets/external.convert_cadquery` handles the subset this catalog expresses exactly; the rest is dropped by cause |
| **FllumaOne-100K** ([arXiv 2606.17696](https://arxiv.org/abs/2606.17696)) | 100k programs for an OpenCascade-based system, kernel-validated feature trees, natural-language descriptions | **CC BY 4.0** (the paper's own licence icon) | Yes, in principle | Unknown — **re-checked, still no release**: the paper links no repository or HuggingFace dataset. The licence is on the *paper* |
| **HistCAD / CADGenBench / WHUCAD / RealCADBench / BenchCAD** (all 2026) | History-based parametric datasets and generation benchmarks | Unread | Unknown | Unassessed — named here so the next look starts from a list rather than a search |
| **SketchGraphs** | 15M Onshape sketches as constraint graphs | Code MIT; sketches are their authors' copyright, licensed via Onshape's Terms of Use §1.g.ii | Yes, on the ToU reading below | **Well** — constrained sketches are what `create_sketch` and the 18 constraint types are for. The best unexploited fit |
| **DeepCAD** | 178k sketch-and-extrude sequences parsed from Onshape public documents via ABC links | Code MIT; **no licence stated for the data files** | Not until the data's terms are stated | Well — sketch + extrude is the core of this catalog |
| **Text2CAD** | Natural-language prompts over DeepCAD's sequences | Inherits DeepCAD's unanswered data question | Same as DeepCAD | Would supply the `goal` field, which is what synthetic sources have to compose |
| **CADFS** | 450k models, 15 operations, FeatureScript | Not stated on the project page | Unknown | Onshape FeatureScript is a different language; a converter is a project of its own |
| **Fusion 360 Gallery** (Autodesk) | 8,625 human design sequences, 8,251 assemblies, sketch/extrude JSON with dimensions and constraints | "only for non-commercial research purposes" | **No** | Would have mapped well, which is what makes it the expensive refusal |
| **ABC** | 1M CAD solids (B-rep) | Per-model terms from Onshape | Not needed | **No** — solids, not sequences. `decompiler` cannot recover operations from an imported `BrepBodyFeature`, so these teach nothing about the API |
| **MIT OpenCourseWare** (2.007, 2.72, 2.008 and the rest of course 2) | Lecture notes, problem sets, slides, some drawings | **CC BY-NC-SA 4.0**, and an explicit AI clause | **No** — see below | **No.** Course materials carry no operation sequences at all; there is nothing for `decompiler.replay` to verify |
| Mesh sets (Thingi10K, ShapeNet, raw meshes) | Triangle soup | Various | — | **No.** A mesh has no operations. Declined here rather than downloaded |

## The two licence readings that decide the table

**Fusion 360 Gallery — declined.** `LICENSE.md`: "You may access, use, reproduce
and modify the Dataset, in each case, only for non-commercial research
purposes," and a modified set may not be made available to others "except for
non-commercial research purposes." It also forbids redistributing the dataset
whole. The restriction is on *use*, so the internal posture does not relax it;
this is the same distinction that makes TraceParts integrable per-user and
forbidden as a seed.

**Onshape public documents — permissive, which is what SketchGraphs rests on.**
Terms of Use §1.g.ii: "Customer grants a worldwide, royalty-free and
non-exclusive license to any End User or third party accessing the Public
Document to use the intellectual property contained in Customer's Public
Document without restriction, including without limitation the rights to use,
copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the
Document." That covers derivatives of sketches from public documents. It does
*not* cure DeepCAD's problem, which is that its released files state no licence
of their own — the fix there is to ask the authors or to re-derive from Onshape
directly, not to assume.

**MIT OpenCourseWare — declined, twice over.** Its FAQ states the NC term in the
same shape Fusion 360's licence does: "Determination of commercial vs.
non-commercial purpose is based on the use, not the user… materials may be used
by individuals, institutions, governments, corporations, or other business
whether for-profit or non-profit so long as the use itself is not a
commercialization of the materials or a use that is directly intended to generate
sales or profit." Training the copilot that designs this organisation's products
is a use directed at the work the organisation sells, so the internal posture does
not relax it — the same reasoning that declined Fusion 360 Gallery. And OCW says
so about this case specifically: "Any commercial application of OCW content
through AI training is strictly prohibited. Additionally, any derivative models,
or AI systems that incorporate OCW Content through training, shall be distributed
under the same CC BY-NC-SA license."

The second clause is the interesting one, because it is share-alike on a *model*
and share-alike is the obligation this codebase's posture does defer: copyleft
attaches to conveying a copy, and an internal tool conveys nothing, which is why
BOLTS and the GPL workbenches may be read. So the SA half would be dormant. **The
NC half is not**, and it is the one that decides.

Worth stating plainly because the request was reasonable: even with a licence,
OCW would contribute **nothing to this corpus**. A sample here is one planner turn
that calls a validated operation and is verified by replaying it; course notes are
prose and PDFs, and there is no sequence in them to replay. What that material
genuinely *could* seed is the eval suite — realistic part requirements, of the
kind `docs/roadmaps/LLM-ACCURACY-ROADMAP.md` now measures against — and a
dimension from a standard mechanical-design problem is a fact rather than an
expression, so writing original tasks informed by one needs no licence from
anybody. That is knowledge, not data, and it is the honest form of the request.

## What is implemented

`datasets/external.py`:

* `samples_from_file(path)` — reads a JSONL the user produced
  (`{"goal", "ops", "source", "license", "expect"?}`), replays each sequence
  into a fresh document inside a recording, verifies it, and turns the verified
  ones into turn samples through the same `copilot.planner_request` path every
  other source uses. Drops are counted by cause.
* `convert_cadquery(source)` — Zero-To-CAD's programs to operations. It parses
  the *program*, because that dataset's `cadquery_ops_json` is a trace of method
  names (`{"op_name": "chamfer", "args": ["p_chamfer"]}`) rather than dimensioned
  operations. The subset is `box`, `cylinder`, `rect`/`circle` + `extrude`,
  `hole`, `cboreHole`, `fillet`, `chamfer`; selectors are skipped and placement
  is not carried across, which is precisely why the geometry check against the
  source's own STEP is not optional. A program using anything else is dropped by
  cause rather than half-converted.
* The goal is composed from the program's named dimensions ("body length 80 mm,
  large dia 60 mm"), which is true by construction. Text2CAD or FllumaOne would
  supply written goals instead, and that is the main reason to revisit them.
* `convert_sketchgraphs(record)` — one exported sketch to a `create_sketch` op.
  The mappings that are substance rather than renaming: `Length` on a line is a
  `distance` between its own endpoints, `Diameter` is a `radius` halved, `Equal`
  is `equal_length` or `equal_radius` depending on what it joins (resolved from
  the referenced entities, never assumed), and `Fix` carries **both** coordinates
  because `fixed` emits one row per axis given. `Mirror`, `Pierce`, the pattern
  and projection types, splines and ellipses are dropped **by cause**, and a
  sketch losing any one constraint is dropped whole — a sketch missing a relation
  solves to a different shape and, unlike a solid, has no volume to catch it.
* `verify_sketch(doc_id)` is why that path is safe. A sketch builds no body, so
  `verify`'s "is this a valid solid" has nothing to answer; the solver's own
  verdict is the stronger check anyway, because a mis-mapped constraint shows up
  as `conflicting`. `under_constrained` is kept: it is the normal state of a real
  user's sketch, and refusing it would drop most of the dataset for being what it
  is. A row says which check applies (`"verify": "sketch"`), rather than the
  loader inferring it from which keys are present.

### The SketchGraphs export contract

Nothing here downloads a dataset, and nothing here parses their binary sequence
format — their library already resolves a `Line`'s parametric form (`pntX`,
`dirX`, `startParam`) into endpoints, so that runs where the library is installed
and this side reads JSONL it can have fixtures for. One object per line:

```json
{"id": "...",
 "entities": [
   {"kind": "line",   "id": "l0", "start": {"x": 0, "y": 0}, "end": {"x": 50, "y": 0}},
   {"kind": "circle", "id": "c0", "center": {"x": 25, "y": 15}, "radius": 6},
   {"kind": "arc",    "id": "a0", "center": {…}, "start": {…}, "end": {…}},
   {"kind": "point",  "id": "p0", "x": 0, "y": 0}],
 "constraints": [
   {"type": "Coincident", "refs": ["l0.end", "l1.start"]},
   {"type": "Length",     "refs": ["l0"], "value": 50},
   {"type": "Diameter",   "refs": ["c0"], "value": 12}]}
```

`refs` name an entity (`"l0"`) or one of its points (`"l0.start"`, `"c0.center"`),
which is how the constraint graph refers to them. Millimetres, matching the
dataset. The export script is a dozen lines over
`sketchgraphs.data.sequence.sketch_from_sequence`, using each entity's
`start_point` / `end_point` properties and `ConstraintType(...).name`.

## Next, in the order the value sits

1. ~~**SketchGraphs → `create_sketch`.**~~ **Converter written and tested**
   (`convert_sketchgraphs`, `verify_sketch`). What remains is an actual download
   and a measured yield: how many of the 15M survive the constraint mapping, and
   which dropped type is worth adding next. Expect `Mirror` to lead — it is the
   one common constraint declined for a missing argument rather than for having no
   equivalent, and carrying the mirror line in the export would fix it.
### Measured, on the real files (2026-09-26)

The test split's 10,000 programs, fetched as **58 MB** by projecting the one
column the converter reads out of a 349 GB repo (`datasets/yield_report.py`):

| | |
|---|---|
| converted | **945 of 10,000 — 9.4%** |
| emitted | `add_extrude` 813, `add_primitive` 150, `add_hole` 35, `add_modifier` **7** |
| first refusal | `center` 28.9%, `translate` 11.3%, `edges` 9.1%, `polyline` 4.6% |
| one call away | `shell` **30 programs**, `pushPoints` 25, `cutBlind` 14 |
| mentioned, unsupported | `union` 58.6%, `center` 56.2%, `cutBlind` 40.2% |

**This contradicts the reason this dataset was made the top priority, so the
reason is withdrawn.** Its card lists `fillet`, `chamfer` and `shell`, and those
are the operations the goal suite found missing — but the programs that *convert*
are overwhelmingly plain extrudes, and only **7 of 945** emitted a fillet or
chamfer at all. That is a selection effect rather than a surprise: a program
simple enough to survive this converter is a program with few operations in it.
Zero-To-CAD will not fill the fillet/chamfer/shell gap that
`corpus.coverage_rows` was written for.

**Widening it one call at a time is also withdrawn.** That was the plan, and the
two-column measurement is what killed it: the best single call to add wins
**30 programs in 10,000**, because `union` appears unsupported in 58.6% of them
and `center` in 56.2% — most programs need several of the missing calls, not one.
Reaching them means a CadQuery-subset interpreter with placement tracking
(`union`, `center`, `cutBlind`, `translate`, `edges`, `pushPoints` and sketch
construction), which is a project rather than an afternoon.

**What it is still worth, and it is a lot.** 9.4% of the 979,633-row train split
is roughly **92,000 verified sequences**, against a v5 corpus of 2,574 — and they
are `add_extrude`, which this corpus has 43 labels of. Sketch-and-extrude depth is
real value even though it is not the value advertised. The limit then stops being
conversion and becomes build time: every sample is replayed and geometry-checked.

2. **Zero-To-CAD — deepen `add_extrude`, do not widen the converter.**
   Pull the train split's program column the same way and ingest what already
   converts: ~92,000 sequences, Apache-2.0. Two things to settle first, and
   neither is the converter. **Verification**: `convert_cadquery` emits no
   `expect`, so `verify` only asks "is this a valid solid" — and this converter
   skips selectors, so a valid solid can be the wrong one. The row carries
   `num_faces`, which is a cheap invariant to compare against the built body, and
   `pip install cadquery` would give the real one by running the source program
   (we already ship OCP, the bulk of that dependency). **Cost**: 92,000 replays
   with a geometry check each, which is where the hours go, so measure one
   thousand before queueing all of them.
3. **FllumaOne**, if it is published: written descriptions plus kernel-validated
   histories is the shape this corpus most lacks.
4. **Ask DeepCAD's authors for a data licence.** One sentence from them would
   open 178k sequences and, through Text2CAD, their written prompts.
