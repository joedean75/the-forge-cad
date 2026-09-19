"""What a build costs, and the four things that keep it down.

Measured on real runs: input was 98% of the tokens, each planner call re-sent
13–30k of them, and the prefix a provider could cache ended before the
transcript and the reference images. These pin the fixes — the prompt order,
the cache breakpoints, the compacted transcript — and the ledger that makes the
spend visible, because a saving nobody can measure is one nobody keeps.
"""

import json

from app.services import copilot, document_service, llm_client


def _tool_call(name, args):
    return {"content": "", "tool_calls": [{"id": "c", "type": "function", "function": {
        "name": name, "arguments": json.dumps(args)}}]}


def _script(monkeypatch, replies, usage=None):
    """Fake the provider: answer `replies` in order and record every prompt."""
    sent = []

    def fake(messages, timeout=0, max_tokens=None, tools=None, model=None):
        sent.append(messages)
        if usage is not None:
            llm_client._charge((llm_client.CALL_CONTEXT.get() or {}).get("ledger"), usage)
        return replies[min(len(sent) - 1, len(replies) - 1)]

    monkeypatch.setattr(llm_client, "LLM_API_KEY", "k")
    monkeypatch.setattr(llm_client, "_post_chat", fake)
    monkeypatch.setattr(llm_client, "supports_tools", lambda: True)
    return sent


def _text(message):
    content = message["content"]
    if isinstance(content, list):
        return "\n\n".join(p.get("text", "") for p in content if isinstance(p, dict))
    return content


def test_each_turn_extends_the_last_one_s_prompt_up_to_the_state(monkeypatch):
    """The provider bills a repeated prefix at a tenth of the price, but only
    up to the first difference. The state changes every turn, so it goes last;
    everything before it on turn two must be turn one's text, extended."""
    doc = document_service.create_document(name="plate")
    sent = _script(monkeypatch, [
        _tool_call("add_primitive", {"shape": "box", "params": {
            "size": {"x": 0.04, "y": 0.03, "z": 0.005}}}),
        _tool_call("finish_task", {"outcome": "answered", "summary": "stopped"}),
    ])
    copilot.run(doc.id, "a plate", max_steps=2, review=False)
    first, second = _text(sent[0][-1]), _text(sent[1][-1])
    state = "Current document (as it stands now):"
    assert first.index("Steps so far") < first.index(state)
    stable = first[:first.index("Steps so far in this run:")]
    assert second.startswith(stable)
    assert sent[0][0]["content"] == sent[1][0]["content"], "the system prompt is fixed for a run"


def test_reference_images_sit_inside_the_cached_prefix():
    parts = copilot._user_content("Goal: x", ["data:image/png;base64,AA"],
                                  steps="Steps so far in this run:\n[]",
                                  tail="Current document (as it stands now):\n{}")
    kinds = [p["type"] for p in parts]
    assert kinds.index("image_url") < [p.get("text", "") for p in parts].index(
        "Current document (as it stands now):\n{}")


def test_explicit_cache_models_get_breakpoints_and_others_get_plain_text(monkeypatch):
    monkeypatch.setattr(llm_client, "LLM_BASE_URL", "https://openrouter.ai/api/v1")
    messages = [{"role": "system", "content": "rules"},
                {"role": "user", "content": [
                    {"type": "text", "text": "goal"},
                    {"type": "text", "text": "steps", "cache": True},
                    {"type": "text", "text": "state"}]}]

    marked = llm_client._cache_marked(messages, "anthropic/claude-sonnet-5")
    assert marked[0]["content"][0]["cache_control"] == {"type": "ephemeral"}
    user = marked[1]["content"]
    assert [bool(p.get("cache_control")) for p in user] == [False, True, False]
    assert not any("cache" in p for p in user)

    plain = llm_client._cache_marked(messages, "openai/gpt-6-astra")
    assert plain[0]["content"] == "rules"
    assert plain[1]["content"] == "goal\n\nsteps\n\nstate", "OpenAI caches a prefix on its own"


def test_a_catalog_search_keeps_its_rows_and_drops_the_prose():
    result = {"parts": [{"key": "bldc-2212", "designation": "2212 brushless motor",
                         "notes": "x" * 400, "supplier_search": "https://...",
                         "size_mm": {"x": 27.7}}],
              "count": 1, "sources": ["a", "b"], "source": "long text " * 30}
    kept = copilot._transcript_result("search_assembly_parts", result)
    assert kept["parts"] == [{"key": "bldc-2212", "designation": "2212 brushless motor",
                              "size_mm": {"x": 27.7}}]
    assert "sources" not in kept and kept["count"] == 1


