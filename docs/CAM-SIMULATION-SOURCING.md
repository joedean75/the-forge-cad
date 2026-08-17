# Newton for CAM simulation — investigated, and declined

**Verdict: do not integrate Newton.** It is a rigid- and soft-body physics
engine for robotics. Material removal is not a dynamics problem, and Newton has
no concept of it. The adjacent piece that *is* relevant — NVIDIA Warp, the
compute layer underneath Newton — is a real option for something else, and is
covered at the end.

Written the same way as
[`PURCHASED-PARTS-SOURCING.md`](PURCHASED-PARTS-SOURCING.md): the checks that
were actually made, so the next person does not repeat them.

---

## What Newton is

Open-source (Apache-2.0), GPU-accelerated, built on **NVIDIA Warp** and
**OpenUSD**, developed by NVIDIA with Google DeepMind and Disney Research, and
now under the Linux Foundation. It ships several rigid-body solvers (MuJoCo
Warp, Kamino), a Vertex Block Descent solver for deformables — cables, cloth,
volumetric materials — a signed-distance-field collision library, and
hydroelastic contact modelling. It integrates as a physics backend for Isaac Sim
and Isaac Lab.

Requirements: Python 3.10+, and for the fast path an **NVIDIA GPU (Maxwell or
newer) with driver 545+ and CUDA 12**. Linux and Windows on x86-64/aarch64;
macOS is CPU-only.

## Why it does not do CAM simulation

CAM simulation is two different problems that get filed under one name.

**1. Material removal — "does this toolpath cut the right part?"** This is a
*geometry* problem: subtract the swept tool volume from the stock and compare
the remainder against the target body. The literature settles on voxel or dexel
representations precisely because it is a boolean-subtraction problem that
parallelises well, not because it involves forces. Newton's documentation has
no mention of material removal, subtractive manufacturing, or CNC. Its
vocabulary is rigid bodies, soft bodies, cloth, cables and contact — nothing
that subtracts material from a workpiece.

**2. Machine motion and collision — "does the spindle housing hit the
fixture?"** This *is* rigid-body kinematics and collision, and it is the one
place Newton would genuinely earn its keep. But we have 2.5-axis CAM
(`services/cam/toolpath.py`): roughing and finishing on stacked Z levels. There
is no articulated machine model, no tool holder, no fixture, no rotary axes —
so there is no kinematic chain for a physics engine to collide. Building the
machine model is the work; Newton would be the easy part afterwards.

And chasing that is explicitly Tier 3 in
[`COMPETITIVE-GAPS.md`](COMPETITIVE-GAPS.md): 5-axis CAM is where Fusion has a
fifteen-year head start and shops already own a seat.

## It also fails the optional-dependency rule

Every optional piece here degrades to something that still works: without OCP
the SDF kernel runs, without an LLM key text-to-CAD refuses with a typed error,
without Stripe plan changes apply locally. Newton's fast path needs an NVIDIA
GPU with CUDA 12. The deployment this ships to is Render's standard plan, which
is CPU-only and where `FORGE_DEVICE` is pinned to `cpu` for exactly that reason.
A dependency whose entire value is GPU throughput, on a host with no GPU, is a
fourth way for a deployment to be half-configured.

## What we would actually build instead

The gap is not simulation, it is **verification** — and the machinery is
already here. `cam/toolpath.py` is SDF-native: it samples the body's signed
distance field to find where a flat endmill's centre can sit without gouging.
The same field verifies the path it produced:

1. Stock is the part's bounding box plus the margin — a box SDF.
2. Each roughing segment removes everything within a tool radius of its line
   and above its Z level; each finishing contour the same. Their union is the
   swept cut volume.
3. `result = stock − cut`, sampled on a grid.
4. Two numbers fall out, and they are the two a machinist asks for:
   - **Gouge** — volume inside the target part that the cut removed. Any
     non-zero value is a scrapped part.
   - **Leftover** — volume outside the target that the cut left behind. Some is
     expected on a 2.5-axis path (undercuts are unreachable); a lot means the
     stepdown or stepover is wrong.

That is a *verifier*, which is the shape everything else in this action space
takes — `verify_against_spec`, `check_geometry`, `check_drawing`, `dfm` — and it
needs no new dependency at all. It is also the thing an agent can optimise
against, which a physics engine's contact forces are not.

## The adjacent thing that is worth revisiting: Warp, not Newton

Warp is the layer Newton is built on, and on its own merits it maps onto this
codebase unusually well:

- It JITs Python kernels to GPU **and has a CPU software fallback with
  identical semantics**, so the same kernel runs either way. That is the exact
  contract `evaluator/sdf_native` already has with the Rust `forge_sdf` wheel.
- `wp.Texture3D` gives hardware-interpolated volume sampling — what `GridSDF`
  does by hand.
- `wp.MarchingCubes.extract_surface_marching_cubes()` is what `geom/meshing`
  calls scikit-image for.

So the honest framing is: Warp is a candidate to *replace* the Rust extension,
not to add CAM. Two things would have to hold first, and neither is free:

- **Bit-identical meshes.** `tests/test_sdf_native_parity.py` compares mesh
  hashes across 15 trees because two evaluators for one field is a correctness
  hazard before it is a speedup — a document that meshes one way with the wheel
  and another without breaks the property the whole content-addressed mesh
  cache rests on. A third sampler needs the same guarantee, against both
  existing ones.
- **A reason.** The Rust sampler is already 3–30× on the trees it handles, and
  with OCP installed the common case never reaches either of them — B-rep is
  tried first. The population Warp would accelerate is what B-rep declines:
  smooth `FilletFeature`/`ChamferFeature` blends and `ShellFeature`. That is a
  real set, and it is a small one.

Worth a benchmark before a line of integration code — and, as the README for
`forge_sdf` records, that benchmark must force `brep.AVAILABLE = False` or it
measures B-rep against itself and concludes nothing.

---

## Sources

- [newton-physics/newton on GitHub](https://github.com/newton-physics/newton) — license, GPU/CUDA requirements, platform support
- [Newton Physics, NVIDIA Developer](https://developer.nvidia.com/newton-physics)
- [Into the Omniverse: Open-Source Physics Engine and OpenUSD Advance Robot Learning](https://blogs.nvidia.com/blog/newton-physics-engine-openusd/) — solvers, SDF collision, hydroelastic contact
- [Linux Foundation Welcomes Newton](https://www.linuxjournal.com/content/linux-foundation-welcomes-newton-next-open-physics-engine-robotics) — governance
- [NVIDIA Warp](https://developer.nvidia.com/warp-python) and [warp CHANGELOG](https://github.com/NVIDIA/warp/blob/main/CHANGELOG.md) — `wp.Texture3D`, marching cubes, CPU fallback semantics
- [Real-time machine simulation using cutting force calculation based on a voxel material removal model](https://link.springer.com/article/10.1007/s00170-019-04418-2) — why material removal is voxel/dexel work
