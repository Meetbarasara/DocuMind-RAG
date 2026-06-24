"""Regression test for BUG-14 (see BUGFIXES.md).

format_chat_history / format_chat_history_async's token-trim loop
re-encoded the FULL remaining (shrinking) text on every iteration while
dropping one message at a time from the front — so the most recent
message's content (which survives in `recent` until the very last
iteration) gets redundantly re-encoded once per iteration, up to `window`
times, instead of once.

Metric used: how many encode() calls contain the most recent message's
content. This is robust to the exact character/token math (no fragile
magic-number thresholds) and directly targets the redundant-re-encoding
structure, not just "is it slow."
"""

import sys
import types

import pytest

from src import utils


class _FakeEncoding:
    def __init__(self, calls):
        self._calls = calls

    def encode(self, text):
        self._calls.append(text)
        return list(text)  # 1 "token" per character — simple and deterministic


@pytest.fixture
def fake_tiktoken(monkeypatch):
    calls = []
    fake_module = types.ModuleType("tiktoken")
    fake_module.get_encoding = lambda name: _FakeEncoding(calls)
    monkeypatch.setitem(sys.modules, "tiktoken", fake_module)
    return calls


def _messages(n, body_len=200):
    # Distinct filler per message (not just repeated "x") so a marker for
    # one specific message can't accidentally match a different message
    # that happens to share the same filler text.
    return [
        {
            "role": "user" if i % 2 == 0 else "assistant",
            "content": f"msg{i}_" + ("x" * body_len),
        }
        for i in range(n)
    ]


def test_format_chat_history_does_not_repeatedly_reencode_latest_message(fake_tiktoken):
    messages = _messages(6)
    last_message_marker = messages[-1]["content"]

    # A tiny budget forces the trim loop to shrink the window heavily.
    utils.format_chat_history(messages, window=6, max_tokens=50)

    hits = sum(1 for call_text in fake_tiktoken if last_message_marker in call_text)
    assert hits <= 2, (
        f"the most recent message's content was encoded {hits} times across "
        f"{len(fake_tiktoken)} calls — looks like the shrinking window gets "
        "fully re-encoded on every trim iteration instead of each message "
        "being encoded once"
    )


@pytest.mark.asyncio
async def test_format_chat_history_async_does_not_repeatedly_reencode_latest_message(fake_tiktoken):
    messages = _messages(6)
    last_message_marker = messages[-1]["content"]

    await utils.format_chat_history_async(messages, window=6, max_tokens=50)

    hits = sum(1 for call_text in fake_tiktoken if last_message_marker in call_text)
    assert hits <= 2, (
        f"the most recent message's content was encoded {hits} times across "
        f"{len(fake_tiktoken)} calls — same redundant re-encoding issue in "
        "the async variant"
    )
