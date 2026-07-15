"""Regression for the TLS-interception startup failure (see BUGFIXES.md).

Loading the (already cached) sentence-transformers model made a HEAD request
to huggingface.co; on networks with HTTPS scanning that died with
CERTIFICATE_VERIFY_FAILED and killed API startup. load_local_embeddings must
try a zero-network cache-only load FIRST, and only fall back to a downloading
load when the model genuinely isn't cached.
"""

import src.components.embeddings as embeddings_module
from src.components.config import Config
from src.components.embeddings import EmbeddingManager, load_local_embeddings


class _Recorder:
    """Stands in for HuggingFaceEmbeddings; records every construction's kwargs.
    With raise_offline=True it simulates a machine with no cached model (the
    cache-only attempt fails)."""

    calls: list = []
    raise_offline = False

    def __init__(self, *args, **kwargs):
        type(self).calls.append(kwargs)
        if type(self).raise_offline and kwargs.get("model_kwargs", {}).get("local_files_only"):
            raise OSError("model not found in local cache")

    def embed_query(self, text):
        return [0.0] * 768

    def embed_documents(self, texts):
        return [[0.0] * 768 for _ in texts]


def _install(monkeypatch, raise_offline):
    _Recorder.calls = []
    _Recorder.raise_offline = raise_offline
    monkeypatch.setattr(embeddings_module, "HuggingFaceEmbeddings", _Recorder)


def test_cached_model_loads_offline_with_no_fallback(monkeypatch):
    _install(monkeypatch, raise_offline=False)
    load_local_embeddings(Config())
    assert len(_Recorder.calls) == 1
    assert _Recorder.calls[0]["model_kwargs"]["local_files_only"] is True


def test_uncached_model_falls_back_to_downloading_load(monkeypatch):
    _install(monkeypatch, raise_offline=True)
    load_local_embeddings(Config())
    assert len(_Recorder.calls) == 2
    assert _Recorder.calls[0]["model_kwargs"]["local_files_only"] is True
    assert "local_files_only" not in _Recorder.calls[1]["model_kwargs"]


def test_embedding_manager_uses_the_cache_first_loader(monkeypatch):
    _install(monkeypatch, raise_offline=False)
    EmbeddingManager(Config())
    assert _Recorder.calls[0]["model_kwargs"]["local_files_only"] is True
