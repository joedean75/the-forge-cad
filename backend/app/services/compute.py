"""Where the numerics run, and the one sparse solve that runs there.

Two large symmetric-positive-definite systems exist in this codebase and they
are the same system: SIMP assembles `KU = F` every iteration, and `fea.solve`
assembles it once. SIMP already had a Jacobi-preconditioned CG written against
a `torch` device, so it used a GPU when there was one. `fea` had a scipy CG that
could not, which meant the analysis a user actually waits for was the half that
stayed on the CPU.

This is that solver, extracted, plus the device decision — made in one place so
the two cannot disagree about it and an operator can override it.

**On CUDA and what is verified.** The device is a `torch.device`; the CUDA path
is the CPU path with a different one, which is the entire reason it is written
this way rather than with a second CUDA-specific implementation. Everything here
is exercised on CPU by the test suite. **No GPU was available where this was
written, so the CUDA branch is unverified by execution** — the honest claim is
that it is device-agnostic torch, not that it has been measured on a GPU.
`FORGE_DEVICE` exists partly so that is checkable: set it to `cuda` on a machine
with one and the same tests run there.
"""

from __future__ import annotations

import logging
import os
from typing import Optional

import numpy as np

_LOGGER = logging.getLogger(__name__)

#: Free DOFs above which a direct factorisation is a bad idea. A sparse LU fills
#: in badly in 3D — a 98k-DOF beam ran five minutes and 5 GB before being killed
#: — where CG is seconds and O(n) memory. Below it the direct solve is exact and
#: faster, so the threshold is a real fork rather than a tuning knob.
ITERATIVE_ABOVE_DOF = 20_000


def torch_available() -> bool:
    try:
        import torch  # noqa: F401
    except Exception:
        return False
    return True


def device(prefer_gpu: bool = True):
    """The torch device to compute on.

    `FORGE_DEVICE` overrides (`cpu` / `cuda` / `cuda:1` / …), which is how a
    deployment pins it and how the GPU path gets exercised deliberately rather
    than only when a machine happens to have one. Without the override this is
    CUDA when it is both wanted and present, and CPU otherwise — never a failure,
    because every caller here works on either.
    """
    import torch

    override = os.environ.get("FORGE_DEVICE", "").strip().lower()
    if override:
        try:
            return torch.device(override)
        except Exception:
            _LOGGER.warning("FORGE_DEVICE=%r is not a torch device; using CPU", override)
            return torch.device("cpu")
    if prefer_gpu and torch.cuda.is_available():
        return torch.device("cuda")
    return torch.device("cpu")


def describe_device(dev=None) -> str:
    """A short name for the report. A number should say where it was computed."""
    import torch

    dev = dev if dev is not None else device()
    if dev.type == "cuda" and torch.cuda.is_available():
        try:
            return f"cuda:{torch.cuda.get_device_name(dev)}"
        except Exception:
            return "cuda"
    return dev.type


def pcg_solve(k_csr, f, dev=None, *, tol: float = 1e-8,
              max_iter: Optional[int] = None) -> np.ndarray:
    """Jacobi-preconditioned conjugate gradients for `KU = F` on `dev`.

    `K` must be SPD — the stiffness matrix with fixed DOFs already removed. The
    whole iteration stays on the device; only the answer comes back, so a GPU
    run does not pay a transfer per iteration.

    Raises `RuntimeError` if it does not converge, rather than returning a
    plausible-looking vector: an unconverged displacement field feeds straight
    into a stress and then into a safety factor, and a wrong safety factor is
    the one output of this system that must never be quietly optimistic.
    """
    import torch

    dev = dev if dev is not None else device()
    n = k_csr.shape[0]
    if max_iter is None:
        max_iter = max(500, 2 * n)

    coo = k_csr.tocoo()
    idx = torch.tensor(np.vstack([coo.row, coo.col]), dtype=torch.long, device=dev)
    val = torch.tensor(coo.data, dtype=torch.float64, device=dev)
    # Invariant checking is opted out of explicitly rather than left to warn on
    # every call: the indices come from a scipy COO matrix, which is already
    # well-formed by construction, and the check is per-element.
    with torch.sparse.check_sparse_tensor_invariants(False):
        a = torch.sparse_coo_tensor(idx, val, (n, n)).coalesce()
    b = torch.tensor(np.asarray(f), dtype=torch.float64, device=dev)

    diag = torch.tensor(k_csr.diagonal().copy(), dtype=torch.float64, device=dev)
    diag = torch.where(diag.abs() < 1e-30, torch.ones_like(diag), diag)
    m_inv = 1.0 / diag

    x = torch.zeros(n, dtype=torch.float64, device=dev)
    r = b.clone()                       # x0 = 0, so r0 = b - A x0 = b
    z = m_inv * r
    p = z.clone()
    rz = torch.dot(r, z)
    b_norm = torch.linalg.norm(b) + 1e-30

    converged = bool(b_norm <= 1e-30)   # a zero load is already solved
    for _ in range(max_iter):
        ap = torch.sparse.mm(a, p.unsqueeze(1)).squeeze(1)
        p_ap = torch.dot(p, ap)
        if p_ap.abs() < 1e-30:
            break
        alpha = rz / p_ap
        x = x + alpha * p
        r = r - alpha * ap
        if torch.linalg.norm(r) / b_norm < tol:
            converged = True
            break
        z = m_inv * r
        rz_new = torch.dot(r, z)
        p = z + (rz_new / rz) * p
        rz = rz_new

    if not converged:
        raise RuntimeError(
            f"conjugate gradients did not converge on {n} degrees of freedom "
            f"in {max_iter} iterations; the model may be under-constrained")
    return x.detach().cpu().numpy()
