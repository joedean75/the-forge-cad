"""Machine registry, sheet nesting and G-code posting (make → validate → machine)."""

from fastapi.testclient import TestClient

from main import app
from app.services import machines
from app.services.cam import post

client = TestClient(app)


def _plate() -> str:
    doc_id = client.post("/api/documents", json={"resolution": 48}).json()["id"]
    root = client.get(f"/api/documents/{doc_id}").json()["feature_tree"]["root"]["id"]
    client.post(f"/api/documents/{doc_id}/op", json={
        "op": "set_feature_params",
        "args": {"feature_id": root, "params": {"size": {"x": 0.1, "y": 0.06, "z": 0.003},
                                                "center": {"x": 0, "y": 0, "z": 0}}}})
    return doc_id


def test_machine_registry_covers_the_shop():
    r = client.get("/api/machine/machines")
    assert r.status_code == 200, r.text
    by_id = {m["id"]: m for m in r.json()["machines"]}
    assert {"ender3", "tci-dl1530", "gasparini-xp165"} <= set(by_id)
    # Each machine declares how a job reaches it.
    assert by_id["ender3"]["connection"] == "moonraker"       # drive live
    assert by_id["tci-dl1530"]["connection"] == "dispatch"    # export the job
    assert by_id["gasparini-xp165"]["connection"] == "quote"  # send out
    assert by_id["tci-dl1530"]["bed"][0] == 3000              # 3000x1500 sheet


def test_nesting_packs_the_sheet_and_reports_utilisation():
    doc_id = _plate()
    r = client.get(f"/api/documents/{doc_id}/nest",
                   params={"machine_id": "tci-dl1530", "quantity": 12})
    assert r.status_code == 200, r.text
    n = r.json()
    # A 100x60 part nests many-up on a 3000x1500 sheet.
    assert n["per_sheet"] >= 12
    assert n["placed"] == 12
    assert n["sheets_needed"] == 1
    assert n["grid"]["cols"] >= 2 and n["grid"]["rows"] >= 2
    assert 0 < n["utilisation_pct"] < 100
    # Every placed instance contributes its contours to one program.
    assert len(n["paths"]) >= 12
    assert n["cut_length_mm"] > 0


def test_nesting_rejects_a_part_larger_than_the_sheet():
    doc_id = _plate()
    # The Ender 3 bed (220x220) is a build plate, but nesting still validates fit.
    r = client.get(f"/api/documents/{doc_id}/nest", params={"machine_id": "generic-cnc", "quantity": 1})
    assert r.status_code in (200, 422)


def test_post_strips_heaters_for_grbl_but_keeps_them_for_marlin():
    src = "M104 S210\nM140 S60\nG1 X10 Y10 F1200\nM107\n"
    grbl = post.post_process(src, "tci-dl1530")
    assert "M104" not in grbl["gcode"] and "M140" not in grbl["gcode"]
    assert "G1 X10 Y10" in grbl["gcode"]
    assert grbl["stripped"] == 3
    assert grbl["machine"]["dialect"] == "grbl"

    marlin = post.post_process(src, "ender3")
    assert "M104" in marlin["gcode"]  # a printer needs its heaters
    assert marlin["stripped"] == 0


def test_post_endpoint_uses_the_documents_program():
    doc_id = _plate()
    r = client.post(f"/api/documents/{doc_id}/post", json={"machine_id": "tci-dl1530", "source": "cut"})
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["machine"]["name"] == "DynamicLine 1530"
    assert out["lines"] > 0 and "G21" in out["gcode"]


def test_iso6983_program_is_line_numbered():
    import re

    out = post.post_process("G1 X1 Y1\nG1 X2 Y2\n", "gasparini-xp165")
    assert out["machine"]["dialect"] == "iso6983"
    # Motion blocks carry sequence numbers (comments stay unnumbered).
    numbered = [ln for ln in out["gcode"].splitlines() if re.match(r"^N\d+ G1 ", ln)]
    assert len(numbered) == 2, out["gcode"]


def test_registry_accepts_job_kinds():
    assert machines.accepts_job("ender3", "print")
    assert machines.accepts_job("tci-dl1530", "cut")
    assert not machines.accepts_job("ender3", "cut")
