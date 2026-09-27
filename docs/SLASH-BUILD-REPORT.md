# 1/10 short-course truck — v6 build report

Assembly `db72a818-8529-469a-8e52-c9e3439780fd`, model `forge-copilot-qwen3.5-2b` (v6 LoRA on Qwen3.5-2B), prompt profile `finetuned`, resolution pinned 128.

Scaffolding (assembly, placement, purchased parts, mates) driven by direct operations; **every printed part designed by v6** through `build_part`.


## Printed parts: 17 built of 17 asked for

* dimensionally correct (≤0.5 mm on all three axes): **16/17**
* valid solids: **17/17**
* run reported `finished=True`: **11/17**
* wrote a spec: **1/17**


| part | built (mm) | asked (mm) | worst err | bores | steps | finished | spec |
|---|---|---|---|---|---|---|---|
| arm_front | 10 × 38 × 95 | 10 × 38 × 95 | 0.0 | 2 | 3 | True | none |
| arm_rear | 10 × 40 × 100 | 10 × 40 × 100 | 0.0 | 2 | 3 | True | none |
| battery_tray | 12 × 52 × 150 | 8 × 52 × 150 | 4.0 | 0 | 3 | False | none |
| body_shell | 90 × 280 × 500 | 90 × 280 × 500 | 0.0 | 0 | 2 | True | none |
| bulkhead_front | 40 × 45 × 88 | 40 × 45 × 88 | 0.0 | 1 | 3 | True | 4 req, score 1.0 |
| bulkhead_rear | 55 × 60 × 90 | 55 × 60 × 90 | 0.0 | 0 | 1 | False | none |
| bumper_front | 12 × 45 × 130 | 12 × 45 × 130 | 0.0 | 0 | 1 | True | none |
| bumper_rear | 12 × 40 × 110 | 12 × 40 × 110 | 0.0 | 0 | 1 | True | none |
| chassis | 4 × 90 × 380 | 4 × 90 × 380 | 0.0 | 4 | 6 | True | none |
| hub_carrier | 22 × 30 × 38 | 22 × 30 × 38 | 0.0 | 2 | 3 | True | none |
| knuckle | 22 × 30 × 40 | 22 × 30 × 40 | 0.0 | 5 | 6 | False | none |
| motor_mount | 30 × 40 × 45 | 30 × 40 × 45 | 0.0 | 0 | 1 | False | none |
| servo_tray | 6 × 42 × 60 | 6 × 42 × 60 | 0.0 | 0 | 5 | False | none |
| shock | 14 × 14 × 78 | 14 × 14 × 78 | 0.0 | 0 | 2 | True | none |
| tower_front | 5 × 60 × 100 | 5 × 60 × 100 | 0.0 | 0 | 1 | False | none |
| tower_rear | 5 × 55 × 95 | 5 × 55 × 95 | 0.0 | 2 | 3 | True | none |
| wheel | 55 × 110 × 110 | 55 × 110 × 110 | 0.0 | 0 | 1 | True | none |

## Parts that did not build: 0

Eleven were lost on the first pass and eight on the second, all to two serving faults
rather than to the model — a planner timeout hardcoded at 60 s, and a driver-level CUDA
out-of-memory that poisoned the context so the server answered 500 for every later
request while `/v1/models` kept returning 200. Both are fixed (`587c104`); every part
was recovered on the resumed pass, which is what made the run's resumability worth
building.

## What was cut inside the envelope

Judged by volume against closed form, and by feature count where a feature is too small
for volume to discriminate (two ⌀4 holes are 0.4% of a shock tower):

| outcome | count | parts |
|---|---|---|
| cut correctly | 6 | chassis (4 × ⌀6), both arms (2 × ⌀4), rear tower, shock (⌀10 × 60 bore: 7,294 mm³ measured against 7,295 predicted), body shell (2 mm wall: 553,152 against 553,152) |
| cut wrong | 3 | front bulkhead (bore present, wrong size), knuckle (5 bores where 1 was asked), hub carrier (over-cut) |
| nothing cut | 8 | rear bulkhead, motor mount, front tower, servo tray, battery tray, both bumpers, wheel |

**`finished=True` is only as honest as the spec behind it.** The wheel and both bumpers
reported `finished=True` with nothing cut. `_unmet_requirements` runs
`verify_against_spec` on the claim of done, and that has nothing to say about a document
with no spec — so for sixteen of seventeen parts the gate checked only that the body was
a valid solid. A missing brief does not weaken the reward function, it removes it. The
2.1% `set_spec` share of the training corpus is the cause.

