"""evalution.py — RAGAS-based evaluation for the DocuMind RAG pipeline.

Note: The filename typo (``evalution``) is intentional to match existing imports.
Rename to ``evaluation.py`` in a future refactor if desired.

Provides:
  - ``EvaluationManager.evaluate_single`` — one Q&A pair
  - ``EvaluationManager.evaluate_batch`` — list of Q&A pairs
"""

from typing import Any, Dict, List, Optional

from src.components.config import Config
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  EvaluationManager
# ══════════════════════════════════════════════════════════════════════════════


class EvaluationManager:
    """Run RAGAS metrics against RAG pipeline outputs.

    The RAGAS judge LLM + embeddings are pointed at Gemini (not RAGAS's OpenAI
    default) so the eval runs on the same provider as the app — see
    _get_ragas_models.

    Metrics evaluated:
        - ``faithfulness``        — Is the answer grounded in the retrieved context?
        - ``answer_relevancy``    — How relevant is the answer to the question?
        - ``context_precision``   — Are the retrieved chunks relevant?
        - ``context_recall``      — Are all relevant chunks retrieved? (requires ground truth)
    """

    def __init__(self, config: Config):
        self.config = config
        self._ragas_available = self._check_ragas()
        # Lazily-built (judge LLM, embeddings) pair, wrapped for RAGAS. Built once
        # and reused across rows. See _get_ragas_models for why this is needed.
        self._ragas_models = None

    # ─────────────────────────────────────────────────────────────────────────
    #  Setup helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_ragas() -> bool:
        """Return True if the ``ragas`` package is importable."""
        import importlib.util

        available = importlib.util.find_spec("ragas") is not None
        if not available:
            logger.warning(
                "ragas is not installed — EvaluationManager will return empty results. "
                "Install it with: pip install ragas"
            )
        return available

    def _get_ragas_models(self):
        """Return the (judge LLM, embeddings) RAGAS should use, wrapped for ragas.

        RAGAS otherwise defaults its judge LLM *and* its embedding-based metrics to
        OpenAI — but the app runs entirely on Gemini and there's no OpenAI key. So
        we point both at the same Gemini models the pipeline uses (judge at
        temperature 0 for deterministic scoring). Built once, cached on the manager.
        """
        if self._ragas_models is None:
            from langchain_google_genai import (
                ChatGoogleGenerativeAI,
                GoogleGenerativeAIEmbeddings,
            )
            from ragas.embeddings import LangchainEmbeddingsWrapper
            from ragas.llms import LangchainLLMWrapper

            llm = LangchainLLMWrapper(ChatGoogleGenerativeAI(
                model=self.config.LLM_MODEL_NAME,
                temperature=0,
                google_api_key=self.config.GOOGLE_API_KEY,
            ))
            embeddings = LangchainEmbeddingsWrapper(GoogleGenerativeAIEmbeddings(
                model=self.config.EMBEDDING_MODEL_NAME,
                google_api_key=self.config.GOOGLE_API_KEY,
            ))
            self._ragas_models = (llm, embeddings)
        return self._ragas_models

    def _get_metrics(self, include_recall: bool):
        """Return the list of RAGAS metric objects to use."""
        from ragas.metrics import (
            answer_relevancy,
            context_precision,
            faithfulness,
        )

        metrics = [faithfulness, answer_relevancy, context_precision]

        if include_recall:
            try:
                from ragas.metrics import context_recall
                metrics.append(context_recall)
            except ImportError:
                logger.warning("context_recall metric not available in this ragas version.")

        return metrics

    # ─────────────────────────────────────────────────────────────────────────
    #  Public API
    # ─────────────────────────────────────────────────────────────────────────

    def evaluate_single(
        self,
        query: str,
        answer: str,
        contexts: List[str],
        ground_truth: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Evaluate a single Q&A pair with RAGAS.

        Args:
            query:        The user question.
            answer:       The LLM-generated answer.
            contexts:     List of retrieved context strings (chunk text).
            ground_truth: Optional reference answer for recall computation.

        Returns:
            Dict mapping metric name → score (float), plus an ``"error"`` key
            on failure.
        """
        if not self._ragas_available:
            return {"error": "ragas is not installed"}

        try:
            from datasets import Dataset
            from ragas import evaluate

            include_recall = ground_truth is not None
            metrics = self._get_metrics(include_recall)

            data = {
                "question": [query],
                "answer": [answer],
                "contexts": [contexts],
            }
            if include_recall:
                data["ground_truth"] = [ground_truth]

            llm, embeddings = self._get_ragas_models()
            dataset = Dataset.from_dict(data)
            result = evaluate(dataset, metrics=metrics, llm=llm, embeddings=embeddings)
            scores = result.to_pandas().iloc[0].to_dict()

            # Clean out non-numeric keys
            numeric_scores = {
                k: float(v)
                for k, v in scores.items()
                if isinstance(v, (int, float)) and k != "__index_level_0__"
            }
            logger.info("RAGAS single eval scores: %s", numeric_scores)
            return numeric_scores

        except Exception as e:
            logger.error("evaluate_single failed: %s", e)
            return {"error": str(e)}

    def evaluate_batch(self, test_set: List[Dict]) -> Dict[str, Any]:
        """Evaluate a list of Q&A pairs with RAGAS.

        Each item in *test_set* should be a dict with keys:
            - ``question``    (str)
            - ``answer``      (str)
            - ``contexts``    (List[str])
            - ``ground_truth``  (str, optional)

        Returns:
            Dict with:
                - ``"scores"``   — list of per-row score dicts
                - ``"summary"``  — averaged metric values across the batch
                - ``"error"``    — error string if evaluation failed
        """
        if not self._ragas_available:
            return {"error": "ragas is not installed"}

        if not test_set:
            return {"error": "Empty test set provided"}

        try:
            from datasets import Dataset
            from ragas import evaluate

            include_recall = all("ground_truth" in item for item in test_set)
            metrics = self._get_metrics(include_recall)

            data: Dict[str, List] = {
                "question": [],
                "answer": [],
                "contexts": [],
            }
            if include_recall:
                data["ground_truth"] = []

            for item in test_set:
                data["question"].append(item["question"])
                data["answer"].append(item["answer"])
                data["contexts"].append(item["contexts"])
                if include_recall:
                    data["ground_truth"].append(item.get("ground_truth", ""))

            llm, embeddings = self._get_ragas_models()
            dataset = Dataset.from_dict(data)
            result = evaluate(dataset, metrics=metrics, llm=llm, embeddings=embeddings)
            df = result.to_pandas()

            metric_cols = [
                c for c in df.columns
                if df[c].dtype in ("float64", "float32", "int64")
                and c != "__index_level_0__"
            ]

            scores = df[metric_cols].to_dict(orient="records")
            summary = {col: float(df[col].mean()) for col in metric_cols}

            logger.info(
                "RAGAS batch eval complete (%d samples). Summary: %s",
                len(test_set), summary,
            )
            return {"scores": scores, "summary": summary}

        except Exception as e:
            logger.error("evaluate_batch failed: %s", e)
            return {"error": str(e)}


# ══════════════════════════════════════════════════════════════════════════════
#  Retrieval metrics (page-level, label-light) + gold-set loader (Pillar E)
# ══════════════════════════════════════════════════════════════════════════════
#
# Page-level (not chunk-id) relevance keeps the gold set robust to re-chunking:
# a row labels which page(s) answer the question, and we check whether the
# retriever surfaced a chunk from one of those pages.


def hit_at_k(retrieved_pages: List, relevant_pages: List, k: int) -> float:
    """1.0 if any of the top-k retrieved pages is relevant, else 0.0."""
    rel = set(relevant_pages)
    return 1.0 if rel and (set(retrieved_pages[:k]) & rel) else 0.0


def recall_at_k(retrieved_pages: List, relevant_pages: List, k: int) -> float:
    """Fraction of the relevant pages that appear in the top-k retrieved."""
    rel = set(relevant_pages)
    if not rel:
        return 0.0
    return len(set(retrieved_pages[:k]) & rel) / len(rel)


def mrr(retrieved_pages: List, relevant_pages: List) -> float:
    """Reciprocal rank of the first relevant page (0.0 if none retrieved)."""
    rel = set(relevant_pages)
    for rank, page in enumerate(retrieved_pages, start=1):
        if page in rel:
            return 1.0 / rank
    return 0.0


def load_goldset(path: str) -> List[Dict]:
    """Load a JSONL gold set (one JSON object per line) into a list of dicts."""
    import json

    rows = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows


def regression_failures(current: Dict, baseline: Dict, tolerance: float = 0.05) -> List:
    """Metrics that dropped more than *tolerance* below the baseline (E2 CI gate).

    Both inputs are flat ``{metric: float}`` dicts. Returns
    ``[(metric, baseline_value, current_value), ...]`` — empty means no
    regression. Non-numeric (and bool) baseline values, and metrics missing from
    *current*, are skipped so the gate only fails on a genuine numeric drop.
    """
    failures = []
    for metric, base_val in baseline.items():
        if isinstance(base_val, bool) or not isinstance(base_val, (int, float)):
            continue
        cur_val = current.get(metric)
        if isinstance(cur_val, (int, float)) and not isinstance(cur_val, bool):
            if cur_val < base_val - tolerance:
                failures.append((metric, base_val, cur_val))
    return failures
