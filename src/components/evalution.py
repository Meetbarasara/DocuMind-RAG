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

    The RAGAS judge LLM (Groq) + embeddings (local) are wired explicitly (not
    RAGAS's OpenAI default) so the eval runs on the same models as the app — see
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
        OpenAI — but the app has no OpenAI key. So we point both at the same models
        the pipeline uses: Groq Llama-3.1-8B as the judge (temperature 0 for
        deterministic scoring) and the local sentence-transformers embeddings.
        Built once, cached on the manager.
        """
        if self._ragas_models is None:
            from langchain_huggingface import HuggingFaceEmbeddings
            from ragas.embeddings import LangchainEmbeddingsWrapper
            from ragas.llms import LangchainLLMWrapper

            from langchain_groq import ChatGroq

            llm = LangchainLLMWrapper(ChatGroq(
                model=self.config.LLM_MODEL_NAME,
                temperature=0,
                api_key=self.config.GROQ_API_KEY,
            ))
            # RAGAS uses embeddings internally (answer_relevancy metric); local,
            # so this adds no API cost.
            embeddings = LangchainEmbeddingsWrapper(HuggingFaceEmbeddings(
                model_name=self.config.EMBEDDING_MODEL_NAME,
                model_kwargs={"device": "cpu"},
                encode_kwargs={"normalize_embeddings": True},
            ))
            self._ragas_models = (llm, embeddings)
        return self._ragas_models

    @staticmethod
    def _ragas_run_config():
        """Throttle RAGAS so it survives a rate-limited judge (e.g. Groq's free
        tier ~30 req/min). RAGAS otherwise fires every (row × metric) job almost
        at once (default 16 workers) and floods the API into TimeoutErrors that
        never recover. Few workers + generous timeout/backoff trades wall-clock
        for actually completing the run."""
        from ragas import RunConfig

        # max_workers=2: on Groq's free tier even 4 concurrent workers tripped the
        # per-minute limit into 20s+ backoffs. Near-sequential is slower per row
        # but avoids the retry storm and actually finishes.
        return RunConfig(max_workers=2, timeout=180, max_retries=10, max_wait=60)

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
            result = evaluate(
                dataset, metrics=metrics, llm=llm, embeddings=embeddings,
                run_config=self._ragas_run_config(),
            )
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
            result = evaluate(
                dataset, metrics=metrics, llm=llm, embeddings=embeddings,
                run_config=self._ragas_run_config(),
            )
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


# ══════════════════════════════════════════════════════════════════════════════
#  Compliance gap-analysis metrics (the judge as a 4-way classifier)
# ══════════════════════════════════════════════════════════════════════════════
#
# The KYC judge assigns each requirement one of four statuses. We score it like a
# classifier against a labeled (requirement, policy_excerpt) -> status gold set:
# accuracy + macro-F1. Macro (not micro) so a rare-but-critical class like
# Conflict counts as much as the common Covered — a compliance tool that never
# flags conflicts should score poorly even if it's "mostly right".

COMPLIANCE_STATUSES = ("Covered", "Partial", "Gap", "Conflict")


def compliance_metrics(predictions: List[str], labels: List[str]) -> Dict:
    """Accuracy + macro-F1 for the gap-analysis judge over the 4 statuses.

    ``predictions`` and ``labels`` are aligned status strings. A prediction
    outside COMPLIANCE_STATUSES (e.g. "Needs review") never equals a valid label,
    so it correctly counts as a miss — it's never silently ignored. Macro-F1
    averages per-status F1 only over statuses that actually appear in ``labels``,
    so a status absent from the gold set doesn't drag the score down to 0.
    """
    if len(predictions) != len(labels):
        raise ValueError("predictions and labels must be the same length")
    n = len(labels)
    accuracy = sum(p == g for p, g in zip(predictions, labels)) / n if n else 0.0

    per_status, f1s = {}, []
    for status in COMPLIANCE_STATUSES:
        tp = sum(p == status and g == status for p, g in zip(predictions, labels))
        fp = sum(p == status and g != status for p, g in zip(predictions, labels))
        fn = sum(p != status and g == status for p, g in zip(predictions, labels))
        support = tp + fn
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / support if support else 0.0
        f1 = (2 * precision * recall / (precision + recall)) if (precision + recall) else 0.0
        per_status[status] = {"precision": precision, "recall": recall, "f1": f1, "support": support}
        if support:                         # average only over classes present in labels
            f1s.append(f1)
    macro_f1 = sum(f1s) / len(f1s) if f1s else 0.0
    return {"accuracy": accuracy, "macro_f1": macro_f1, "n": n, "per_status": per_status}


# Statuses that assert the policy DOES contain a relevant clause — so they must
# cite grounded evidence. Gap cites nothing by definition; "Needs review" never
# produced a usable verdict. Neither is an evidence-faithfulness failure.
_EVIDENCE_BEARING = ("Covered", "Partial", "Conflict")


def evidence_faithfulness(statuses: List[str], verified: List[bool]) -> Dict:
    """Of the verdicts that assert a policy clause, the fraction whose cited quote
    is verifiably grounded in a retrieved policy clause.

    ``statuses`` and ``verified`` are aligned per row; ``verified[i]`` is True when
    row i's evidence quote grounded to a real clause (i.e.
    ``compliance._verify_evidence`` cleared its threshold). Gap rows are excluded
    from the denominator — a Gap correctly cites nothing, so counting it would
    penalise a perfectly faithful run for finding gaps. Returns
    ``{"evidence_faithfulness": float, "n_grounded": int}``; the score is 1.0 when
    there are no evidence-bearing verdicts (nothing that could be unfaithful).
    """
    if len(statuses) != len(verified):
        raise ValueError("statuses and verified must be the same length")
    bearing = [bool(v) for s, v in zip(statuses, verified) if s in _EVIDENCE_BEARING]
    n = len(bearing)
    score = (sum(bearing) / n) if n else 1.0
    return {"evidence_faithfulness": score, "n_grounded": n}
