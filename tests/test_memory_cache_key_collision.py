"""Regression test for Logical Mistake #7 (see BUGFIXES.md / CODE_REVIEW.md §3).

_hash_messages keyed the memory-summarization cache on just
"count:<len>::last:<first 50 chars of the last message>" -- ignoring every
other message's content entirely. Two different conversations with the
same number of messages and the same (or same-prefix) last message collide
on the same cache key and get served each other's cached summary.
"""

import src.utils as utils_module
from src.utils import _hash_messages, _summarize_older_messages


def test_hash_messages_distinguishes_different_conversations_with_same_tail():
    same_tail = "Thanks, can you also check the deployment logs from yesterday please"
    convo_a = [
        {"role": "user", "content": "Tell me about quantum computing"},
        {"role": "assistant", "content": "Quantum computing uses qubits in superposition."},
        {"role": "user", "content": same_tail},
    ]
    convo_b = [
        {"role": "user", "content": "What's the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},
        {"role": "user", "content": same_tail},
    ]

    assert len(convo_a) == len(convo_b)
    assert convo_a[-1]["content"][:50] == convo_b[-1]["content"][:50]

    assert _hash_messages(convo_a) != _hash_messages(convo_b), (
        "two different conversations with the same length and the same last "
        "message must not collide on the same cache key"
    )


def test_hash_messages_is_stable_for_the_same_conversation():
    convo = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "hi there"},
    ]
    assert _hash_messages(convo) == _hash_messages(list(convo)), (
        "identical conversations must still hit the cache"
    )


class _FakeLLMChain:
    """Stands in for `summary_prompt | llm` -- returns a summary that proves
    which conversation it actually summarized, so a cache collision would
    return the *wrong* fake's text instead of erroring."""

    def __init__(self, label):
        self.label = label

    def invoke(self, _inputs):
        return type("R", (), {"content": f"summary-of-{self.label}"})()


def test_summarize_older_messages_does_not_serve_the_wrong_cached_summary(monkeypatch):
    utils_module._summary_cache.clear()

    same_tail = "Thanks, can you also check the deployment logs from yesterday please"
    convo_a = [
        {"role": "user", "content": "Tell me about quantum computing"},
        {"role": "assistant", "content": "Quantum computing uses qubits in superposition."},
        {"role": "user", "content": same_tail},
    ]
    convo_b = [
        {"role": "user", "content": "What's the capital of France?"},
        {"role": "assistant", "content": "The capital of France is Paris."},
        {"role": "user", "content": same_tail},
    ]

    # Patch at the langchain_core boundary: stub ChatPromptTemplate so
    # `prompt | llm` just returns an object whose .invoke(...) calls llm
    # directly with a marker so we can tell which conversation produced it.
    class _StubPrompt:
        def __or__(self, llm):
            return llm

    monkeypatch.setattr(
        "langchain_core.prompts.ChatPromptTemplate.from_messages",
        lambda _msgs: _StubPrompt(),
    )

    summary_a = _summarize_older_messages(convo_a, _FakeLLMChain("A"))
    summary_b = _summarize_older_messages(convo_b, _FakeLLMChain("B"))

    assert summary_a == "summary-of-A"
    assert summary_b == "summary-of-B", (
        f"convo_b got back {summary_b!r} -- served convo_a's cached summary "
        "instead of summarizing its own distinct content"
    )
