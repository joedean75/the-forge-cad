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
