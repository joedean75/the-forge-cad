"""A build is the work of one model, from the plan to the last part.

`LLM_MODEL` is process-wide and `set_model` changes it for every session. A run
that read it on each call could plan an assembly with one model and design its
parts with another — which is what a build left on "default" did, because the
parent handed its nested part runs `None`. The model is now pinned when the
outermost run starts, and every provider call beneath it reads that.
"""

from app.services import copilot, document_service, llm_client, text_to_assembly


class _Resp:
    status_code = 200
    headers: dict = {}
    text = ""

    def json(self):
        return {"choices": [{"message": {"content": "{}"}}]}


def _recording(monkeypatch):
    """Answer every provider call politely and remember which model it named."""
    sent = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            sent.append((kw.get("json") or {}).get("model"))
            return _Resp()

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    monkeypatch.setattr(llm_client, "LLM_API_KEY", "test-key")
    monkeypatch.setattr(llm_client, "LLM_MODEL", "model-a")
    return sent


def _ask():
    llm_client._post_chat([{"role": "user", "content": "hi"}], timeout=5.0)


def test_a_part_built_inside_an_assembly_run_uses_the_assembly_s_model(monkeypatch):
    """The shape `build_part` has: a whole part run nested inside one step of
    the parent. The default is switched between the two, as a second session
    picking another model would."""
    sent = _recording(monkeypatch)
    outer = document_service.create_document(name="outer")
    inner = document_service.create_document(name="inner")

    def inner_planner(goal, transcript, summary):
        _ask()
        return {"done": True, "summary": "part done"}

    def outer_planner(goal, transcript, summary):
        _ask()
        llm_client.set_model("model-b")           # someone else changes the default
        copilot.run(inner.id, "a part", max_steps=1, planner=inner_planner,
                    model=copilot.nested_model(), review=False)
        _ask()
        return {"done": True, "summary": "assembly done"}

    copilot.run(outer.id, "an assembly", max_steps=1, planner=outer_planner, review=False)
    assert sent == ["model-a", "model-a", "model-a"]


def test_an_explicitly_chosen_model_still_wins(monkeypatch):
    sent = _recording(monkeypatch)
    doc = document_service.create_document(name="chosen")

    def planner(goal, transcript, summary):
        _ask()
        return {"done": True, "summary": "ok"}

    copilot.run(doc.id, "g", max_steps=1, planner=planner, model="model-c", review=False)
    assert sent == ["model-c"]


def test_text_to_assembly_plans_and_builds_with_one_model(monkeypatch):
    sent = _recording(monkeypatch)

    def planner(prompt):
        _ask()
        llm_client.set_model("model-b")
        return {"name": "rig", "parts": [{"key": "p", "name": "P", "prompt": "box"}],
                "components": [{"part": "p"}]}

    def build_part(*, name, prompt, resolution, owner_id):
        _ask()
        from app.services import cad_operations
        doc = document_service.create_document(name=name, owner_id=owner_id)
        cad_operations.execute("add_primitive", doc.id, {"shape": "box", "params": {
            "size": {"x": 0.01, "y": 0.01, "z": 0.01}}})
        return document_service.get_document(doc.id)

    text_to_assembly.create_assembly_from_prompt("a rig", planner=planner,
                                                 build_part=build_part, resolution=32)
    assert sent == ["model-a", "model-a"]
