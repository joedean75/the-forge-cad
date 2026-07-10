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
