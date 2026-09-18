---
name: forge-cad
description: Use when building or editing a part in The Forge — the units contract, what a new document starts as, and why the schema refuses invented arguments
---

# Building a part

Every capability is an **operation** in one catalog. `GET /api/operations` (or the
MCP `list_operations` tool) publishes strict JSON Schema for all of them; run one
with `POST /api/documents/{id}/op` or the `run_operation` tool. Nothing the UI can
do is outside it — if you cannot find an operation for it, it is not there.

## Lengths are metres going in, millimetres coming back

`{"radius": 0.004}` is 4 mm. The observation you read back reports `bbox_size_mm`,
so the same part is `100.0` in a result and `0.1` in the next call. This is the
single most common way an agent builds a part a thousand times too big.

A parameter named `size` **is a length** wherever it appears, which is why the
hole operation takes `screw` ("M4") and the renderer takes `pixels` — a thread
designation and a picture are not distances.

## Write the brief first

`set_spec` records the measurable requirements — `bounding_box` (axis + value or
min/max, mm), `hole` (diameter + count), `mass`, `volume`, `material`,
`watertight`, `bodies`. Then `verify_against_spec` scores the body against it.

Record **only what the request states**. The spec is the score, so a dimension
you invent becomes a target the run then reports as met — a guess is worse than
an absent requirement. A request with nothing measurable in it (a fillet, a
rename) needs no spec.

## A new document is three datum planes and no body

There is nothing to cut yet, so a first-op `subtract` is refused rather than
returning the tool as the part. Build material first (`add_primitive`,
`add_extrude`, `apply_template`), then modify it.

`apply_template` is twelve starting points, each an operation sequence rather
than a canned tree; it refuses a document that already has a body.

## The schema is enforced, and defaults are not inventions

Undeclared arguments are **refused**, not dropped: `add_primitive` with
`{"width": …, "depth": …}` does not quietly build a default cube. Likewise
`add_extrude` with no profile and no sketch is refused, because the alternative
is a 50 × 30 rectangle nobody asked for reported as success.

Every failure carries a code and a remedy. `PRECONDITION` means a different
*earlier* step is needed — build what the operation needs, then call it again;
different arguments will not help.

## Look at it before you claim it is done

`check_geometry` says the body is a well-formed solid. It cannot say the body is
the shape that was requested — a 50 × 30 plate passes every check while the goal
said 120 × 80. `render_view` returns shaded views (URLs, not bytes) so you can
compare what you built with what was asked for. Do that before reporting success.
