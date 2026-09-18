---
name: forge-inspect
description: Use when checking a part in The Forge — which verifier answers which question, and where each one refuses to guess
---

# Checking the part

Five different questions, five different operations. Picking the wrong one is how
a part passes a check that was never about the thing that is wrong with it.

| Question | Operation |
|---|---|
| Is it a well-formed solid? | `check_geometry` — watertight, winding, connected bodies |
| Is it the **right** solid? | `verify_against_spec` — scored against `set_spec` |
| Can it be made? | `dfm` — `additive` / `milling` / `molding` / `casting` |
| Will it come off the bed? | `check_printability` — against a *machine*, not a process |
| Will it hold? | `simulate` — real hex-element FEA |
| Did my selections survive? | `check_references` |
| Is it one part or several? | `bodies` |

## Where each one refuses rather than guesses

**`verify_against_spec` needs a spec** and refuses a document with no evaluated
geometry. An unmeasurable requirement **fails loudly** rather than being skipped:
a spec scoring 1.0 because half of it was not checked is the worst possible
reading, since this score is the reward function.

**`simulate` says which solver ran.** Check `solver` and `estimate_only` in the
result — a closed-form estimate arrives in the same shape as a real solve, and
only that field tells them apart. The safety factor comes from peak element
stress, not the 99th percentile, so it is conservative by construction.

**`dfm` gives no quote for the non-additive processes**, because there is no cost
model for them and an invented number is a signal you would optimise against.

**Without the exact kernel (OCP), the wall-thickness and draft checks fail
loudly** rather than passing. "Pass" for a question nobody could answer is the
one failure mode a manufacturability report must not have.

**`mass_properties` on a sheet reports area, not a zero volume.** A surface has
no enclosed volume, and that is not a measurement of zero — the volume-derived
keys are absent rather than 0.0, which would read as "no material assigned".

## After an edit that re-topologises

Cutting a slot across a filleted face turns one face into two. `check_references`
reports `matched` / `split` / `deleted` / `unresolved` per selection — a reference
that cannot be matched is reported, never snapped to whatever was nearest. If it
says `deleted`, reselect: the fillet you re-apply blindly will land somewhere
plausible and wrong.
