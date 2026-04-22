"""frontend/utils.py — API client + session state helpers for DocuMind Streamlit UI."""

import json
from typing import Dict, Generator, List, Optional

import httpx
import streamlit as st

API_BASE = "http://localhost:8000"
REQUEST_TIMEOUT = 60.0


# ──────────────────────────────────────────────────────────────────────────────
#  Session state
# ──────────────────────────────────────────────────────────────────────────────


def init_session_state() -> None:
    """Ensure all required session keys are initialised."""
    defaults = {
        "access_token": None,
        "user_id": None,
        "user_email": None,
        "chat_history": [],          # list of {"role": "human"|"ai", "content": str}
        "documents": [],             # list of document metadata dicts
        "selected_file": None,       # filename filter for retrieval
        "page": "login",             # "login" | "chat" | "documents"
    }
    for key, default in defaults.items():
        if key not in st.session_state:
            st.session_state[key] = default


def is_authenticated() -> bool:
    return bool(st.session_state.get("access_token"))


def auth_headers() -> Dict[str, str]:
    return {"Authorization": f"Bearer {st.session_state['access_token']}"}


def clear_session() -> None:
    for key in ["access_token", "user_id", "user_email", "chat_history", "documents", "selected_file"]:
        st.session_state[key] = None if key != "chat_history" and key != "documents" else []
    st.session_state["page"] = "login"


# ──────────────────────────────────────────────────────────────────────────────
#  Auth API
# ──────────────────────────────────────────────────────────────────────────────


def api_signup(email: str, password: str) -> Dict:
    resp = httpx.post(
        f"{API_BASE}/api/auth/signup",
        json={"email": email, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def api_login(email: str, password: str) -> Dict:
    resp = httpx.post(
        f"{API_BASE}/api/auth/login",
        json={"email": email, "password": password},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def api_logout() -> None:
    try:
        httpx.post(
            f"{API_BASE}/api/auth/logout",
            headers=auth_headers(),
            timeout=REQUEST_TIMEOUT,
        )
    except Exception:
        pass
    clear_session()


# ──────────────────────────────────────────────────────────────────────────────
#  Documents API
# ──────────────────────────────────────────────────────────────────────────────


def api_upload_document(file_bytes: bytes, filename: str, content_type: str) -> Dict:
    resp = httpx.post(
        f"{API_BASE}/api/documents/upload",
        headers=auth_headers(),
        files={"file": (filename, file_bytes, content_type)},
        timeout=120.0,   # fast parsing ~5-10s; hi_res ~2-3 min (CPU)
    )
    resp.raise_for_status()
    return resp.json()


def api_list_documents() -> List[Dict]:
    resp = httpx.get(
        f"{API_BASE}/api/documents/",
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("documents", [])


def api_delete_document(filename: str) -> Dict:
    resp = httpx.delete(
        f"{API_BASE}/api/documents/{filename}",
        headers=auth_headers(),
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


# ──────────────────────────────────────────────────────────────────────────────
#  Chat API
# ──────────────────────────────────────────────────────────────────────────────


def api_query(
    question: str,
    chat_history: List[Dict],
    filename_filter: Optional[str] = None,
) -> Dict:
    resp = httpx.post(
        f"{API_BASE}/api/chat/query",
        headers=auth_headers(),
        json={
            "question": question,
            "chat_history": chat_history,
            "filename_filter": filename_filter,
        },
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json()


def api_query_stream(
    question: str,
    chat_history: List[Dict],
    filename_filter: Optional[str] = None,
) -> Generator[Dict, None, None]:
    """Yields parsed SSE event dicts from the streaming endpoint."""
    with httpx.stream(
        "POST",
        f"{API_BASE}/api/chat/query/stream",
        headers={**auth_headers(), "Accept": "text/event-stream"},
        json={
            "question": question,
            "chat_history": chat_history,
            "filename_filter": filename_filter,
        },
        timeout=120.0,
    ) as response:
        response.raise_for_status()
        for line in response.iter_lines():
            if line.startswith("data:"):
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    yield json.loads(payload)
                except json.JSONDecodeError:
                    pass


# ──────────────────────────────────────────────────────────────────────────────
#  UI helpers
# ──────────────────────────────────────────────────────────────────────────────


def show_error(msg: str) -> None:
    st.error(f"❌ {msg}")


def show_success(msg: str) -> None:
    st.success(f"✅ {msg}")


def format_file_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 ** 2:
        return f"{size_bytes / 1024:.1f} KB"
    else:
        return f"{size_bytes / 1024 ** 2:.2f} MB"
