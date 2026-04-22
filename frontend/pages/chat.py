"""frontend/pages/chat.py — Main chat interface for DocuMind."""

from typing import Dict, List, Optional

import streamlit as st

from frontend.utils import (
    api_list_documents,
    api_query,
    api_query_stream,
    format_file_size,
    show_error,
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
            st.rerun()

    # ── Render existing messages ──────────────────────────────────────────
    for msg in st.session_state.get("chat_history", []):
        with st.chat_message(msg["role"]):
            st.markdown(msg["content"])
            if msg.get("sources"):
                _render_sources(msg["sources"])

    # ── Chat input ────────────────────────────────────────────────────────
    if prompt := st.chat_input("Ask anything about your documents…"):
        # Display user message
        with st.chat_message("human"):
            st.markdown(prompt)

        # Build API history (role + content only)
        history_for_api = [
            {"role": m["role"], "content": m["content"]}
            for m in st.session_state.get("chat_history", [])
        ]

        # ── Stream the answer ─────────────────────────────────────────────
        sources: List[Dict] = []
        full_answer = ""

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
                        show_error(event.get("message", "Stream error"))
                        break

                # Final render without cursor
                placeholder.markdown(full_answer)
                if sources:
                    _render_sources(sources)

            except Exception as e:
                # Fallback to blocking query
                show_error(f"Streaming unavailable, retrying… ({e})")
                try:
                    result = api_query(prompt, history_for_api, filename_filter)
                    full_answer = result.get("answer", "")
                    sources = result.get("sources", [])
                    placeholder.markdown(full_answer)
                    if sources:
                        _render_sources(sources)
                except Exception as e2:
                    show_error(f"Query failed: {e2}")
                    return

        # ── Append to history ──────────────────────────────────────────────
        st.session_state["chat_history"].append(
            {"role": "human", "content": prompt}
        )
        st.session_state["chat_history"].append(
            {"role": "ai", "content": full_answer, "sources": sources}
        )
