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
| **FllumaOne-100K** | 100k programs for an OpenCascade-based system, kernel-validated feature trees, natural-language descriptions | **CC BY 4.0** (per the paper) | Yes, in principle | Unknown — no release location found yet; revisit |
| **SketchGraphs** | 15M Onshape sketches as constraint graphs | Code MIT; sketches are their authors' copyright, licensed via Onshape's Terms of Use §1.g.ii | Yes, on the ToU reading below | **Well** — constrained sketches are what `create_sketch` and the 18 constraint types are for. The best unexploited fit |
| **DeepCAD** | 178k sketch-and-extrude sequences parsed from Onshape public documents via ABC links | Code MIT; **no licence stated for the data files** | Not until the data's terms are stated | Well — sketch + extrude is the core of this catalog |
| **Text2CAD** | Natural-language prompts over DeepCAD's sequences | Inherits DeepCAD's unanswered data question | Same as DeepCAD | Would supply the `goal` field, which is what synthetic sources have to compose |
| **CADFS** | 450k models, 15 operations, FeatureScript | Not stated on the project page | Unknown | Onshape FeatureScript is a different language; a converter is a project of its own |
| **Fusion 360 Gallery** (Autodesk) | 8,625 human design sequences, 8,251 assemblies, sketch/extrude JSON with dimensions and constraints | "only for non-commercial research purposes" | **No** | Would have mapped well, which is what makes it the expensive refusal |
| **ABC** | 1M CAD solids (B-rep) | Per-model terms from Onshape | Not needed | **No** — solids, not sequences. `decompiler` cannot recover operations from an imported `BrepBodyFeature`, so these teach nothing about the API |
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

## Next, in the order the value sits

1. **SketchGraphs → `create_sketch`.** 15M constrained sketches against the one
   part of this catalog that has no synthetic source at all, under a licence that
   reads clearly. A sketch's constraint graph converts to a `sketch` spec, and
   `check_sketch` verifies the result (`well_constrained`, the dof count) without
   needing geometry at all — a stronger check than a volume comparison.
2. **Zero-To-CAD coverage.** Measure the yield on a real download, then widen the
   subset by whichever unsupported call appears most in `DROPS`. `revolve` is
   likely first, and `add_revolve` exists.
3. **FllumaOne**, if it is published: written descriptions plus kernel-validated
   histories is the shape this corpus most lacks.
4. **Ask DeepCAD's authors for a data licence.** One sentence from them would
   open 178k sequences and, through Text2CAD, their written prompts.
