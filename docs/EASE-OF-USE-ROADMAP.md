# Ease-of-use roadmap — three sprints

> **Status: all three landed** (2026-08-08).
>
> | Sprint | What shipped |
> |---|---|
> | E1 | A face id per triangle in a third FMSH block on the exact path; hover-highlight and click-select in the viewport feeding the same `topo.selected` the list writes; edges resolved against the picked face's boundary; the list kept as the mirror and the only path on an SDF body. |
> | E2 | `ApiError` carrying `code`/`remedy`/`arg`; `FailureNotice` inline where the operation was run, dismissed by the user rather than on a timer; ⌘Z / ⇧⌘Z awaiting `historySyncSettled()`; a `?` sheet. `derived.error` stays a warning. |
> | E3 | Four templates as operation sequences (`services/templates.py`); `EmptyDocumentGuide` in a document with no solid; 210 standard purchased parts generated from the published standards (`services/parts_catalog.py`); the prompt bar edits the open part instead of replacing it. |
>
> Three things came out of E3 that the sprint did not plan and that were
> pre-existing rather than new — each is written up in `CLAUDE.md`:
>
> - **No parameterised document could be decompiled.** The stored tree holds
>   `"=length"` and the emitted `add_primitive` carried it into a numeric field,
>   so every document with design intent in it scored `replay_failed`. Templates
>   would have made that the common case.
> - **A parameter expression crashed the viewport.** `collectSnapPoints` did
>   `q.z.toFixed(4)` on the stored tree. `derived.numeric_tree` is now the tree a
>   client reads as geometry.
> - **The header prompt bar replaced the open part.** It only ever called
>   `createFromPrompt`; `ai_edit` had existed the whole time.
>
> The two deferred items remain deferred: drag a face to resize it, and the
> Y-up/Z-up viewport frame. So does the oldest open finding — no request-body
> size limit and no rate limit on the operation endpoint.

The last five programmes built capability: an exact kernel, a sketch solver,
speed, an action space a model can drive, and the product surface around them.
What none of them went after is the minute-to-minute experience of *operating*
the thing with a mouse and a keyboard.

That gap has a specific shape here, and it is worth naming before the sprints:
**this system is unusually good at being driven by an agent and unusually
awkward to drive by hand.** Every operation is typed, validated, and returns a
failure that names its own remedy — and the browser renders that failure as
`[object Object]`. Every face and edge has a stable, rebuild-surviving id — and
the only way to choose one is to read a sidebar list of coordinates. The
machine-facing half got five sprints of care. The human-facing half inherited
whatever fell out.

Same format as the other roadmaps: what to do, how you know it worked, and the
trap that will actually bite. Every number below was measured against `main` on
2026-08-07 and the measurement is quoted so it can be re-run rather than
believed.

## What is already true

| Landed | Where |
|---|---|
| Stable face/edge ids that survive a rebuild | `cad_kernel/core/naming.py`, `core/history.py` |
| One feature id per triangle, exact on the B-rep path | `services/picking.py`, FMSH owner block |
| Typed failures with a `remedy` on every code | `services/op_errors.py` |
| Local undo/redo, ~0 ms to the restored body | `useDocumentStore` history of `{tree, derived}` |
| Drag-to-move a feature, committed as one operation | `components/MoveHandles.tsx` |
| A command palette | `components/CommandPalette.tsx` (⌘K) |
| One column with a switcher below 1024 px | `hooks/useNarrow`, `EditorScreen.Column` |
| A new document is three datum planes, not a block | `ops/datum_plane.py` |

---

## Sprint E1 — Click the thing you mean

**Why now.** Four operations — `fillet_edges`, `chamfer_edges`, `shell_faces`
and sketch-on-face — take a face or edge id, and there is exactly one way for a
person to produce one: read it out of a list. Measured on a 100×60×12 mm plate
with eight bolt holes, which is the plainest real part this system makes:

```
faces                                    14
edges                                    36
distinct edge labels in the picker        4   ("line 100.0", "line 60.0",
                                               "line 12.0", "circle 15.708")
ways to select a face or edge in 3D       0
```

Thirty-two of thirty-six rows are indistinguishable by their label. The only
thing separating them is a midpoint coordinate triple, which means choosing the
top-left hole's edge is an exercise in arithmetic.

The reason it is a list is real and is documented: `Kernel.from_mesh` produces
one `Face` with `surface=None`, so a marching-cubes body's triangles cannot name
a face, and a nearest-entity guess would be right most of the time — the worst
possible accuracy for a selection. **That reason does not apply on the exact
kernel, and has not since P3.** `brep._face_owners` already walks the shape's
faces at tessellation time and calls `entities.tag_of(face)` to build the
per-triangle feature id. The face is in its hand. What is missing is emitting
the naming id alongside the feature id.

