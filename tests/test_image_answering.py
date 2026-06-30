"""B-hybrid answer side: page images are gathered for visual chunks (capped,
deduped) and routed into a multimodal generation call; text-only answers stay
on the fast path.

The real vision API call is mocked (like every other LLM call in the suite) —
these pin the routing + message assembly, not the model's behavior.
"""

from types import SimpleNamespace

import pytest
from langchain_core.documents import Document

from src.components.config import Config
from src.components.generation import AnswerGeneration
from src.pipeline.pipeline import RAGPipeline

# ── generation: multimodal message assembly + routing ─────────────────────

def test_multimodal_messages_embed_each_page_image():
    system, human = AnswerGeneration._multimodal_messages(
        context="CTX", query="what is shown?", history_str="", page_images=["AAAA", "BBBB"],
    )
    assert "CTX" in system.content
    parts = human.content
    assert parts[0] == {"type": "text", "text": "what is shown?"}
    img_parts = [p for p in parts if p.get("type") == "image_url"]
    assert len(img_parts) == 2
    assert img_parts[0]["image_url"]["url"] == "data:image/png;base64,AAAA"


@pytest.mark.asyncio
async def test_generate_uses_multimodal_path_only_when_images_present():
    gen = AnswerGeneration(Config())
    docs = [Document(page_content="ctx", metadata={"filename": "f.pdf", "page_number": 1})]

    class _FakeChain:
        calls = 0

        async def ainvoke(self, payload):
            type(self).calls += 1
            return "text answer"

    class _FakeLLM:
        calls = 0

        async def ainvoke(self, messages):
            type(self).calls += 1
            return SimpleNamespace(content="vision answer")

    gen.chain = _FakeChain()
    gen.llm = _FakeLLM()

    r1 = await gen.generate("q", docs)                       # no images -> text chain
    assert r1["answer"] == "text answer"
    assert _FakeChain.calls == 1 and _FakeLLM.calls == 0

    r2 = await gen.generate("q", docs, page_images=["AAAA"])  # images -> multimodal llm
    assert r2["answer"] == "vision answer"
    assert _FakeLLM.calls == 1


# ── pipeline: gathering page images for retrieved visual chunks ────────────

class _FakeDB:
    def __init__(self):
        self.calls = []

    def download_page_image(self, namespace, filename, page):
        self.calls.append((namespace, filename, page))
        return b"PNG-bytes"


def test_gather_page_images_is_capped_deduped_and_visual_only():
    p = RAGPipeline(Config(MAX_PAGE_IMAGES_PER_ANSWER=2, USE_IMAGE_ANSWERING=True), db=_FakeDB())
    docs = [
        Document(page_content="a", metadata={"filename": "f.pdf", "page_number": 3, "has_visual": True}),
        Document(page_content="b", metadata={"filename": "f.pdf", "page_number": 3, "has_visual": True}),  # dup page
        Document(page_content="c", metadata={"filename": "f.pdf", "page_number": 4, "has_visual": True}),
        Document(page_content="d", metadata={"filename": "f.pdf", "page_number": 5, "has_visual": True}),  # past cap
        Document(page_content="e", metadata={"filename": "f.pdf", "page_number": 6}),  # not visual
    ]

    imgs = p._gather_page_images(docs, "ns1")

    assert len(imgs) == 2                                   # capped
    assert all(isinstance(i, str) for i in imgs)            # base64 strings
    assert [c[2] for c in p.db.calls] == [3, 4]             # deduped + stopped at cap, visual only


def test_gather_page_images_is_a_noop_without_db():
    p = RAGPipeline(Config(), db=None)
    docs = [Document(page_content="a", metadata={"filename": "f.pdf", "page_number": 3, "has_visual": True})]
    assert p._gather_page_images(docs, "ns1") == []
