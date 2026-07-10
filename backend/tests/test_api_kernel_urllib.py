import sys
import time
import json
from urllib.request import urlopen, Request
from urllib.error import HTTPError

spec = {
    "name": "Test Node",
    "design_space": {"size": {"x": 3, "y": 2, "z": 1.5}, "center": {"x": 0, "y": 0, "z": 0}},
    "preserve_regions": [
        {"id": "p1", "name": "A", "type": "sphere", "center": {"x": -1, "y": 0, "z": 0}, "radius": 0.35},
        {"id": "p2", "name": "B", "type": "sphere", "center": {"x": 1, "y": 0, "z": 0}, "radius": 0.35},
    ],
    "obstacle_regions": [],
    "load_cases": [
        {"id": "l1", "name": "Load", "point": {"x": 0, "y": 0.8, "z": 0}, "force": {"x": 0, "y": -5000, "z": 0}, "load_type": "static"},
    ],
    "constraints": [
        {"id": "c1", "name": "FixA", "type": "fixed", "point": {"x": -1, "y": 0, "z": 0}},
        {"id": "c2", "name": "FixB", "type": "fixed", "point": {"x": 1, "y": 0, "z": 0}},
    ],
    "material_id": "al-7075",
    "objective": {"target": "minimize_mass", "weight": 1},
}

BASE = "http://127.0.0.1:8001"


def post(path, body):
    data = json.dumps(body).encode()
    req = Request(f"{BASE}{path}", data=data, headers={"Content-Type": "application/json"}, method="POST")
    with urlopen(req) as r:
        return json.loads(r.read().decode())


def get(path):
    with urlopen(f"{BASE}{path}") as r:
        return json.loads(r.read().decode())


def main():
    job = post("/api/optimize", spec)
    print("job", job)
    for _ in range(60):
        time.sleep(0.5)
        status = get(f"/api/optimize/{job['id']}")
        print("status", status["status"], status.get("progress"))
        if status["status"] in ("done", "failed"):
            break

    result = get(f"/api/optimize/{job['id']}/result")
    print("candidates", len(result["candidates"]))
    cand = result["candidates"][0]
    print("cad present", cand.get("cad") is not None)

    sim = post(f"/api/simulate?candidate_id={cand['id']}", {})
    print("simulate", sim)

    mfg = post(f"/api/manufacturing?candidate_id={cand['id']}&build_axis=z", {})
    print("manufacturing", mfg)

    step_text = urlopen(f"{BASE}/api/export/{cand['id']}/step").read().decode()
    print("step length", len(step_text), "has MANIFOLD", "MANIFOLD_SOLID_BREP" in step_text)


if __name__ == "__main__":
    main()
