# forge_sdf — native SDF grid sampler (Rust)

Samples the signed-distance field over a grid, which is the hot path of the
numpy evaluator. Bit-for-bit identical meshes; **3–30× faster**, measured on
this machine (4 cores) with the B-rep kernel forced off so the SDF actually runs:

| tree | res 64 | res 128 |
|---|---|---|
| box | 3.1× | 5.7× |
| plate + bore | 5.8× | 8.1× |
| 3-body union | 4.4× | 10.5× |
| 12-body union | 16.5× | 30.4× |

The gain scales with the tree, because numpy evaluates every node over the whole
grid and pays for each one, while the Rust sampler walks the tree once per point
and short-circuits. `tests/test_sdf_native_parity.py` compares mesh hashes
across 15 trees on every run.

## Read this before expecting a speedup

**With OpenCascade installed the common case never reaches this code.**
`Kernel.evaluate_feature` tries B-rep first, and a box, a plate, a bore, a
revolve — anything `brep_from_feature` can build — is built exactly. `sampler_for`
will say `brep`, and the numbers above are irrelevant to it. (A first attempt at
benchmarking this measured brep-against-brep and concluded the Rust was no
faster. It is: the toggle was doing nothing.)

Where it does earn its place:

- **What B-rep declines**, which is the SDF's only path: smooth `FilletFeature` /
  `ChamferFeature` blends *between bodies*, and `ShellFeature`.
- **Every deployment without OCP**, where the SDF is the only kernel there is.

## Coverage

Compiles to native: box, sphere, cylinder, cone, boolean, smooth fillet/chamfer,
shell, hole, pattern, transform, extrude.

Falls back to numpy: `OptimizeFeature` and `ImportFeature` (a voxel grid with no
node on the Rust side), and everything sketch- or OCC-derived — revolve, sweep,
loft, sheet metal, the `Edge*`/`Face*` exact treatments, `BrepBodyFeature`. The
fallback is silent by design and `test_every_declared_shape_actually_compiles`
is what stops it becoming silent *regression*.

## Build

The production image builds this — see the `native` stage in the root
`Dockerfile`. It had not, for as long as the extension existed, which is why a
3–30× fast path had never once run in a deployment.

Locally:

```bash
pip install maturin
cd backend/native/forge_sdf
maturin build --release --out target/wheels
pip install --force-reinstall target/wheels/forge_sdf-*.whl
```

`maturin develop --release` also works inside a venv. Rebuild after editing
`src/lib.rs`; pyo3 is not built with `abi3` here, so a wheel is specific to one
CPython minor version and a mismatch means no fast path and no error.

To extend coverage, add a `Node` variant plus an `eval` arm in `src/lib.rs` and a
matching branch in `sdf_native.compile_feature` — then add the shape to `TREES`
in the parity test, which is what proves the two agree.
