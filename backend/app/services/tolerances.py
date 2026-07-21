"""GD&T & tolerances (roadmap feature 02).

The language manufacturers quote and inspect against. Two standards-grounded
calculators plus annotation data the drawing renders:

* **ISO 286 fits** — IT tolerance grades and fundamental deviations from the
  standard formulas (tolerance unit i = 0.45·∛D + 0.001·D µm; IT grades as its
  multiples; shaft/hole fundamental deviations per letter), so ``H7/g6`` on a
  20 mm nominal returns real hole/shaft limits and the clearance range.
* **ISO 2768** general tolerances (fine/medium/coarse/very-coarse) by size band.

Callouts (a fit or ± on a labelled feature) and feature-control-frames
(flatness/position/…) are stored on the document and drawn on the print.
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Tuple

# Nominal size bands (mm), upper bound inclusive, for IT + 2768 lookups.
_BANDS: List[Tuple[float, float]] = [
    (0, 3), (3, 6), (6, 10), (10, 18), (18, 30), (30, 50), (50, 80),
    (80, 120), (120, 180), (180, 250), (250, 315), (315, 400), (400, 500),
]

# IT grade as a multiple of the tolerance unit i (µm).
_IT_MULT = {5: 7, 6: 10, 7: 16, 8: 25, 9: 40, 10: 64, 11: 100, 12: 160, 13: 250}

# ISO 2768 general linear tolerances (± mm) by class and size band start.
# class -> list aligned to _BANDS (0.5–3 .. 400–1000 collapsed to our bands).
_ISO2768 = {
    "f": [0.05, 0.05, 0.06, 0.08, 0.10, 0.10, 0.12, 0.15, 0.20, 0.23, 0.26, 0.29, 0.31],
    "m": [0.10, 0.10, 0.12, 0.15, 0.20, 0.20, 0.30, 0.35, 0.40, 0.46, 0.52, 0.57, 0.63],
    "c": [0.20, 0.30, 0.40, 0.50, 0.60, 0.60, 0.80, 1.00, 1.20, 1.40, 1.60, 1.80, 2.00],
    "v": [0.00, 0.50, 0.70, 1.00, 1.20, 1.20, 1.50, 2.00, 2.50, 3.00, 3.50, 4.00, 4.50],
}


def _band(nominal: float) -> Tuple[float, float]:
    n = abs(nominal)
    for lo, hi in _BANDS:
        if lo < n <= hi or (lo == 0 and n <= hi):
            return lo, hi
    return _BANDS[-1]


def _D(nominal: float) -> float:
    """Geometric mean of the size band (mm), used by the ISO 286 formulas."""
    lo, hi = _band(nominal)
    lo = max(lo, 1.0)  # the first band uses ~1 mm as its representative size
    return math.sqrt(lo * hi)


def it_tolerance_mm(nominal: float, grade: int) -> float:
    """IT tolerance value (mm) for a nominal size and grade (IT5–IT13)."""
    D = _D(nominal)
    i = 0.45 * D ** (1 / 3) + 0.001 * D  # tolerance unit, µm
    mult = _IT_MULT.get(grade)
    if mult is None:
        raise ValueError(f"unsupported IT grade {grade}")
    return round(mult * i / 1000.0, 4)  # µm → mm


def _fundamental_deviation_mm(nominal: float, letter: str) -> float:
    """Fundamental (nearest-to-zero) deviation in mm for a shaft/hole letter.

    Upper-case = hole, lower-case = shaft. Approximates the ISO 286 tables via
    the standard fundamental-deviation formulas for common letters.
    """
    D = _D(nominal)
    up = letter.isupper()
    L = letter.lower()
    # Shaft fundamental deviations (µm); hole is the mirror (H hole lower dev = 0).
    if L == "h":
        dev = 0.0                       # shaft upper dev = 0
    elif L == "g":
        dev = -(2.5 * D ** 0.34)
    elif L == "f":
        dev = -(5.5 * D ** 0.41)
    elif L == "e":
        dev = -(11 * D ** 0.41)
    elif L == "d":
        dev = -(16 * D ** 0.44)
    elif L == "c":
        dev = -(52 * D ** 0.2) if D <= 40 else -(95 + 0.8 * D)
    elif L == "k":
        dev = +(0.6 * D ** (1 / 3))     # shaft lower dev ~ +
    elif L == "n":
        dev = +(5 * D ** 0.34)
    elif L == "p":
        dev = +(5.5 * D ** 0.41 + 1.0)  # ≈ IT7 + 0..
    else:
        raise ValueError(f"unsupported tolerance letter '{letter}'")
    dev_mm = dev / 1000.0
    # For a hole (upper-case) the fundamental deviation is the mirror of the shaft.
    return round(-dev_mm if up else dev_mm, 4)


def limits_mm(nominal: float, spec: str) -> Dict:
    """Upper/lower deviation and limit sizes for a single tolerance spec, e.g. 'H7' or 'g6'."""
    letter = "".join(c for c in spec if c.isalpha())
    grade = int("".join(c for c in spec if c.isdigit()) or "7")
    it = it_tolerance_mm(nominal, grade)
    fd = _fundamental_deviation_mm(nominal, letter)
    if letter.isupper():  # hole: fundamental dev is the lower deviation
        lower, upper = fd, fd + it
    else:                 # shaft: fundamental dev is the upper deviation
        upper, lower = fd, fd - it
    return {
        "spec": spec, "nominal": nominal, "grade": grade,
        "upper_dev": round(upper, 4), "lower_dev": round(lower, 4),
        "max": round(nominal + upper, 4), "min": round(nominal + lower, 4),
        "tolerance": round(it, 4),
    }


def fit(nominal: float, fit_code: str) -> Dict:
    """Analyse a hole/shaft fit like 'H7/g6' at a nominal size.

    Returns both limit sets plus the clearance range and classification.
    """
    if "/" not in fit_code:
        raise ValueError("fit must be 'HOLE/shaft', e.g. 'H7/g6'")
    hole_s, shaft_s = fit_code.split("/", 1)
    hole = limits_mm(nominal, hole_s.strip())
    shaft = limits_mm(nominal, shaft_s.strip())
    max_clear = hole["max"] - shaft["min"]
    min_clear = hole["min"] - shaft["max"]
    if min_clear >= 0:
        kind = "clearance"
    elif max_clear <= 0:
        kind = "interference"
    else:
        kind = "transition"
    return {
        "fit": fit_code, "nominal": nominal, "type": kind,
        "hole": hole, "shaft": shaft,
        "max_clearance": round(max_clear, 4), "min_clearance": round(min_clear, 4),
    }


def general_tolerance_mm(nominal: float, tol_class: str = "m") -> float:
    """ISO 2768 general linear tolerance (± mm) for a nominal size and class."""
    row = _ISO2768.get(tol_class.lower())
    if row is None:
        raise ValueError(f"unknown ISO 2768 class '{tol_class}'")
    lo, hi = _band(nominal)
    idx = next((k for k, (blo, bhi) in enumerate(_BANDS) if (blo, bhi) == (lo, hi)), 0)
    return row[idx]


# GD&T symbols (Unicode) for feature-control frames.
GDT_SYMBOLS = {
    "straightness": "⏤", "flatness": "⏥", "circularity": "○", "cylindricity": "⌭",
    "profile_line": "⌒", "profile_surface": "⌓", "angularity": "∠",
    "perpendicularity": "⊥", "parallelism": "∥", "position": "⌖",
    "concentricity": "◎", "symmetry": "⌯", "runout": "↗", "total_runout": "⌰",
}


def format_fcf(symbol: str, tolerance_mm: float, datums: Optional[List[str]] = None) -> str:
    """Human-readable feature-control frame, e.g. '⌖ 0.1 | A | B'."""
    sym = GDT_SYMBOLS.get(symbol, symbol)
    parts = [f"{sym} {tolerance_mm:g}"]
    parts += list(datums or [])
    return " | ".join(parts)