def test_a_long_face_listing_is_cut_and_says_how_many_were_left_out():
    result = {"faces": [{"id": f"f{i}", "kind": "plane"} for i in range(60)]}
    kept = copilot._transcript_result("list_faces", result)
    assert len(kept["faces"]) == copilot._MAX_TOPOLOGY_ROWS
    assert kept["faces_omitted"] == 60 - copilot._MAX_TOPOLOGY_ROWS


def test_a_run_reports_what_it_spent_including_its_nested_runs(monkeypatch):
    usage = {"prompt_tokens": 10_000, "cached_tokens": 8_000,
             "completion_tokens": 200, "cost": 0.004}
    outer = document_service.create_document(name="outer")
    inner = document_service.create_document(name="inner")
    _script(monkeypatch, [_tool_call("finish_task", {"outcome": "answered", "summary": "ok"})],
            usage=usage)

    def outer_planner(goal, transcript, summary):
        llm_client._charge(llm_client.CALL_CONTEXT.get()["ledger"], usage)
        copilot.run(inner.id, "a part", max_steps=1, review=False)   # default planner: one call
        return {"done": True, "summary": "done"}

    out = copilot.run(outer.id, "an assembly", max_steps=1, planner=outer_planner, review=False)
    assert out["usage"]["calls"] == 2
    assert out["usage"]["prompt_tokens"] == 20_000 and out["usage"]["cached_tokens"] == 16_000
    assert abs(out["usage"]["cost_usd"] - 0.008) < 1e-9 and out["usage"]["cost_known"]


def test_usage_reads_openrouter_s_cost_and_cache_fields():
    usage = llm_client._usage({"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                               "cost": 0.0012, "prompt_tokens_details": {"cached_tokens": 80}})
    assert usage == {"prompt_tokens": 100, "completion_tokens": 5, "total_tokens": 105,
                     "cached_tokens": 80, "cost": 0.0012}


def test_a_schema_loaded_mid_run_lands_at_the_end_of_the_tool_list(monkeypatch):
    """Tools are the first thing a provider hashes for its cache. A schema
    discovered mid-run inserted in catalog order would sit in the middle and
    invalidate everything after it for the rest of the run."""
    sent = []

    def fake(messages, timeout=0, max_tokens=None, tools=None, model=None):
        sent.append([t["function"]["name"] for t in tools or []])
        if len(sent) == 1:
            return _tool_call("discover_operations", {"names": ["add_thread"]})
        return _tool_call("finish_task", {"outcome": "answered", "summary": "ok"})

    monkeypatch.setattr(llm_client, "LLM_API_KEY", "k")
    monkeypatch.setattr(llm_client, "_post_chat", fake)
    monkeypatch.setattr(llm_client, "supports_tools", lambda: True)
    doc = document_service.create_document(name="threaded")
    # A goal that does not name the operation, or it would be enabled from the
    # start and there would be nothing to discover.
    copilot.run(doc.id, "tidy this up", max_steps=3, review=False)

    from app.services import copilot_context

    control = {t["function"]["name"] for t in copilot_context.control_tools()}
    before = [n for n in sent[0] if n not in control]
    after = [n for n in sent[1] if n not in control]
    assert "add_thread" not in before, "nothing to discover otherwise"
    assert after[-1] == "add_thread", "the new schema goes last, after what was already sent"
    assert after[:len(before)] == before, "the tools already sent keep their places"


def test_a_run_stops_at_the_spending_cap_and_says_so(monkeypatch):
    """A cap bounds the whole build: a nested part run shares the ledger, so it
    cannot be walked past by spending inside sub-runs."""
    monkeypatch.setattr(copilot, "MAX_COST_USD", 0.05)
    doc = document_service.create_document(name="capped")
    _script(monkeypatch, [_tool_call("add_primitive", {"shape": "box", "params": {
        "size": {"x": 0.04, "y": 0.03, "z": 0.005}}})],
        usage={"prompt_tokens": 1000, "completion_tokens": 10, "cost": 0.03})

    out = copilot.run(doc.id, "a plate", max_steps=8, review=False)
    assert out["stop_reason"] == "cost_limit"
    assert out["usage"]["calls"] == 2, "stopped between steps, once the cap was passed"
    assert "spending cap" in json.dumps(out["steps"])
