"""frontend/pages/chat.py — Main chat interface for DocuMind."""

from typing import Dict, List, Optional

import streamlit as st

from frontend.utils import (
    api_feedback,
    api_list_documents,
    api_page_image,
    api_query_stream,
    format_file_size,
)

# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _cached_page_image(filename: str, page) -> Optional[bytes]:
    """Fetch a page snapshot once per session, then reuse it.

    Streamlit re-runs the whole script (and executes collapsed expanders) on
    every interaction, so without this each rerun re-fetched every cited page
    image over HTTP — visible latency on every question. Cached in per-session
    state, NOT @st.cache_data (which is shared across users and could serve one
    user's page image to another who happens to have a same-named file).
    """
    cache = st.session_state.setdefault("_page_image_cache", {})
    key = f"{filename}::{page}"
    if key not in cache:
        cache[key] = api_page_image(filename, page)
    return cache[key]


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
            badge = " · 🖼️ page snapshot" if s.get("has_visual") else ""
            st.markdown(
                f"**[{s.get('source_id', '?')}]** `{fname}` — Page {page} ({ctype}){badge}"
            )
            if snippet:
                st.caption(f"> {snippet[:300]}…" if len(snippet) > 300 else f"> {snippet}")
            # B-hybrid: show the actual rendered page the multimodal answer read from.
            if s.get("has_visual"):
                img = _cached_page_image(fname, page)
                if img:
                    st.image(img, caption=f"📄 {fname} — page {page}", use_container_width=True)
            st.divider()


def _submit_feedback(run_id: str, score: float) -> None:
    """O4: POST a thumbs score, remember it, and re-render to show the ack."""
    try:
        api_feedback(run_id, score)
        st.session_state.setdefault("feedback_given", {})[run_id] = score
        st.toast("Thanks for your feedback!", icon="✅")
    except Exception:
        st.toast("Couldn't record feedback.", icon="⚠️")
    st.rerun()


def _render_feedback(run_id: Optional[str]) -> None:
    """O4: 👍/👎 under an answer → a LangSmith feedback score on its trace.

    Only shown when the answer carried a run_id (i.e. tracing is on); once rated,
    the buttons are replaced by a small acknowledgement. Keyed on the run_id
    alone (not the history index) so the SAME widget identity is used whether
    this turn is rendered live or later from history — a click while it's still
    the live bubble isn't lost when it moves into history.
    """
    if not run_id:
        return
    if run_id in st.session_state.get("feedback_given", {}):
        st.caption("✓ Thanks for your feedback.")
        return
    up, down, _ = st.columns([1, 1, 10])
    if up.button("👍", key=f"fb_up_{run_id}", help="Helpful"):
        _submit_feedback(run_id, 1.0)
    if down.button("👎", key=f"fb_down_{run_id}", help="Not helpful"):
        _submit_feedback(run_id, 0.0)


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

    Returns ``(full_answer, sources, run_id, interrupted)``. L5: on a mid-stream error
    event or a dropped connection it stops cleanly and reports
    ``interrupted=True`` — it no longer silently re-fires a full *blocking*
    re-query (that doubled the cost of every hiccup and could hang the UI for
    seconds). The caller offers an explicit Retry instead.
    """
    sources: List[Dict] = []
    full_answer = ""
    run_id: Optional[str] = None
    interrupted = False

    with st.chat_message("ai"):
        placeholder = st.empty()
        # Until the first token arrives (rewrite → retrieve → rerank → first
        # token can be a few seconds), show a thinking indicator so the bubble
        # isn't just an empty void.
        placeholder.markdown("🤔 _Thinking…_")
        try:
            for event in api_query_stream(prompt, history_for_api, filename_filter):
                etype = event.get("type")
                if etype == "sources":
                    sources = event.get("sources", [])
                elif etype == "token":
                    full_answer += event.get("content", "")
                    placeholder.markdown(full_answer + "▌")
                elif etype == "meta":
                    run_id = event.get("run_id")   # O4: trace id for 👍/👎
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
            # Show 👍/👎 right here in the live bubble — no extra st.rerun needed
            # to surface it (the stable run_id key carries over once this turn
            # lands in history on the next interaction).
            _render_feedback(run_id)

    return full_answer, sources, run_id, interrupted


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
            if msg["role"] == "ai":
                _render_feedback(msg.get("run_id"))

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

    full_answer, sources, run_id, interrupted = _stream_answer(prompt, history_for_api, filename_filter)

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
        {"role": "ai", "content": full_answer, "sources": sources, "run_id": run_id}
    )
    # No st.rerun() here: the streamed answer and its 👍/👎 are already on
    # screen, so forcing a second full re-render per question (which also
    # re-fetched every cited page image) was pure latency + a visible blank
    # flash. The turn re-renders from history on the next interaction, with the
    # same run_id-keyed feedback widget.
