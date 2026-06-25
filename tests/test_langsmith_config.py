"""Guard for the LangSmith observability wiring (O1).

LangChain auto-traces to LangSmith purely from environment variables, so the
only thing that can go wrong in *our* code is the default: tracing must be OFF
unless explicitly opted in, or every deployment would silently ship trace data
(prompts, document text, answers) to an external service. This pins that safe
default and that the config surface exists.
"""

import os

import pytest

from src.components.config import Config


def test_langsmith_tracing_off_by_default():
    """No trace data leaves the process unless LANGSMITH_TRACING is opted in."""
    # Config reads LANGSMITH_TRACING from the env at import time; skip if the
    # ambient env happens to have opted in (don't fail a correctly-configured box).
    if os.getenv("LANGSMITH_TRACING", "").strip().lower() == "true":
        pytest.skip("LANGSMITH_TRACING is enabled in the ambient environment")
    assert Config().LANGSMITH_TRACING is False


def test_langsmith_config_surface_exists():
    """The observability knobs are present and discoverable on Config."""
    cfg = Config()
    assert hasattr(cfg, "LANGSMITH_TRACING")
    assert hasattr(cfg, "LANGSMITH_API_KEY")
    assert cfg.LANGSMITH_PROJECT  # has a sensible default project name
