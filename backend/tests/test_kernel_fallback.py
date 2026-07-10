import sys
sys.path.insert(0, '/home/joed/cad/backend')

from app.services.mesh_generator import _kernel_candidates
from app.models.schemas import DesignSpec, DesignSpace, Vector3, LoadCase, Constraint
from app.routers.simulate import _compute_simulation_result
from app.routers.manufacturing import _compute_manufacturing_result

spec = DesignSpec(
    name="Fallback Test",
    design_space=DesignSpace(size=Vector3(x=2, y=1, z=1), center=Vector3(x=0, y=0, z=0)),
    preserve_regions=[
        {"id": "p1", "name": "A", "type": "sphere", "center": Vector3(x=-0.7, y=0, z=0), "radius": 0.25},
        {"id": "p2", "name": "B", "type": "sphere", "center": Vector3(x=0.7, y=0, z=0), "radius": 0.25},
    ],
    obstacle_regions=[],
    load_cases=[LoadCase(id="l1", name="Load", point=Vector3(x=0, y=0.5, z=0), force=Vector3(x=0, y=-1000, z=0), load_type="static")],
    constraints=[Constraint(id="c1", name="Fix", type="fixed", point=Vector3(x=-0.7, y=0, z=0))],
    material_id="al-7075",
    objective={"target": "minimize_mass", "weight": 1},
)

candidates = _kernel_candidates(spec, n=2)
print("candidates", len(candidates))
for c in candidates:
    print(c.id, c.name, c.mass, c.score, "cad", c.cad is not None)
    sim = _compute_simulation_result(c.id)
    print(" sim", sim)
    mfg = _compute_manufacturing_result(c.id, build_axis="z")
    print(" mfg", mfg)