**What to do.**

1. **A face id per triangle**, in a third FMSH block beside the owner block, on
   the exact path only. `Kernel.from_mesh` bodies emit nothing and keep the list.
2. **Hover-highlight and click-select faces in the viewport**, feeding the same
   `topo.selected` state `TopologyPicker` already writes — one selection, two
   input methods, never two selection models.
3. **Edges by way of the face you picked.** A triangle belongs to a face, not to
   an edge, so edge picking is *not* the same problem. Resolve it against the
   boundary of the face under the cursor — a short, exact list (four for a box
   face) — rather than against all 36.
4. **Keep the list, and make it a mirror.** It is the precise fallback, the
   keyboard-accessible path, and the only thing that works on an SDF body.
   Selecting in one must show in the other.

**How you know it worked.** Fillet the four corner edges of a bracket without
reading a single coordinate, and do the same thing on an imported STL — where it
must fall back to the list rather than to a guess.

**Traps.**

- **A wrong face id is worse than a list.** This is the whole reason the picker
  is a list today. Ship it *only* where the answer is exact: no triangle-to-face
  mapping means no 3D picking, and the fallback is the list, never a nearest-hit
  guess.
- **`face_table` sorts its output.** The per-triangle array must carry the
  **descriptor id**, not an index into the listing, or every selection silently
  moves the first time the sort order changes. `naming.faces` and
  `history._explore` share a traversal today; the sort is applied after.
- **The owner block is 4 bytes a triangle and this doubles it.** The mesh is
  314 KB indexed. Measure before and after, and drop the block rather than ship
  a misaligned one — `write_mesh_binary` already refuses an owner list whose
  length does not match the triangles, for exactly this reason.
- **Hover must not cost a round trip.** The ids are in the mesh the browser
  already holds. Anything that fetches on hover will be unusable at 60 Hz.
- **`list_faces` takes an optional `feature`** and a sub-body's face ids are its
  own. A 3D pick is on the *whole* body; a sketch datum wants the feature's. Do
  not conflate them.

**Size.** Medium-large. The kernel change is small and the interaction is where
the work is.

---

## Sprint E2 — Say what went wrong, and what to do about it

**Why now.** The A-series spent a sprint making every failure carry a code and a
remedy. Here is what the browser does with one:

```
what a typed op failure renders as    "API error: 422 [object Object]"
references to `remedy` in frontend/    0
where an error appears                 a `truncate` span in the 32 px status bar
toast / inline error component         none
routers returning a structured detail  9   (all of them mangled)
```

`client.ts:fetchJson` does ``detail = body?.detail ?? detail`` and then
`` `API error: ${res.status} ${detail}` ``. When `detail` is a string that reads
fine, which is why nobody noticed: 42 of the 51 raise sites return a string. The
9 that return `{code, message, arg, remedy}` — which is to say every operation
failure, the ones with the most to say — stringify to `[object Object]`.

The same sprint should close the other half of the hand-driving gap, because it
is the same complaint. Four global shortcuts exist:

```
⌘K   command palette
F    fit the view
Esc  cancel a sketch pick
Del  delete the selected feature

undo / redo   no shortcut at all
```

Undo has been local and effectively instant since P3 — the store restores
`{tree, derived}` together and the browser already holds the mesh. The one thing
it is missing is the key everybody presses.

**What to do.**

1. **Format a structured detail** in `fetchJson`, keeping `code`, `message` and
   `remedy` as fields rather than flattening them into a sentence.
2. **Show the remedy.** It is generated per code and reaches zero pixels today.
3. **Put the failure where it happened.** An error from the fillet button
   belongs next to the fillet button, not in a shared 32 px strip that truncates
   it. One inline surface, used everywhere.
4. **The keyboard layer**: ⌘Z / ⇧⌘Z, single-key tool access, and a `?` sheet that
   lists them — a shortcut nobody can discover is a shortcut nobody has.

**How you know it worked.** Subtract from an empty document and read, without
opening devtools, both what happened and what to do instead. Then undo it with
the keyboard.

**Traps.**

- **A toast that auto-dismisses hides the remedy before it is read.** The
  remedy is the useful half and it is the longer half. Dismissal should be the
  user's.
- **`derived.error` is a warning, not a failure.** Binding a feature to a
  parameter before defining it is a supported workflow, and `execute` reports it
  in `warnings` on an `ok: true` envelope. A UI that renders every non-empty
  error field in red destroys that distinction — which the operation envelope
  was built to make.
