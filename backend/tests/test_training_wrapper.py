"""The server that lets the copilot talk to a locally trained model.

The copilot only knows how to call a chat-completions URL, so a LoRA `.pth` is
unreachable until something wraps it. What is pinned here is what can be tested
without the weights: that the server renders its prompt with the *trainer's* own
function, and that it answers in the form the caller asked in.
"""

import json

import pytest

from training import copilot_sft, serve


def test_the_server_and_the_trainer_share_one_prompt_renderer():
    """A server with its own copy of the formatting serves a model a prompt it
    was never trained on — the failure the single-generator rule exists for."""
    assert serve.render is copilot_sft.render


def test_a_sample_renders_as_prompt_and_target():
    prompt, target = copilot_sft.render([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Goal: a plate"},
        {"role": "assistant", "content": '{"op": "add_primitive", "args": {}}'},
    ])
    assert prompt.endswith("<|im_start|>assistant\n")
    assert "rules" in prompt and "Goal: a plate" in prompt
    assert target == '{"op": "add_primitive", "args": {}}<|im_end|>'
    assert "add_primitive" not in prompt, "the label must not leak into the prompt"


def test_a_tool_call_label_renders_as_the_json_the_prose_path_parses():
    """A `full`-profile sample labels with `tool_calls`; one trained format is
    better than two, so it renders as the same JSON object."""
    _, target = copilot_sft.render([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": "Goal: x"},
        {"role": "assistant", "content": "", "tool_calls": [{"id": "c", "type": "function",
            "function": {"name": "bounding_box", "arguments": "{}"}}]},
    ])
    assert json.loads(target.replace("<|im_end|>", "")) == {"op": "bounding_box", "args": {}}


def test_multipart_user_content_is_flattened():
    prompt, _ = copilot_sft.render([
        {"role": "user", "content": [{"type": "text", "text": "Goal: x"},
                                     {"type": "text", "text": "Steps so far: none"}]},
        {"role": "assistant", "content": "{}"},
    ])
    assert "Goal: x" in prompt and "Steps so far: none" in prompt


@pytest.mark.parametrize("text, found", [
    ('{"op": "bounding_box", "args": {}}', {"op": "bounding_box", "args": {}}),
    ('Sure: {"op": "dof", "args": {}} — done', {"op": "dof", "args": {}}),
    ('{"broken": ', None),
    ('no json here', None),
])
def test_the_first_json_object_is_recovered(text, found):
    assert serve._first_json(text) == found


def test_a_tool_calling_caller_gets_a_tool_call():
    message = serve.message_for('{"op": "add_hole", "args": {"radius": 0.0025}, '
                                '"rationale": "the bore"}', wants_tools=True)
    call = message["tool_calls"][0]["function"]
    assert call["name"] == "add_hole" and json.loads(call["arguments"]) == {"radius": 0.0025}
    assert message["content"] == "the bore"


def test_a_finish_answer_becomes_plain_text():
    message = serve.message_for('{"done": true, "summary": "built the plate"}', wants_tools=True)
    assert message == {"role": "assistant", "content": "built the plate"}


def test_a_prose_caller_gets_the_text_unchanged():
    """The finetuned profile asks for JSON and reads it itself; the server must
    not rewrite the reply for a caller that offered no tools."""
    raw = '{"op": "check_geometry", "args": {}}'
    assert serve.message_for(raw, wants_tools=False) == {"role": "assistant", "content": raw}


def test_prose_with_no_json_is_passed_through_not_invented():
    message = serve.message_for("I could not work out what to do.", wants_tools=True)
    assert message["content"].startswith("I could not")
    assert "tool_calls" not in message


def test_merging_an_adapter_leaves_the_same_function():
    """Serving merges LoRA into the weights for speed; the merge has to be a
    change of representation, not of behaviour."""
    import torch

    torch.manual_seed(0)
    base = torch.nn.Linear(8, 4, bias=False)
    wrapped = copilot_sft.LoRALinear(base, rank=2, alpha=4)
    with torch.no_grad():                     # give the adapter a real effect
        wrapped.b.normal_(0, 0.1)
    holder = torch.nn.Sequential(wrapped)
    x = torch.randn(3, 8)
    before = holder(x)

    assert copilot_sft.merge_lora(holder) == 1
    assert isinstance(holder[0], torch.nn.Linear), "the wrapper is gone"
    assert torch.allclose(before, holder(x), atol=1e-6)


@pytest.mark.parametrize("chunks, stops_after", [
    (['{"op"', ': "dof"', ', "args": {}', '}'], 4),
    (['{"a": {"b": 1}', '}'], 2),
    (['{"note": "a } brace in a string"', '}'], 2),
    # A quote escaped *inside* the string, built with json.dumps so the escaping
    # is the real thing. The first version of this case wrote an escaped
    # backslash followed by a quote, which genuinely ends the JSON string — so
    # stopping there was right and the test was wrong.
    ([json.dumps({"note": 'a quote " and a } brace'})[:-1], '}'], 2),
])
def test_generation_stops_at_the_end_of_one_json_object(chunks, stops_after):
    """The protocol's answer is one object, so this is exact rather than a guess —
    and braces inside strings must not end it early."""
    watcher = serve.JsonStop()
    fired = [i + 1 for i, chunk in enumerate(chunks) if watcher.feed(chunk)]
    assert fired and fired[0] == stops_after


def test_the_stop_watcher_ignores_text_before_the_object():
    watcher = serve.JsonStop()
    assert not watcher.feed("sure, here you go: ")
    assert watcher.feed('{"op": "bounding_box", "args": {}}')
