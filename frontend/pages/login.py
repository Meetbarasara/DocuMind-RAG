"""frontend/pages/login.py — Sign-in / Sign-up page for DocuMind."""

import streamlit as st

from frontend.utils import api_login, api_signup, init_session_state, show_error, show_success


def render_login_page() -> None:
    """Render the authentication page with sign-in and sign-up tabs."""
    st.markdown(
        """
        <style>
        .auth-container { max-width: 420px; margin: 0 auto; padding-top: 2rem; }
        </style>
        """,
        unsafe_allow_html=True,
    )

    st.markdown(
        "<h1 style='text-align:center; color:#7C3AED;'>🧠 DocuMind</h1>",
        unsafe_allow_html=True,
    )
    st.markdown(
        "<p style='text-align:center; color:#6B7280;'>AI-powered document intelligence</p>",
        unsafe_allow_html=True,
    )
    st.divider()

    tab_login, tab_signup = st.tabs(["🔑 Sign In", "📝 Sign Up"])

    # ── Sign In ───────────────────────────────────────────────────────────
    with tab_login:
        with st.form("login_form"):
            email = st.text_input("Email", placeholder="you@example.com")
            password = st.text_input("Password", type="password", placeholder="••••••••")
            submitted = st.form_submit_button("Sign In", use_container_width=True, type="primary")

        if submitted:
            if not email or not password:
                show_error("Please fill in all fields.")
            else:
                with st.spinner("Signing in…"):
                    try:
                        result = api_login(email, password)
                        st.session_state["access_token"] = result["access_token"]
                        st.session_state["user_id"] = result["user_id"]
                        st.session_state["user_email"] = result["email"]
                        st.session_state["page"] = "chat"
                        st.rerun()
                    except Exception as e:
                        show_error(f"Login failed: {e}")

    # ── Sign Up ───────────────────────────────────────────────────────────
    with tab_signup:
        with st.form("signup_form"):
            new_email = st.text_input("Email", placeholder="you@example.com", key="su_email")
            new_password = st.text_input(
                "Password", type="password", placeholder="Min. 6 characters", key="su_pass"
            )
            confirm_password = st.text_input(
                "Confirm Password", type="password", placeholder="Repeat password", key="su_confirm"
            )
            submitted_su = st.form_submit_button("Create Account", use_container_width=True, type="primary")

        if submitted_su:
            if not new_email or not new_password:
                show_error("Please fill in all fields.")
            elif new_password != confirm_password:
                show_error("Passwords do not match.")
            elif len(new_password) < 6:
                show_error("Password must be at least 6 characters.")
            else:
                with st.spinner("Creating account…"):
                    try:
                        api_signup(new_email, new_password)
                        show_success(
                            "Account created! Check your email to confirm, then sign in."
                        )
                    except Exception as e:
                        show_error(f"Sign-up failed: {e}")
