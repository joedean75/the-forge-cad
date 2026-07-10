import sys
sys.path.insert(0, '/home/joed/cad/backend')

from fastapi.testclient import TestClient
from main import app

client = TestClient(app)

spec = {
    "name": "Test Node",
    "design_space": {"size": {"x": 3, "y": 2, "z": 1.5}, "center": {"x": 0, "y": 0, "z": 0}},
    "preserve_regions": [
        {"id": "p1", "name": "Mount A", "type": "sphere", "center": {"x": -1, "y": 0, "z": 0}, "radius": 0.35},
        {"id": "p2", "name": "Mount B", "type": "sphere", "center": {"x": 1, "y": 0, "z": 0}, "radius": 0.35},
    ],
    "obstacle_regions": [],
    "load_cases": [
        {"id": "l1", "name": "Vertical Load", "point": {"x": 0, "y": 0.8, "z": 0}, "force": {"x": 0, "y": -5000, "z": 0}, "load_type": "static"},
    ],
    "constraints": [
        {"id": "c1", "name": "Fixed A", "type": "fixed", "point": {"x": -1, "y": 0, "z": 0}},
        {"id": "c2", "name": "Fixed B", "type": "fixed", "point": {"x": 1, "y": 0, "z": 0}},
    ],
    "material_id": "al-7075",
    "objective": {"target": "minimize_mass", "weight": 1},
}


def test_optimize_and_export():
    # Trigger SIMP; if it fails it will fall back to kernel candidates.
    resp = client.post("/api/optimize", json=spec)
    assert resp.status_code == 200, resp.text
    job = resp.json()
    print("job", job)

    # Poll until done/failed
    import time
    for _ in range(60):
        r = client.get(f"/api/optimize/{job['id']}")
        status = r.json()
        if status["status"] in ("done", "failed"):
            break
        time.sleep(0.5)
    print("final status", status)

    res = client.get(f"/api/optimize/{job['id']}/result")
    assert res.status_code == 200, res.text
    result = res.json()
    print("candidates", len(result["candidates"]))
    cand = result["candidates"][0]
    print("candidate has cad", cand.get("cad") is not None)

    sim = client.post(f"/api/simulate?candidate_id={cand['id']}")
    print("simulate", sim.status_code, sim.json())

    mfg = client.post(f"/api/manufacturing?candidate_id={cand['id']}&build_axis=z")
    print("manufacturing", mfg.status_code, mfg.json())

    step = client.get(f"/api/export/{cand['id']}/step")
    print("step export", step.status_code, len(step.text))


if __name__ == "__main__":
    test_optimize_and_export()
