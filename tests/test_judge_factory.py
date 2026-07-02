"""The compliance judge is routed to a stronger model via an OpenAI-compatible
endpoint, swappable by config. These pin: right provider -> right base_url + key,
clear error when the key is missing, and that an unknown provider is rejected.
No network — ChatOpenAI construction is offline."""

import pytest

from src.components.config import Config
from src.components.judge import JudgeNotConfigured, build_judge_llm

_BASE = dict(PINECONE_API_KEY="x", SUPABASE_URL="x", SUPABASE_ANON_KEY="x",
             SUPABASE_SERVICE_ROLE_KEY="x")


def _cfg(**over):
    # GROQ_API_KEY is required; everything else defaults.
    return Config(GROQ_API_KEY="gsk-fake", **_BASE, **over)


def test_cerebras_default_builds_with_key():
    llm = build_judge_llm(_cfg(CEREBRAS_API_KEY="csk-fake"))
    assert llm.model_name == "gpt-oss-120b"
    assert "api.cerebras.ai" in str(llm.openai_api_base)


def test_missing_key_raises_clear_error():
    # cerebras is the default provider, but no CEREBRAS_API_KEY set
    with pytest.raises(JudgeNotConfigured) as e:
        build_judge_llm(_cfg())
    assert "CEREBRAS_API_KEY" in str(e.value)


def test_groq_provider_reuses_groq_key():
    # falling back to groq should work with the already-required GROQ_API_KEY
    llm = build_judge_llm(_cfg(JUDGE_PROVIDER="groq", JUDGE_MODEL="llama-3.1-8b-instant"))
    assert "api.groq.com" in str(llm.openai_api_base)


def test_unknown_provider_rejected():
    with pytest.raises(JudgeNotConfigured):
        build_judge_llm(_cfg(JUDGE_PROVIDER="nope", CEREBRAS_API_KEY="csk-fake"))


def test_blank_key_is_treated_as_missing():
    with pytest.raises(JudgeNotConfigured):
        build_judge_llm(_cfg(CEREBRAS_API_KEY="   "))
