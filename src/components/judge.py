"""judge.py — factory for the compliance *judge* LLM.

The gap-analysis judge does the hard reasoning ("does this policy clause satisfy
this RBI requirement?"), which is beyond the fast 8b chat model. We route it to a
stronger model *without paying*: free Cerebras gpt-oss-120b by default. Cerebras,
Groq and OpenRouter all expose an OpenAI-compatible API, so one ChatOpenAI with a
per-provider base_url covers all three — and avoids langchain-cerebras, whose
0.6.0 pins an old langchain-core that breaks this stack (verified 2026-07-02).

Swappable via config.JUDGE_PROVIDER / JUDGE_MODEL with zero code change.
"""

import json
import re

from langchain_core.messages import AIMessage
from langchain_openai import ChatOpenAI

from src.components.config import Config

# provider -> (OpenAI-compatible base URL, the Config attribute holding its key)
_PROVIDERS = {
    "cerebras": ("https://api.cerebras.ai/v1", "CEREBRAS_API_KEY"),
    "groq": ("https://api.groq.com/openai/v1", "GROQ_API_KEY"),
    "openrouter": ("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
}


class FakeJudgeLLM:
    """Deterministic, zero-cost stand-in for the judge (JUDGE_PROVIDER=fake).

    Exists so end-to-end tests (and key-free demos) can run a REAL compliance
    check — SSE route, persistence, UI streaming — without live LLM calls,
    which on the free tiers are slow (minutes of 429 backoff) and budgeted.
    It answers the engine's two prompts with valid JSON, and its verdict
    quotes are copied VERBATIM from the supplied excerpts so the engine's
    evidence verification genuinely passes. Opt-in only — never a fallback.
    """

    # Same boundary set _split_clauses cuts on, so a quote never straddles two
    # clauses (which would drop its containment score below the threshold).
    _CLAUSE_END = re.compile(r"[.!?;:\n]")

    def _first_clause(self, text: str) -> str:
        for part in self._CLAUSE_END.split(text):
            if len(part.strip()) >= 10:
                return part.strip()
        return text.strip()[:80]

    def _judge_reply(self, human: str) -> dict:
        req, _, excerpts = human.partition("\n\nCompany policy excerpts:\n")
        req = req.removeprefix("Requirement:\n")
        if excerpts.strip().startswith("(no policy excerpts found)"):
            return {"status": "Gap", "policy_quote": "", "confidence": 0.9,
                    "rationale": "Fake judge: no excerpts were retrieved."}
        # Vary the verdict deterministically off the requirement text so a
        # test table exercises all the UI states (filters, coverage bar).
        status = ("Covered", "Partial", "Gap")[len(req.strip()) % 3]
        if status == "Gap":
            return {"status": "Gap", "policy_quote": "", "confidence": 0.9,
                    "rationale": "Fake judge: deterministic Gap verdict."}
        body = re.search(r"\[1\][^\n]*\n(.*?)(?=\n\n\[\d+\]|\Z)", excerpts, re.DOTALL)
        quote = self._first_clause(body.group(1) if body else excerpts)
        return {"status": status, "policy_quote": quote, "confidence": 0.9,
                "rationale": f"Fake judge: deterministic {status} verdict."}

    def _extract_reply(self, human: str) -> dict:
        reqs = [{"text": part.strip(), "section": None}
                for part in re.split(r"(?<=[.!?])\s+", human) if len(part.strip()) >= 40]
        return {"requirements": reqs[:3]}

    def invoke(self, messages):
        system = getattr(messages[0], "content", "") if messages else ""
        human = getattr(messages[-1], "content", "") if messages else ""
        if '"requirements"' in system:
            reply = self._extract_reply(human)
        elif '"status"' in system:
            reply = self._judge_reply(human)
        else:  # e.g. remediation drafting — plain text, not JSON
            return AIMessage(content="Fake judge: draft clause for testing purposes.")
        return AIMessage(content=json.dumps(reply))

    async def ainvoke(self, messages):
        return self.invoke(messages)


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
    if provider == "fake":
        return FakeJudgeLLM()
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
        # Reasoning models (e.g. gpt-oss) spend output budget *thinking* before
        # emitting any JSON. On dense legal text ~1000 reasoning tokens hit a
        # 1024 cap and truncated the JSON entirely, so extraction/judging of a
        # real RBI Master Direction silently failed on many chunks. 4096 leaves
        # ample room for the reasoning pass plus the (small) JSON payload.
        max_tokens=4096,
        timeout=120,
        model_kwargs=model_kwargs,
    )
