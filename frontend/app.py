"""frontend/app.py — DocuMind Streamlit application entry point.

Run with:
    streamlit run frontend/app.py
"""
import sys
from pathlib import Path

import streamlit as st

# Ensure the project root is on sys.path so `from frontend.*` and
# `from src.*` imports resolve correctly when Streamlit runs this file.
_PROJECT_ROOT = str(Path(__file__).parent.parent.resolve())
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)


# ── Page config (must be first Streamlit call) ────────────────────────────────
st.set_page_config(
    page_title="DocuMind — AI Document Intelligence",
    page_icon="🧠",
    layout="wide",
    initial_sidebar_state="expanded",
)

# These imports must come after set_page_config so page modules don't
# trigger their own Streamlit initialisation before ours.
from frontend.pages.chat import render_chat_page  # noqa: E402
from frontend.pages.documents import render_documents_page  # noqa: E402
from frontend.pages.login import render_login_page  # noqa: E402
from frontend.utils import (  # noqa: E402
    api_logout,
    init_session_state,
    is_authenticated,
)

# ── Initialise session state ──────────────────────────────────────────────────
init_session_state()


# ── Custom CSS ────────────────────────────────────────────────────────────────
st.markdown(
    """
    <style>
    /* ── Global ── */
    @import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap');
    html, body, [class*="css"] { font-family: 'Inter', sans-serif; }

    /* ── Sidebar ── */
    [data-testid="stSidebar"] { background: #0F0F1A; }
    [data-testid="stSidebar"] * { color: #E5E7EB !important; }

    /* ── Primary button ── */
    .stButton > button[kind="primary"] {
        background: linear-gradient(135deg, #7C3AED, #4F46E5);
        border: none; color: #fff; border-radius: 8px;
        transition: opacity .2s;
    }
    .stButton > button[kind="primary"]:hover { opacity: 0.88; }

    /* ── Chat messages ── */
    [data-testid="stChatMessage"] { border-radius: 12px; padding: 0.5rem; }

    /* ── Source expander ── */
    .streamlit-expanderHeader { font-size: 0.85rem; color: #6B7280; }
    </style>
    """,
    unsafe_allow_html=True,
)


# ── Routing ───────────────────────────────────────────────────────────────────
if not is_authenticated():
    render_login_page()
else:
    # ── Authenticated sidebar navigation ─────────────────────────────────
    with st.sidebar:
        st.markdown(
            "<h2 style='color:#7C3AED; margin-bottom:0;'>🧠 DocuMind</h2>",
            unsafe_allow_html=True,
        )
        st.caption(f"Signed in as **{st.session_state.get('user_email', '')}**")
        st.divider()

        nav = st.radio(
            "Navigate",
            ["💬 Chat", "📁 Documents"],
            index=0 if st.session_state.get("page") == "chat" else 1,
            label_visibility="collapsed",
        )

        st.divider()
        if st.button("🚪 Sign Out", use_container_width=True):
            api_logout()
            st.rerun()

    # ── Page rendering ────────────────────────────────────────────────────
    if nav == "💬 Chat":
        st.session_state["page"] = "chat"
        render_chat_page()
    else:
        st.session_state["page"] = "documents"
        render_documents_page()
