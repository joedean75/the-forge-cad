"""G-code post-processor — convert a generated program to a machine's dialect.

The Forge emits neutral RepRap-flavor G-code. Real controllers differ: a GRBL
laser/plasma has no heaters and wants a spindle/beam word, Klipper prefers its
own start/end macros, and ISO-6983 controls (press brakes, older CNC) want a
stripped, numbered program. Posting is the last step before a job leaves for a
specific machine.
"""

from __future__ import annotations

import re
from typing import Dict, List

from app.services import machines

# Commands a controller cannot execute, by dialect (absent = keep everything).
_NO_HEATERS = re.compile(r"^\s*M(104|109|140|190|106|107|82|83|84)\b")  # heaters/fan/extruder
_STRIP = {
    "grbl": _NO_HEATERS,
    "iso6983": _NO_HEATERS,
}

_HEADER = {
    "grbl": ["; posted for GRBL", "G21", "G90", "G17"],
    "klipper": ["; posted for Klipper", "START_PRINT"],
    "marlin": ["; posted for Marlin"],
    "iso6983": ["; posted for ISO-6983", "G21", "G90"],
}

_FOOTER = {
    "grbl": ["M5", "G0 X0 Y0", "M30"],
    "klipper": ["END_PRINT"],
    "marlin": ["M84"],
    "iso6983": ["M30"],
}


def post_process(gcode: str, machine_id: str, number_lines: bool = False) -> Dict:
    """Convert neutral G-code to the machine's dialect; returns the posted program."""
    machine = machines.get_machine(machine_id)
    dialect = machine.dialect
    strip = _STRIP.get(dialect)

    body: List[str] = []
    removed = 0
    for line in gcode.splitlines():
        if strip and strip.match(line):
            removed += 1
            continue
        body.append(line)

    out: List[str] = [f"; {machine.vendor} {machine.name} · {dialect}"]
    out += _HEADER.get(dialect, [])
    out += body
    out += _FOOTER.get(dialect, [])

    if number_lines or dialect == "iso6983":
        out = [f"N{(i + 1) * 10} {ln}" if ln and not ln.startswith(";") else ln
               for i, ln in enumerate(out)]

    posted = "\n".join(out) + "\n"
    return {
        "machine": {"id": machine.id, "name": machine.name, "dialect": dialect},
        "gcode": posted,
        "lines": len(out),
        "stripped": removed,
        "bytes": len(posted),
    }
