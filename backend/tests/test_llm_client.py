"""Tests for the optional LLM client."""

from app.services import llm_client


def test_llm_not_configured_when_key_missing():
    original = llm_client.LLM_API_KEY
    try:
        llm_client.LLM_API_KEY = ""
        assert not llm_client.is_configured()
    finally:
        llm_client.LLM_API_KEY = original


def test_llm_interpret_without_key_returns_none():
    original = llm_client.LLM_API_KEY
    try:
        llm_client.LLM_API_KEY = ""
        assert llm_client.interpret_prompt("box 100x50x30 mm") is None
    finally:
        llm_client.LLM_API_KEY = original


# ---------------------------------------------------------------------------
# What the provider said, not just which status it sent
# ---------------------------------------------------------------------------

class _Resp:
    """The parts of an httpx response the client reads on a failure."""

    def __init__(self, status: int, body=None, text: str = ""):
        self.status_code, self._body, self.text = status, body, text
        self.headers = {}

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


def _answers(monkeypatch, resp):
    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return resp

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    monkeypatch.setattr(llm_client, "LLM_API_KEY", "test-key")


def _failure(monkeypatch, resp):
    import pytest

    from app.services.llm_errors import LLMFailure

    _answers(monkeypatch, resp)
    with pytest.raises(LLMFailure) as caught:
        llm_client._post_chat([{"role": "user", "content": "hi"}], timeout=5.0, max_tokens=8)
    return caught.value


def test_a_spend_cap_says_so_rather_than_reading_as_a_bare_403(monkeypatch):
    """What a live run reported: `Client error '403 Forbidden'`, which names
    neither the cause nor the account it belongs to. OpenRouter had said exactly
    what was wrong and it was being thrown away."""
    failure = _failure(monkeypatch, _Resp(
        403, {"error": {"message": "Budget limit exceeded (daily limit). Contact your org admin.",
                        "code": 403}}))
    assert "Budget limit exceeded (daily limit)" in str(failure)
    # A cap is a billing matter; "check model access" sends the reader to the
    # wrong settings page.
    assert failure.code == "PROVIDER_BILLING"
    assert "spending limit" in failure.remedy
    assert failure.retryable is False


def test_a_real_permission_403_stays_an_access_failure(monkeypatch):
    failure = _failure(monkeypatch, _Resp(
        403, {"error": {"message": "No endpoints found matching your data policy."}}))
    assert failure.code == "PROVIDER_ACCESS"
    assert "data policy" in str(failure)


def test_a_failure_with_no_usable_body_still_reports_its_status(monkeypatch):
    failure = _failure(monkeypatch, _Resp(500, None, text=""))
    assert "HTTP 500" in str(failure)
    assert failure.code == "PROVIDER_ERROR" and failure.retryable is True


def test_a_plain_text_error_body_is_carried_through(monkeypatch):
    failure = _failure(monkeypatch, _Resp(502, None, text="upstream connect error"))
    assert "upstream connect error" in str(failure)


def test_the_status_rides_on_the_failure_for_the_job_to_report(monkeypatch):
    failure = _failure(monkeypatch, _Resp(429, {"error": {"message": "slow down"}}))
    assert failure.to_dict()["provider_status"] == 429
    assert failure.code == "PROVIDER_THROTTLED" and failure.retryable is True


def _recording(monkeypatch):
    """A client that answers politely and remembers what it was sent."""
    sent = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            sent.append(kw.get("json") or {})
            return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    monkeypatch.setattr(llm_client, "LLM_API_KEY", "test-key")
    return sent


def test_a_tool_call_asks_for_one_action_at_a_time(monkeypatch):
    """Left to default, a live model returned `set_spec` and `add_primitive`
    together and the run stopped before doing either. Nothing here executes a
    batch, so it is asked not to send one."""
    sent = _recording(monkeypatch)
    tool = {"type": "function", "function": {"name": "noop", "parameters": {"type": "object"}}}
    llm_client._post_chat([{"role": "user", "content": "hi"}], timeout=5.0, tools=[tool])
    assert sent[-1]["parallel_tool_calls"] is False

    llm_client._post_chat([{"role": "user", "content": "hi"}], timeout=5.0)
    assert "parallel_tool_calls" not in sent[-1], "only meaningful when tools are offered"


# ---------------------------------------------------------------------------
# A timeout is a property of the hardware, so it is configurable
# ---------------------------------------------------------------------------

def _timeouts(monkeypatch):
    """A client that answers politely and remembers the timeout it was built with."""
    seen = []

    class _Client:
        def __init__(self, *a, **kw):
            seen.append(kw.get("timeout"))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def post(self, *a, **kw):
            return _Resp(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_client.httpx, "Client", _Client)
    monkeypatch.setattr(llm_client, "LLM_API_KEY", "test-key")
    return seen


def test_a_call_with_no_timeout_takes_the_configured_one(monkeypatch):
    """A 2B served off a laptop GPU needs more than the hosted default, and the
    failure when it does not get it reads as the model being unable to answer."""
    seen = _timeouts(monkeypatch)
    monkeypatch.setattr(llm_client, "LLM_TIMEOUT", 300.0)
    llm_client._post_chat([{"role": "user", "content": "hi"}])
    assert seen == [300.0]


def test_an_explicit_timeout_still_wins(monkeypatch):
    seen = _timeouts(monkeypatch)
    monkeypatch.setattr(llm_client, "LLM_TIMEOUT", 300.0)
    llm_client._post_chat([{"role": "user", "content": "hi"}], timeout=5.0)
    assert seen == [5.0]


def test_the_planner_turn_does_not_pin_its_own_timeout():
    """`copilot`'s planner call was the one hardcoded 60 s that mattered: no
    deployment could raise it without editing code."""
    import inspect

    from app.services import copilot

    src = inspect.getsource(copilot)
    assert "timeout=60.0" not in src
