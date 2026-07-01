"""judge.py — factory for the compliance *judge* LLM.

The gap-analysis judge does the hard reasoning ("does this policy clause satisfy
this RBI requirement?"), which is beyond the fast 8b chat model. We route it to a
stronger model *without paying*: free Cerebras llama-3.3-70b by default. Cerebras,
Groq and OpenRouter all expose an OpenAI-compatible API, so one ChatOpenAI with a
per-provider base_url covers all three — and avoids langchain-cerebras, whose
0.6.0 pins an old langchain-core that breaks this stack (verified 2026-07-02).

Swappable via config.JUDGE_PROVIDER / JUDGE_MODEL with zero code change.
"""

from langchain_openai import ChatOpenAI

from src.components.config import Config

# provider -> (OpenAI-compatible base URL, the Config attribute holding its key)
_PROVIDERS = {
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


class JudgeNotConfigured(RuntimeError):
    """Raised when the selected judge provider has no API key (or is unknown).

    The compliance routes catch this and return a clear 503 instead of crashing —
    the rest of the app never depends on the judge being configured.
    """


def build_judge_llm(config: Config, *, json_mode: bool = True) -> ChatOpenAI:
    """Return a ChatOpenAI pointed at the configured judge provider.

    Args:
        config:    app Config (reads JUDGE_PROVIDER / JUDGE_MODEL + the key).
        json_mode: request strict JSON responses (the judge always returns JSON).

    Raises:
        JudgeNotConfigured: unknown provider, or its key is missing/blank.
    """
    provider = (config.JUDGE_PROVIDER or "").strip().lower()
    if provider not in _PROVIDERS:
        raise JudgeNotConfigured(
            f"Unknown JUDGE_PROVIDER {provider!r}. Options: {', '.join(_PROVIDERS)}"
        )

    base_url, key_attr = _PROVIDERS[provider]
    api_key = (getattr(config, key_attr, None) or "").strip()
    if not api_key:
        raise JudgeNotConfigured(
            f"JUDGE_PROVIDER={provider!r} needs {key_attr} set in your environment or .env"
        )

    # response_format json_object makes the model return parseable JSON; the
    # engine still parses defensively (a bad row degrades to "Needs review",
    # it never crashes the whole check).
    model_kwargs = {"response_format": {"type": "json_object"}} if json_mode else {}
    return ChatOpenAI(
        model=config.JUDGE_MODEL,
        temperature=0,              # deterministic verdicts
        api_key=api_key,
        base_url=base_url,
        max_tokens=1024,
        timeout=60,
        model_kwargs=model_kwargs,
    )