## Assembly

* components placed: **128**
* all-up mass: **11,006 g** against a 2,640 g target — 4.2x over, and not a units bug: four wheels left as solid cylinders are 2.6 kg by themselves. The parts that were dimensionally perfect and internally empty are exactly the parts that blew the budget, which is what a mass budget is for.
* centre of mass: [-7.4441, 0.1686, 14.1026] mm
* interferences: **3,741** (clearance warnings 993, skipped 0, approximated 1, clear=False) — split by kind, reconciling exactly against that total:
  * 3,554 hardware ∩ hardware and 156 hardware ∩ part: the 96 fasteners and bearings left at the origin, since a screw here is a BOM row and not a placement
  * **31 part ∩ part — the real ones, and every one is a layout error of mine, not the model's:**
    * steering knuckle ∩ wheel: gap -26.833 mm
    * rear hub carrier ∩ wheel: gap -26.833 mm
    * steering knuckle ∩ wheel: gap -26.833 mm
    * rear hub carrier ∩ wheel: gap -26.833 mm
    * rear bulkhead ∩ rear shock tower: gap -19.5 mm
    * motor mount ∩ rear bulkhead: gap -17.5 mm
    * rear shock tower ∩ 3650 brushless inrunner: gap -16.821 mm
    * front bulkhead ∩ front shock tower: gap -16.0 mm
    * front bulkhead ∩ standard RC servo: gap -14.25 mm
    * motor mount ∩ 3650 brushless inrunner: gap -13.586 mm
    * 3650 brushless inrunner ∩ LiPo pack (length 138 mm, width 46 mm, height 25 mm, mass 320 g): gap -12.088 mm
    * motor mount ∩ rear shock tower: gap -10.333 mm
* mates: **none**. The four bounding-box mates align one world axis between two extremes, so a `fasten` would have put the front bulkhead in the middle of the chassis top face rather than on the front axle line — and `place_component` then refuses to correct it, because a mate owns the placement. Rotation is impossible for the same structural reason. Everything is placed directly.
* degrees of freedom: {"components": [{"component_id": "c292b847-1eac-47da-900f-809e476ed988", "name": "chassis plate", "dof": 6, "constrained_by": [], "over_constrained": false}, {"component_id": "2c99e70c-98a9-41ba-aa22-
* BOM: **29 lines**, total 11006.18 g

| qty | part | mass (g) | basis |
|---|---|---|---|
| 1 | chassis plate |  | geometric |
| 1 | front bulkhead |  | geometric |
| 2 | front lower arm |  | geometric |
| 2 | rear lower arm |  | geometric |
| 2 | steering knuckle |  | geometric |
| 1 | motor mount |  | geometric |
| 1 | rear bulkhead |  | geometric |
| 2 | rear hub carrier |  | geometric |
| 1 | front shock tower |  | geometric |
| 1 | rear shock tower |  | geometric |
| 4 | shock body |  | geometric |
| 1 | servo tray |  | geometric |
| 1 | battery tray |  | geometric |
| 1 | front bumper |  | geometric |
| 1 | rear bumper |  | geometric |
| 4 | wheel |  | geometric |
| 1 | body shell |  | geometric |
| 1 | 3650 brushless inrunner |  | catalog |
| 1 | standard RC servo |  | catalog |
| 1 | LiPo pack (length 138 mm, width 46 mm, height 25 mm, mass 320 g) |  | catalog |
| 1 | 4-in-1 ESC, 30.5 x 30.5 stack (width 36 mm, length 36 mm, height 24 mm, mass 70 g) |  | catalog |
| 1 | flight controller, 20 x 20 stack (width 35 mm, length 25 mm, height 15 mm, mass 12 g) |  | catalog |
| 8 | MR115 miniature ball bearing |  | geometric |
| 4 | MR126 miniature ball bearing |  | geometric |
| 24 | ISO 4762 M3×10 |  | geometric |
| 12 | ISO 4762 M3×16 |  | geometric |
| 16 | ISO 4032 hex nut M3 |  | geometric |
| 24 | ISO 7089 washer M3 |  | geometric |
| 8 | ISO 4762 M2.5×8 |  | geometric |
