"""evalution.py — RAGAS-based evaluation for the DocuMind RAG pipeline.

Note: The filename typo (``evalution``) is intentional to match existing imports.
Rename to ``evaluation.py`` in a future refactor if desired.

Provides:
  - ``EvaluationManager.evaluate_single`` — one Q&A pair
  - ``EvaluationManager.evaluate_batch`` — list of Q&A pairs
"""

import sys
from typing import Any, Dict, List, Optional

from src.components.config import Config
from src.exception import CustomException
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  EvaluationManager
# ══════════════════════════════════════════════════════════════════════════════


class EvaluationManager:
    """Run RAGAS metrics against RAG pipeline outputs.

    Metrics evaluated:
        - ``faithfulness``        — Is the answer grounded in the retrieved context?
        - ``answer_relevancy``    — How relevant is the answer to the question?
        - ``context_precision``   — Are the retrieved chunks relevant?
        - ``context_recall``      — Are all relevant chunks retrieved? (requires ground truth)
    """

    def __init__(self, config: Config):
        self.config = config
        self._ragas_available = self._check_ragas()

    # ─────────────────────────────────────────────────────────────────────────
    #  Setup helpers
    # ─────────────────────────────────────────────────────────────────────────

    @staticmethod
    def _check_ragas() -> bool:
        """Return True if the ``ragas`` package is importable."""
        try:
            import ragas  # noqa: F401
            return True
        except ImportError:
            logger.warning(
                "ragas is not installed — EvaluationManager will return empty results. "
                "Install it with: pip install ragas"
            )
            return False

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

            dataset = Dataset.from_dict(data)
            result = evaluate(dataset, metrics=metrics)
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

            dataset = Dataset.from_dict(data)
            result = evaluate(dataset, metrics=metrics)
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
