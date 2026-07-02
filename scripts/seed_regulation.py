"""seed_regulation.py — one-time admin step to make a regulation checkable.

For a regulation PDF (an RBI circular), this:
  1. parses it and EXTRACTS its atomic requirements via the judge model,
  2. INGESTS it into the shared "regulations" Pinecone namespace,
  3. UPSERTS a row into the Supabase `regulations` table with the extracted
     requirements CACHED as JSON — so `POST /api/compliance/check` never
     re-extracts (expensive) per user check.

Run once per regulation. Needs live keys (Cerebras judge + Pinecone + Supabase):

    python -m scripts.seed_regulation \
        --pdf data/compliance/rbi_kyc_requirements.pdf \
        --name "RBI KYC (synthetic demo)" --regulator RBI

The core (`seed_regulation`) takes injectable dependencies so it can be unit
tested without any network; `main()` wires the real ones.
"""

import argparse
import asyncio
from dataclasses import asdict
from pathlib import Path

from src.components.compliance import extract_requirements
from src.components.ingestion import DocumentProcessor
from src.logger import get_logger

logger = get_logger(__name__)

_DEFAULT_PDF = str(Path(__file__).resolve().parents[1] / "data" / "compliance" / "rbi_kyc_requirements.pdf")


async def seed_regulation(*, pdf_path, name, regulator, circular_id, namespace,
                          config, pipeline, judge_llm, db) -> dict:
    """Extract → ingest → cache one regulation. Returns a small summary dict.

    Extraction runs over ALL parsed chunks (not retrieval) so no requirement is
    missed; each requirement keeps the page it came from (the RBI citation
    origin). The requirement list is serialised with dataclasses.asdict so its
    keys (id/text/page/section) match what the check route reconstructs.
    """
    proc = DocumentProcessor(config)
    chunks = proc.build_langchain_documents(proc.process_documents(pdf_path))
    logger.info("Parsed %s into %d chunk(s)", pdf_path, len(chunks))

    requirements = await extract_requirements(chunks, judge_llm)
    logger.info("Extracted %d requirement(s)", len(requirements))

    n_ingested = pipeline.ingest_file(pdf_path, namespace=namespace)
    logger.info("Ingested %d chunk(s) into namespace=%s", n_ingested, namespace)

    record = db.upsert_regulation(
        name=name,
        regulator=regulator,
        circular_id=circular_id,
        requirements=[asdict(r) for r in requirements],
        namespace=namespace,
    )
    return {
        "regulation_id": record.get("id"),
        "name": name,
        "chunks_ingested": n_ingested,
        "requirements": len(requirements),
    }


def main():
    # Imported here so unit tests of seed_regulation don't construct real clients.
    from src.components.config import Config
    from src.components.database import SupabaseManager
    from src.components.judge import build_judge_llm
    from src.pipeline.pipeline import RAGPipeline

    parser = argparse.ArgumentParser(description="Seed a regulation for KYC gap-analysis.")
    parser.add_argument("--pdf", default=_DEFAULT_PDF, help="Path to the regulation PDF.")
    parser.add_argument("--name", default="RBI KYC (synthetic demo)", help="Regulation name (unique).")
    parser.add_argument("--regulator", default="RBI")
    parser.add_argument("--circular-id", default=None)
    parser.add_argument("--namespace", default="regulations",
                        help="Shared Pinecone namespace for regulations.")
    args = parser.parse_args()

    config = Config()
    judge_llm = build_judge_llm(config)   # raises JudgeNotConfigured if no key
    db = SupabaseManager(config)
    pipeline = RAGPipeline(config, db=db)

    result = asyncio.run(seed_regulation(
        pdf_path=args.pdf, name=args.name, regulator=args.regulator,
        circular_id=args.circular_id, namespace=args.namespace,
        config=config, pipeline=pipeline, judge_llm=judge_llm, db=db,
    ))
    print(f"\nSeeded regulation: {result}")
    print(f"Use regulation_id={result['regulation_id']!r} in POST /api/compliance/check")


if __name__ == "__main__":
    main()
