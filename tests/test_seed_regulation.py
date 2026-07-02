"""Seed script core: parse → extract → ingest → cache one regulation.

No network — the judge, pipeline, and DB are fakes. Guards the contract that
matters across modules: requirements are cached as a list of dicts whose keys
match what POST /api/compliance/check reconstructs (id/text/page/section), and
the doc is ingested into the shared 'regulations' namespace.
"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from scripts.seed_regulation import seed_regulation
from src.components.config import Config

_FIXTURE = str(Path(__file__).resolve().parents[1] / "data" / "compliance" / "rbi_kyc_requirements.pdf")


class FakeJudge:
    """Returns one requirement per chunk (extraction maps over chunks)."""

    async def ainvoke(self, messages):
        return SimpleNamespace(
            content='{"requirements": [{"text": "Retain records for five years.", "section": "3"}]}'
        )


class FakePipeline:
    def __init__(self, config):
        self.config = config
        self.ingested = []

    def ingest_file(self, path, namespace=""):
        self.ingested.append((path, namespace))
        return 3


class FakeDb:
    def __init__(self):
        self.saved = None

    def upsert_regulation(self, name, regulator=None, circular_id=None,
                          requirements=None, namespace="regulations"):
        self.saved = {"name": name, "regulator": regulator, "circular_id": circular_id,
                      "requirements": requirements, "namespace": namespace}
        return {"id": "reg-xyz", **self.saved}


def test_seed_extracts_ingests_and_caches_requirements():
    config = Config()
    pipeline = FakePipeline(config)
    db = FakeDb()

    result = asyncio.run(seed_regulation(
        pdf_path=_FIXTURE, name="RBI KYC (demo)", regulator="RBI", circular_id="X1",
        namespace="regulations", config=config, pipeline=pipeline, judge_llm=FakeJudge(), db=db,
    ))

    # Ingested into the SHARED regulations namespace (not a user namespace).
    assert pipeline.ingested and pipeline.ingested[0][1] == "regulations"

    # Requirements cached as a list of dicts with exactly the keys the check
    # route reconstructs from (dataclasses.asdict of Requirement).
    reqs = db.saved["requirements"]
    assert isinstance(reqs, list) and len(reqs) >= 1
    assert set(reqs[0]) == {"id", "text", "page", "section"}
    assert reqs[0]["text"] == "Retain records for five years."

    assert db.saved["namespace"] == "regulations"
    assert result["regulation_id"] == "reg-xyz"
    assert result["requirements"] == len(reqs)
