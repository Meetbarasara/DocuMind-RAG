"""frontend/pages/documents.py — Document management page for DocuMind."""

from pathlib import Path

import streamlit as st

from frontend.utils import (
    api_delete_document,
    api_list_documents,
    api_upload_document,
    format_file_size,
    show_error,
    show_success,
)

SUPPORTED_TYPES = ["pdf", "docx", "pptx", "txt", "xlsx", "csv", "html"]


def render_documents_page() -> None:
    """Render the document management interface."""
    st.header("📁 My Documents")
    st.caption("Upload documents to build your personal knowledge base.")

    # ── Upload section ────────────────────────────────────────────────────
    st.subheader("Upload a Document")
    uploaded_file = st.file_uploader(
        "Drop a file here or browse",
        type=SUPPORTED_TYPES,
        help=f"Supported: {', '.join(SUPPORTED_TYPES)}",
    )

    if uploaded_file is not None:
        col1, col2 = st.columns([3, 1])
        with col1:
            st.info(
                f"📄 **{uploaded_file.name}** — {format_file_size(uploaded_file.size)}"
            )
        with col2:
            if st.button("⬆️ Upload & Process", use_container_width=True, type="primary"):
                with st.spinner(f"Uploading and ingesting **{uploaded_file.name}**…"):
                    try:
                        result = api_upload_document(
                            file_bytes=uploaded_file.read(),
                            filename=uploaded_file.name,
                            content_type=uploaded_file.type or "application/octet-stream",
                        )
                        show_success(
                            f"Uploaded **{uploaded_file.name}** — "
                            f"{result['chunks_ingested']} chunks indexed."
                        )
                        # Refresh document list
                        st.session_state["documents"] = api_list_documents()
                        st.rerun()
                    except Exception as e:
                        show_error(f"Upload failed: {e}")

    st.divider()

    # ── Document list ─────────────────────────────────────────────────────
    st.subheader("Uploaded Documents")

    if st.button("🔄 Refresh", key="refresh_docs"):
        with st.spinner("Loading…"):
            st.session_state["documents"] = api_list_documents()

    # Load on first visit
    if not st.session_state.get("documents"):
        with st.spinner("Loading documents…"):
            st.session_state["documents"] = api_list_documents()

    docs = st.session_state.get("documents", [])

    if not docs:
        st.info("No documents uploaded yet. Upload a file above to get started.")
        return

    for doc in docs:
        fname = doc.get("filename", "Unknown")
        ftype = doc.get("file_type", "?").upper()
        size = format_file_size(doc.get("size_bytes", 0))
        uploaded_at = doc.get("uploaded_at", "")[:10]  # date only

        col_info, col_del = st.columns([5, 1])
        with col_info:
            st.markdown(
                f"**{fname}** &nbsp;&nbsp; `{ftype}` &nbsp; · &nbsp; {size} &nbsp; · &nbsp; 📅 {uploaded_at}"
            )
        with col_del:
            if st.button("🗑️", key=f"del_{fname}", help=f"Delete {fname}"):
                with st.spinner(f"Deleting {fname}…"):
                    try:
                        api_delete_document(fname)
                        show_success(f"**{fname}** deleted.")
                        st.session_state["documents"] = api_list_documents()
                        st.rerun()
                    except Exception as e:
                        show_error(f"Delete failed: {e}")
        st.divider()
