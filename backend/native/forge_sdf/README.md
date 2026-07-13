# forge_sdf — native SDF grid sampler (Rust)

Accelerates the hot path of geometry evaluation: sampling the signed-distance
field over a grid, which was ~97% of evaluation time in pure numpy. Gives a
**~25× speedup** on box/sphere/cylinder + boolean trees (the common case) with
bit-for-bit identical meshes.

The backend uses it automatically via `app/cad_kernel/evaluator/sdf_native.py`
(`Kernel.evaluate_feature`). It is **optional** — if the module isn't built, the
kernel falls back to the numpy SDF evaluator. Features it doesn't yet compile
(fillet/chamfer/shell/hole/pattern/transform/extrude/optimize) also fall back.

## Build

Requires the Rust toolchain (`cargo`) and `maturin` (`pip install maturin`).

```bash
cd backend/native/forge_sdf
../../.venv/bin/maturin develop --release   # builds + installs into the venv
```

Rebuild after editing `src/lib.rs`. To extend coverage, add a `Node` variant +
`eval` arm here and a matching branch in `sdf_native.compile_feature`.
