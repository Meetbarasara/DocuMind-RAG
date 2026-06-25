"""frontend/pages/chat.py — Main chat interface for DocuMind."""

from typing import Dict, List, Optional

import streamlit as st

from frontend.utils import (
    api_list_documents,
    api_query_stream,
    format_file_size,
)

# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _render_sources(sources: List[Dict]) -> None:
    """Render an expandable citations section below an answer."""
    if not sources:
        return
    with st.expander(f"📚 {len(sources)} source(s) used", expanded=False):
        for s in sources:
            fname = s.get("filename") or "Unknown"
            page = s.get("page") or "N/A"
            ctype = s.get("chunk_type") or "text"
            snippet = s.get("content", "")
            st.markdown(
                f"**[{s.get('source_id', '?')}]** `{fname}` — Page {page} ({ctype})"
            )
            if snippet:
                st.caption(f"> {snippet[:300]}…" if len(snippet) > 300 else f"> {snippet}")
            st.divider()


def _sidebar_documents() -> Optional[str]:
    """Render document selector in sidebar; returns selected filename filter or None."""
    with st.sidebar:
        st.markdown("### 📂 Documents")

        if st.button("🔄 Refresh", key="sidebar_refresh"):
            st.session_state["documents"] = api_list_documents()

        if not st.session_state.get("documents"):
            st.session_state["documents"] = api_list_documents()

        docs = st.session_state.get("documents", [])

        if not docs:
            st.info("No documents yet.\nGo to **Documents** to upload files.")
            return None

        doc_options = ["All Documents"] + [d["filename"] for d in docs]
        selected = st.selectbox(
            "Filter by document",
            doc_options,
            index=0,
            key="doc_selector",
        )

        if selected != "All Documents":
            # Show file info
            doc = next((d for d in docs if d["filename"] == selected), None)
            if doc:
                size = format_file_size(doc.get("size_bytes", 0))
                st.caption(f"📄 {doc.get('file_type','').upper()} · {size}")
            return selected

        return None

    return None


# ──────────────────────────────────────────────────────────────────────────────
#  Main page
# ──────────────────────────────────────────────────────────────────────────────


def _stream_answer(
    prompt: str, history_for_api: List[Dict], filename_filter: Optional[str]
):
    """Stream one answer into a fresh assistant bubble.

    Returns ``(full_answer, sources, interrupted)``. L5: on a mid-stream error
    event or a dropped connection it stops cleanly and reports
    ``interrupted=True`` — it no longer silently re-fires a full *blocking*
    re-query (that doubled the cost of every hiccup and could hang the UI for
    seconds). The caller offers an explicit Retry instead.
    """
    sources: List[Dict] = []
    full_answer = ""
    interrupted = False

    with st.chat_message("ai"):
        placeholder = st.empty()
        try:
            for event in api_query_stream(prompt, history_for_api, filename_filter):
                etype = event.get("type")
                if etype == "sources":
                    sources = event.get("sources", [])
                elif etype == "token":
                    full_answer += event.get("content", "")
                    placeholder.markdown(full_answer + "▌")
                elif etype == "error":
                    interrupted = True
                    break
        except Exception:
            interrupted = True

        # No tokens at all also means the stream never really delivered.
        if interrupted or not full_answer:
            interrupted = True
            placeholder.markdown("⚠️ _Connection interrupted before the answer finished._")
        else:
            placeholder.markdown(full_answer)
            if sources:
                _render_sources(sources)

    return full_answer, sources, interrupted


def render_chat_page() -> None:
    """Render the main streaming chat interface."""

    filename_filter = _sidebar_documents()

    # ── Header ────────────────────────────────────────────────────────────
    col_title, col_btn = st.columns([5, 1])
    with col_title:
        filter_label = f" · *{filename_filter}*" if filename_filter else ""
        st.markdown(f"## 💬 Chat{filter_label}")
    with col_btn:
        if st.button("🆕 New Chat", key="new_chat", use_container_width=True):
            st.session_state["chat_history"] = []
            st.session_state.pop("retry_prompt", None)
            st.rerun()

    # ── Render existing messages ──────────────────────────────────────────
    for msg in st.session_state.get("chat_history", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sources"):
                _render_sources(msg["sources"])

    # ── Retry affordance for a previously interrupted answer (L5) ─────────
    # Streaming no longer silently re-fires a slow blocking query on a dropped
    # connection — the user decides whether to resend the same question.
    pending_retry = st.session_state.get("retry_prompt")
    retry_clicked = False
    if pending_retry:
        st.warning("⚠️ The last answer was interrupted before it finished.")
        retry_clicked = st.button(f"🔄 Retry: {pending_retry[:60]}", key="retry_btn")

    # ── Decide what to run: an explicit retry, or fresh chat input ────────
    new_prompt = st.chat_input("Ask anything about your documents…")
    prompt = pending_retry if retry_clicked else new_prompt
    if not prompt:
        return

    st.session_state.pop("retry_prompt", None)  # we're attempting it now

    # Display user message
    with st.chat_message("human"):
        st.markdown(prompt)

    # Build API history (role + content only)
    history_for_api = [
        {"role": m["role"], "content": m["content"]}
        for m in st.session_state.get("chat_history", [])
    ]

    full_answer, sources, interrupted = _stream_answer(prompt, history_for_api, filename_filter)

    if interrupted:
        # Don't append a broken turn; stash the prompt and re-render so the
        # Retry button shows up immediately.
        st.session_state["retry_prompt"] = prompt
        st.rerun()

    # ── Append to history (success only) ──────────────────────────────────
    st.session_state["chat_history"].append(
        {"role": "human", "content": prompt}
    )
    st.session_state["chat_history"].append(
        {"role": "ai", "content": full_answer, "sources": sources}
    )