- **⌘Z must await `historySyncSettled()`.** The store's write to the server runs
  in the background and only adopts its response if the tree it sent is still
  current. A held-down ⌘Z that skips the await will land an undo before the edit
  it reverses.
- **⌘Z inside a text field is the browser's, not yours.** The existing handlers
  already check `INPUT`/`TEXTAREA`/`SELECT`/`isContentEditable`; the sketch
  canvas and the parameter table are the ones that will be forgotten.
- **Do not invent a second error channel.** `error` in the store is read by the
  status bar today; two sources of truth for "is something wrong" is how a
  cleared error stays on screen.

**Size.** Small-medium. The formatting fix is an afternoon; placing errors well
is the work.

---

## Sprint E3 — Never start from nothing

**Why now.** A new document is now — correctly — three datum planes and no body.
That was the right change and it left a hole this sprint should fill:

```
what a new document shows        three planes, and no guidance of any kind
templates / starter parts        none
standard parts (fasteners, …)    none
the viewport's example prompts   shown only when no document is open at all
```

The one place this app tells a new user what to do is an empty state keyed on
`!doc` — so it disappears the moment they click *New part*, which is the moment
they need it. And the deferred item from the MVP roadmap is now unavoidable
rather than theoretical: M1 made assemblies real, every real assembly needs
screws, and there is no way to get one except modelling it.

**What to do.**

1. **Templates.** A plate, a bracket, an enclosure, a shaft — as *feature trees*
   with driving parameters already named, so the first edit is changing a number
   rather than starting a tree.
2. **Guidance in an empty document**, on the planes rather than instead of them:
   the three things you can do next, from the document you are already in.
3. **A standard-parts library** — fasteners first, to a real standard, inserted
   into an assembly as components rather than modelled.
4. **Make the prompt bar work on an open document**, not only on an empty one.
   `ai_edit` already exists and takes a whole-part instruction.

**How you know it worked.** A user who has never seen the app produces a plate
with four M4 clearance holes and a screw in each, without being told where
anything is.

**Traps.**

- **A template is a feature tree, not a file.** Anything that arrives as an
  `ImportFeature` or a `BrepBodyFeature` is an opaque body: no parameters, no
  editable history, and undecompilable — it would drag `corpus_report` down
  while looking like content.
- **No canned shapes in the parser.** The rule the text-to-CAD parser lives
  under applies here too. A template is chosen from a gallery, deliberately; it
  must never be something a prompt containing "bracket" silently returns.
- **A fastener library is a units and standards problem, not a modelling one.**
  M4×0.7, clearance vs tapped, ISO vs ANSI. Getting the geometry roughly right
  and the designation wrong is worse than not shipping it — the designation is
  what gets ordered.
- **A screw is a *reference*, like every other component.** `add_component`
  refuses a part its owner cannot open. A shared library part must be genuinely
  readable by every account, or every assembly containing one breaks the first
  time somebody else opens it.
- **Guidance that cannot be dismissed is an obstacle.** It has to disappear on
  the first real edit and stay gone.

**Size.** Medium. Templates and the empty state are small; the parts library is
most of it and could be scoped to one standard.

---

## Suggested order

**E1 → E2 → E3**, and unlike the MVP roadmap the dependencies here are real.

E1 first because it is the largest ratio of frustration to work anywhere in the
product, and because the enabling half already shipped: P3's per-triangle
attribution did the hard part and stopped one field short of solving this too.

E2 second because it is cheap and because E1 will generate errors — a 3D pick
that resolves to nothing, a fillet radius that will not build — and they should
land in a surface that can say why.

E3 last because templates are worth more once the editor is pleasant, and
because the fastener library wants E1: choosing which face a screw sits on is
the same selection problem.

**Deferred, and worth saying so:** drag a face to resize it. It is the natural
sequel to `MoveHandles` and it needs E1's face picking to exist first, so it
cannot be in this programme; it is also closer to a modelling feature than to
ease of use. Also deferred: comments and review threads, and multi-select across
the feature tree.

**One thing this roadmap will not fix, and should say so.** The viewport draws
the kernel's Z axis as depth, because three.js is Y-up and the viewport accepts
that. So the datum plane named *Top* appears vertical on screen. The naming is
kernel-correct and consistent with the sketch planes, the drawing views and the
thumbnail renderer; the viewport is the odd one out. Fixing it is a single
change to `useFit.toDisplay` plus every camera position, marker and handle that
assumes the current frame — small in principle, and a visual change to every
screenshot, test and stored camera in the product. It deserves its own sprint
and a decision, not a corner of this one.

**Not in scope, and now the oldest open finding by three roadmaps:** the
operation endpoint has no request-body size limit and no rate limit. Only auth
is throttled.
