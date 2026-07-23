"""Machine registry — the shop's real equipment.

A machine defines *how a job leaves The Forge*: its process (FDM print, fiber
laser, press brake, CNC), build/sheet envelope, the G-code dialect its
controller speaks, and how you dispatch to it — drive it directly (Moonraker),
export a job file, or send it out for a quote.

This drives the whole make→validate→machine tail: the Make step nests and
post-processes to the selected machine's sheet + dialect, Validate simulates the
job on it, and Machine sends it there.
"""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class Machine(BaseModel):
    id: str
    name: str
    vendor: str
    process: str          # fdm | laser | press_brake | cnc
    # Working envelope in mm: (x, y, z). Sheet machines use z=0.
    bed: List[float]
    dialect: str          # marlin | klipper | grbl | iso6983 | trumpf
    # How a job reaches it: 'moonraker' (drive directly), 'dispatch' (export a
    # job package), or 'quote' (send out to a service bureau).
    connection: str
    notes: str = ""


_MACHINES: Dict[str, Machine] = {
    m.id: m for m in [
        Machine(id="ender3", name="Ender 3", vendor="Creality", process="fdm",
                bed=[220, 220, 250], dialect="marlin", connection="moonraker",
                notes="Desktop FDM. Drive live over Moonraker/Klipper."),
        Machine(id="prusa-mk4", name="MK4", vendor="Prusa", process="fdm",
                bed=[250, 210, 220], dialect="marlin", connection="moonraker",
                notes="FDM. Marlin dialect, Moonraker-capable."),
        Machine(id="tci-dl1530", name="DynamicLine 1530", vendor="TCI Cutting", process="laser",
                bed=[3000, 1500, 0], dialect="grbl", connection="dispatch",
                notes="Fiber laser, 3000×1500 mm sheet. Nest parts; export the cut job."),
        Machine(id="gasparini-xp165", name="XP165", vendor="Gasparini", process="press_brake",
                bed=[3000, 0, 165], dialect="iso6983", connection="quote",
                notes="165-ton press brake for sheet-metal bending. Send out for quote/setup."),
        Machine(id="generic-cnc", name="3-axis CNC", vendor="Generic", process="cnc",
                bed=[500, 400, 200], dialect="grbl", connection="dispatch",
                notes="Generic 3-axis mill. GRBL-flavor G-code."),
    ]
}

DEFAULT_MACHINE_ID = "ender3"

# Which processes each machine can consume, for job-compatibility hints.
_PROCESS_JOBS = {
    "fdm": {"print"},
    "laser": {"cut"},
    "cnc": {"mill", "cut"},
    "press_brake": {"bend"},
}


def list_machines() -> List[Machine]:
    return list(_MACHINES.values())


def get_machine(machine_id: Optional[str]) -> Machine:
    if machine_id and machine_id in _MACHINES:
        return _MACHINES[machine_id]
    return _MACHINES[DEFAULT_MACHINE_ID]


def accepts_job(machine_id: str, job_kind: str) -> bool:
    m = get_machine(machine_id)
    return job_kind in _PROCESS_JOBS.get(m.process, set())
