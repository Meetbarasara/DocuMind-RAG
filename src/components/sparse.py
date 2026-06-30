"""sparse.py — stateless sparse (lexical) encoder for Pinecone native hybrid (L1).

Pinecone native hybrid fuses a DENSE vector (local sentence-transformers embedding) with a SPARSE
keyword vector *server-side*, in one dotproduct index — so there is no
in-process BM25 index to rebuild or hold in RAM (the failing of the old local
hybrid: per-process, rebuilt from the whole namespace on the first query after
an upload, not multi-worker/restart safe).

The standard sparse encoder (pinecone-text BM25Encoder) can't be installed here
(its mmh3 build has no Python-3.13 Windows wheel), so this is a small,
dependency-free, **stateless** encoder: tokenize → drop stopwords → hash each
term into a fixed sparse space with a sublinear-TF weight. Stateless means
nothing to "fit" on a corpus and nothing to persist — exactly the property that
makes server-side hybrid simpler than a managed local index. The same encoder is
used for documents (ingest) and queries (retrieval), so a shared term lands on
the same index in both.
"""

import hashlib
import math
import re
from collections import Counter
from typing import Dict, List, Tuple

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_DIM = 2 ** 20  # sparse hash space
_STOPWORDS = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "of", "to",
    "and", "or", "in", "on", "for", "with", "as", "by", "at", "from", "it",
    "this", "that", "these", "those", "we", "our", "you", "your", "i",
}


def _hash(token: str) -> int:
    return int(hashlib.md5(token.encode("utf-8")).hexdigest(), 16) % _DIM


def _tokenize(text: str) -> List[str]:
    return [
        t for t in _TOKEN_RE.findall((text or "").lower())
        if len(t) > 1 and t not in _STOPWORDS
    ]


def encode_text(text: str) -> Dict:
    """Return a Pinecone sparse vector ``{"indices": [...], "values": [...]}``.

    Sublinear-TF weighting (``1 + log(tf)``); hash collisions sum.
    """
    counts = Counter(_tokenize(text))
    if not counts:
        return {"indices": [], "values": []}
    by_index: Dict[int, float] = {}
    for token, tf in counts.items():
        idx = _hash(token)
        by_index[idx] = by_index.get(idx, 0.0) + (1.0 + math.log(tf))
    return {"indices": list(by_index.keys()), "values": list(by_index.values())}


def convex_scale(dense: List[float], sparse: Dict, alpha: float) -> Tuple[List[float], Dict]:
    """Weight dense vs sparse for a hybrid query (``alpha=1`` → dense only,
    ``0`` → sparse only).

    Pinecone fuses by dot product, so weighting is just scaling each vector:
    dense by ``alpha``, sparse values by ``1 - alpha``.
    """
    alpha = min(1.0, max(0.0, alpha))
    scaled_dense = [v * alpha for v in dense]
    scaled_sparse = {
        "indices": list(sparse.get("indices", [])),
        "values": [v * (1.0 - alpha) for v in sparse.get("values", [])],
    }
    return scaled_dense, scaled_sparse
