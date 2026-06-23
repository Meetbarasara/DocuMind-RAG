"""Regression test for the frontend API_BASE LOW nit (see BUGFIXES.md).

frontend/utils.py hardcoded API_BASE = "http://localhost:8000" — no way to
point the Streamlit UI at a deployed backend without editing source.
API_BASE is computed at module import time, so the test reimports the
module after changing the env var rather than just checking the constant
once.
"""

import importlib

import pytest

from frontend import utils as frontend_utils


@pytest.fixture(autouse=True)
def reload_back_to_default():
    """Leave the module in its default state for any other test/import."""
    yield
    import os
    os.environ.pop("API_BASE", None)
    importlib.reload(frontend_utils)


def test_api_base_defaults_to_localhost_when_unset(monkeypatch):
    monkeypatch.delenv("API_BASE", raising=False)
    importlib.reload(frontend_utils)
    assert frontend_utils.API_BASE == "http://localhost:8000"


def test_api_base_reads_env_var(monkeypatch):
    monkeypatch.setenv("API_BASE", "https://api.example.com")
    importlib.reload(frontend_utils)
    assert frontend_utils.API_BASE == "https://api.example.com"
