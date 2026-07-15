"""database.py — Supabase Auth + File Storage manager for DocuMind.

Provides:
  - User authentication (sign-up, sign-in, JWT validation, sign-out)
  - File storage in Supabase Storage under ``documents/{user_id}/{filename}``
  - File metadata persistence in the ``user_documents`` Supabase table
"""

import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional

import httpx
from supabase import Client, create_client

from src.components.config import Config
from src.exception import CustomException
from src.logger import get_logger
from src.utils import sanitize_filename

logger = get_logger(__name__)

# Messages that identify a "try again" socket condition when the exception type
# alone doesn't (some client libs wrap the original OSError in their own type).
_TRANSIENT_MARKERS = ("WinError 10035", "WouldBlock", "timed out", "temporarily unavailable")


def _is_transient_net_error(exc: BaseException) -> bool:
    """True for retryable network blips, walking the exception chain.

    The concrete case that motivated this: on Windows, the shared sync HTTP
    client under two concurrent requests can surface WSAEWOULDBLOCK
    ([WinError 10035]) — the socket just wasn't ready; an immediate retry
    succeeds."""
    e: Optional[BaseException] = exc
    for _ in range(5):
        if e is None:
            return False
        if isinstance(e, (OSError, httpx.HTTPError)):
            return True
        if any(marker in str(e) for marker in _TRANSIENT_MARKERS):
            return True
        e = e.__cause__ or e.__context__
    return False


def _retry_transient(fn: Callable, what: str, attempts: int = 3, base_delay: float = 0.15):
    """Call ``fn()``, briefly retrying transient network errors.

    Anything still failing after the retries (or failing for a non-network
    reason) is raised to the caller. Callers must NOT translate that into an
    empty result: an availability blip rendering as "no data" is exactly how
    the WinError-10035 bug presented a fully seeded regulations table as
    "No regulations yet" (see BUGFIXES.md)."""
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as e:
            if attempt == attempts or not _is_transient_net_error(e):
                raise
            logger.warning(
                "%s: transient network error (attempt %d/%d), retrying: %s",
                what, attempt, attempts, e,
            )
            time.sleep(base_delay * attempt)


# ══════════════════════════════════════════════════════════════════════════════
#  SupabaseManager — auth + storage + metadata
# ══════════════════════════════════════════════════════════════════════════════


class SupabaseManager:
    """Thin wrapper around the Supabase Python client for DocuMind operations."""

    def __init__(self, config: Config):
        self.config = config

        if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
            raise CustomException("SUPABASE_URL or SUPABASE_ANON_KEY is not set.")

        # SEC-9 fix: this used to fall back to the anon client (with only a
        # warning) when the service-role key was missing. Storage and admin
        # operations (and the app's whole user-isolation model — see SEC-3)
        # assume the service-role client is what's actually in use; silently
        # substituting the anon client would silently change the app's
        # security posture instead of failing loudly where a
        # misconfiguration is easy to notice.
        if not config.SUPABASE_SERVICE_ROLE_KEY:
            raise CustomException(
                "SUPABASE_SERVICE_ROLE_KEY is not set. It's required for "
                "storage and admin operations — refusing to silently fall "
                "back to the anon key."
            )

        # Public (anon) client — used for auth operations
        self.client: Client = create_client(
            config.SUPABASE_URL,
            config.SUPABASE_ANON_KEY,
        )

        # Service-role client — used for storage + admin metadata operations
        self.service_client: Client = create_client(
            config.SUPABASE_URL,
            config.SUPABASE_SERVICE_ROLE_KEY,
        )

        self._bucket = config.SUPABASE_STORAGE_BUCKET
        logger.info("SupabaseManager initialised (bucket=%s)", self._bucket)

    # ─────────────────────────────────────────────────────────────────────────
    #  Auth
    # ─────────────────────────────────────────────────────────────────────────

    def sign_up(self, email: str, password: str) -> Dict:
        """Create a new Supabase Auth user.

        Returns:
            Dict with ``user`` and ``session`` keys from the Supabase response.

        Raises:
            CustomException: on Supabase auth errors.
        """
        try:
            response = self.client.auth.sign_up({"email": email, "password": password})
            logger.info("sign_up: new user registered (%s)", email)
            return {"user": response.user, "session": response.session}
        except Exception as e:
            logger.error("sign_up failed for %s: %s", email, e)
            raise CustomException(f"Sign-up failed: {e}") from e

    def sign_in(self, email: str, password: str) -> Dict:
        """Authenticate a user and return JWT tokens.

        Returns:
            Dict with ``user``, ``access_token``, and ``refresh_token``.
        """
        try:
            response = self.client.auth.sign_in_with_password(
                {"email": email, "password": password}
            )
            logger.info("sign_in: user authenticated (%s)", email)
            return {
                "user": response.user,
                "access_token": response.session.access_token,
                "refresh_token": response.session.refresh_token,
            }
        except Exception as e:
            logger.error("sign_in failed for %s: %s", email, e)
            raise CustomException(f"Sign-in failed: {e}") from e

    def refresh_session(self, refresh_token: str) -> Dict:
        """Exchange a refresh token for a fresh access+refresh token pair.

        Supabase access tokens are short-lived (~1h). Without this, a session
        silently dies mid-use once the access token expires; the frontend uses
        this to renew transparently instead of bouncing the user to login.

        Returns:
            Dict with ``user``, ``access_token``, and ``refresh_token``.
        """
        try:
            response = self.client.auth.refresh_session(refresh_token)
            logger.info("refresh_session: session renewed")
            return {
                "user": response.user,
                "access_token": response.session.access_token,
                "refresh_token": response.session.refresh_token,
            }
        except Exception as e:
            logger.warning("refresh_session failed: %s", e)
            raise CustomException(f"Session refresh failed: {e}") from e

    def get_current_user(self, access_token: str) -> Optional[Dict]:
        """Validate a JWT and return the user payload, or *None* if invalid."""
        try:
            response = self.client.auth.get_user(access_token)
            return response.user
        except Exception as e:
            logger.warning("get_current_user: invalid token — %s", e)
            return None

    def sign_out(self, access_token: str) -> bool:
        """Invalidate a user session server-side.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        try:
            # Bug 5 fix: set_session(token, "") was passing an empty string as
            # refresh_token, which the Supabase SDK rejects or silently ignores.
            # The correct approach is to call sign_out() with the JWT directly
            # so Supabase invalidates the server-side session without needing
            # the refresh token at all.
            #
            # BUG-2 fix: admin.* operations require the service-role client.
            # self.client is the anon-key client — calling admin.sign_out on
            # it raised every time, was caught right below, and silently
            # returned False, so logout never actually invalidated the
            # session server-side (the JWT stayed valid until natural
            # expiry; only the frontend's local state was cleared).
            self.service_client.auth.admin.sign_out(access_token)
            logger.info("sign_out: session invalidated")
            return True
        except Exception as e:
            logger.error("sign_out failed: %s", e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  File Storage
    # ─────────────────────────────────────────────────────────────────────────

    def _storage_path(self, user_id: str, filename: str) -> str:
        """Build the storage key: ``{user_id}/{filename}``.

        SEC-2: sanitizes *filename* to a basename so a value containing
        ``..`` segments can't move the storage key outside the user's prefix.
        """
        try:
            safe_filename = sanitize_filename(filename)
        except ValueError as e:
            raise CustomException(f"Invalid filename: {filename!r}") from e
        return f"{user_id}/{safe_filename}"

    # ── B-hybrid: page snapshots ──────────────────────────────────────────

    @staticmethod
    def _page_image_path(namespace: str, filename: str, page_number) -> str:
        """Deterministic storage key for a rendered page snapshot."""
        safe = sanitize_filename(filename)
        return f"pages/{namespace}/{safe}/{page_number}.png"

    def upload_page_image(self, namespace: str, filename: str, page_number, data: bytes) -> str:
        """Store a rendered PDF page snapshot (B-hybrid). Upsert so re-uploads overwrite."""
        path = self._page_image_path(namespace, filename, page_number)
        self.service_client.storage.from_(self._bucket).upload(
            path=path, file=data,
            file_options={"content-type": "image/png", "upsert": "true"},
        )
        return path

    def download_page_image(self, namespace: str, filename: str, page_number) -> Optional[bytes]:
        """Fetch a page snapshot, or None if it isn't stored."""
        path = self._page_image_path(namespace, filename, page_number)
        try:
            return self.service_client.storage.from_(self._bucket).download(path)
        except Exception as e:
            logger.warning("download_page_image miss for %s: %s", path, e)
            return None

    def upload_file(
        self, user_id: str, file_bytes: bytes, filename: str, content_type: str = "application/octet-stream"
    ) -> str:
        """Upload *file_bytes* to Supabase Storage.

        Args:
            user_id:      The authenticated user's UUID.
            file_bytes:   Raw bytes of the file to upload.
            filename:     Target filename in storage (used as the key suffix).
            content_type: MIME type for the stored object.

        Returns:
            The full storage path string.

        Raises:
            CustomException: on upload failure.
        """
        storage_path = self._storage_path(user_id, filename)
        try:
            self.service_client.storage.from_(self._bucket).upload(
                path=storage_path,
                file=file_bytes,
                file_options={"content-type": content_type, "upsert": "true"},
            )
            logger.info("Uploaded %s → %s/%s", filename, self._bucket, storage_path)
            return storage_path
        except Exception as e:
            logger.error("upload_file failed for %s: %s", filename, e)
            raise CustomException(f"File upload failed: {e}") from e

    def list_files(self, user_id: str) -> List[Dict]:
        """List all files stored for *user_id*.

        Returns:
            List of dicts returned by the Supabase Storage list API.
        """
        try:
            result = self.service_client.storage.from_(self._bucket).list(user_id)
            logger.info("list_files: found %d files for user %s", len(result), user_id)
            return result
        except Exception as e:
            logger.error("list_files failed for user %s: %s", user_id, e)
            return []

    def download_file(self, user_id: str, filename: str) -> str:
        """Download a file from Supabase Storage to a local temp path.

        Returns:
            Absolute path to the downloaded temp file.

        Raises:
            CustomException: if the download fails.
        """
        # SEC-2: sanitize before building the *local* tmp_path below — this is
        # a direct filesystem join, separate from _storage_path's own check.
        try:
            filename = sanitize_filename(filename)
        except ValueError as e:
            raise CustomException(f"Invalid filename: {filename!r}") from e

        storage_path = self._storage_path(user_id, filename)
        try:
            file_bytes = self.service_client.storage.from_(self._bucket).download(storage_path)

            # Write to a deterministic temp path so callers can clean up
            tmp_dir = Path(self.config.UPLOAD_DIR)
            tmp_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = tmp_dir / filename

            tmp_path.write_bytes(file_bytes)
            logger.info("Downloaded %s → %s", storage_path, tmp_path)
            return str(tmp_path)
        except Exception as e:
            logger.error("download_file failed for %s: %s", filename, e)
            raise CustomException(f"File download failed: {e}") from e

    def delete_file(self, user_id: str, filename: str) -> bool:
        """Delete a file from Supabase Storage.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        storage_path = self._storage_path(user_id, filename)
        try:
            self.service_client.storage.from_(self._bucket).remove([storage_path])
            logger.info("Deleted storage object: %s", storage_path)
            return True
        except Exception as e:
            logger.error("delete_file failed for %s: %s", filename, e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  File Metadata (user_documents table)
    # ─────────────────────────────────────────────────────────────────────────

    def record_upload(
        self, user_id: str, filename: str, file_type: str, size_bytes: int
    ) -> Optional[Dict]:
        """Upsert a row into the ``user_documents`` metadata table.

        Uses upsert (on_conflict) so re-uploading the same file updates the
        existing row rather than raising a unique constraint violation.

        Expected schema:
            user_documents(id, user_id, filename, file_type, size_bytes, uploaded_at)

        Returns:
            The upserted row dict, or *None* on failure.
        """
        try:
            row = {
                "user_id": user_id,
                "filename": filename,
                "file_type": file_type,
                "size_bytes": size_bytes,
                # BUG-13 fix: utcnow() is deprecated since 3.12 and naive
                # (no tz info) — now() with UTC is tz-aware.
                "uploaded_at": datetime.now(UTC).isoformat(),
            }
            result = (
                self.service_client.table("user_documents")
                .upsert(row, on_conflict="user_id,filename")
                .execute()
            )
            logger.info("Recorded upload metadata for %s (user=%s)", filename, user_id)
            return result.data[0] if result.data else None
        except Exception as e:
            logger.warning("record_upload metadata upsert failed: %s", e)
            return None

    def get_user_documents(self, user_id: str) -> List[Dict]:
        """Return all document metadata rows for *user_id*.

        Returns:
            List of row dicts from ``user_documents``.
        """
        try:
            result = (
                self.service_client.table("user_documents")
                .select("*")
                .eq("user_id", user_id)
                .order("uploaded_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error("get_user_documents failed for user %s: %s", user_id, e)
            return []

    def delete_document_record(self, user_id: str, filename: str) -> bool:
        """Delete the metadata row for *filename* owned by *user_id*.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        try:
            self.service_client.table("user_documents").delete().match(
                {"user_id": user_id, "filename": filename}
            ).execute()
            logger.info("Deleted metadata record for %s (user=%s)", filename, user_id)
            return True
        except Exception as e:
            logger.error("delete_document_record failed: %s", e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  Chat history (conversations + messages tables)
    #
    #  All scoped by user_id (from the JWT) so one user can never read or write
    #  another's conversation, even though we use the service-role client.
    # ─────────────────────────────────────────────────────────────────────────

    def create_conversation(self, user_id: str, title: str = "New chat") -> Dict:
        """Create a new conversation for *user_id* and return its row."""
        try:
            result = (
                self.service_client.table("conversations")
                .insert({"user_id": user_id, "title": (title or "New chat")[:120]})
                .execute()
            )
            return result.data[0]
        except Exception as e:
            logger.error("create_conversation failed: %s", e)
            raise CustomException(f"Could not create conversation: {e}") from e

    def list_conversations(self, user_id: str) -> List[Dict]:
        """Return *user_id*'s conversations, most-recently-updated first."""
        try:
            result = (
                self.service_client.table("conversations")
                .select("id, title, updated_at")
                .eq("user_id", user_id)
                .order("updated_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error("list_conversations failed: %s", e)
            return []

    def get_conversation_messages(self, user_id: str, conversation_id: str) -> List[Dict]:
        """Return the messages of *conversation_id*, oldest first.

        Scoped by ``user_id`` too, so a guessed/forged conversation id belonging
        to another user returns nothing.
        """
        try:
            result = (
                self.service_client.table("messages")
                .select("role, content, sources, run_id, created_at")
                .eq("conversation_id", conversation_id)
                .eq("user_id", user_id)
                .order("created_at", desc=False)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error("get_conversation_messages failed: %s", e)
            return []

    def add_message(
        self,
        user_id: str,
        conversation_id: str,
        role: str,
        content: str,
        sources: Optional[List] = None,
        run_id: Optional[str] = None,
    ) -> Dict:
        """Append a message to a conversation and bump its ``updated_at``."""
        try:
            result = (
                self.service_client.table("messages")
                .insert({
                    "conversation_id": conversation_id,
                    "user_id": user_id,
                    "role": role,
                    "content": content,
                    "sources": sources,
                    "run_id": run_id,
                })
                .execute()
            )
            # Touch the parent so the sidebar orders by latest activity.
            self.service_client.table("conversations").update(
                {"updated_at": datetime.now(UTC).isoformat()}
            ).match({"id": conversation_id, "user_id": user_id}).execute()
            return result.data[0]
        except Exception as e:
            logger.error("add_message failed: %s", e)
            raise CustomException(f"Could not save message: {e}") from e

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        """Delete a conversation (its messages cascade) owned by *user_id*."""
        try:
            self.service_client.table("conversations").delete().match(
                {"id": conversation_id, "user_id": user_id}
            ).execute()
            return True
        except Exception as e:
            logger.error("delete_conversation failed: %s", e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  Compliance gap-analysis (regulations + compliance_checks tables)
    #
    #  `regulations` is SHARED reference data (an RBI circular, ingested once by
    #  the seed step); `requirements` caches the extracted requirement list so a
    #  check never re-extracts. `compliance_checks` is per-user (like
    #  conversations) and stores completed gap tables so re-opening is instant.
    # ─────────────────────────────────────────────────────────────────────────

    def upsert_regulation(
        self,
        name: str,
        regulator: Optional[str] = None,
        circular_id: Optional[str] = None,
        requirements: Optional[List] = None,
        namespace: str = "regulations",
    ) -> Dict:
        """Insert/refresh a shared regulation (seed/admin path).

        Upserts on the unique ``name`` so re-seeding updates the same row's
        cached requirements instead of raising a duplicate-key error.
        """
        try:
            row = {
                "name": name,
                "regulator": regulator,
                "circular_id": circular_id,
                "namespace": namespace,
                "requirements": requirements or [],
                "ingested_at": datetime.now(UTC).isoformat(),
            }
            result = (
                self.service_client.table("regulations")
                .upsert(row, on_conflict="name")
                .execute()
            )
            return result.data[0] if result.data else row
        except Exception as e:
            logger.error("upsert_regulation failed: %s", e)
            raise CustomException(f"Could not save regulation: {e}") from e

    def list_regulations(self) -> List[Dict]:
        """List available regulations, newest first. Omits the large
        ``requirements`` blob — that's fetched per check via get_regulation.

        Raises (CustomException) when the store can't be queried: this used to
        swallow every error into ``[]``, so a one-off socket blip made the UI
        assert "No regulations yet" while the table held data."""
        try:
            result = _retry_transient(
                lambda: (
                    self.service_client.table("regulations")
                    .select("id, name, regulator, circular_id, namespace, ingested_at")
                    .order("ingested_at", desc=True)
                    .execute()
                ),
                "list_regulations",
            )
            return result.data or []
        except Exception as e:
            logger.error("list_regulations failed: %s", e)
            raise CustomException(f"Could not list regulations: {e}") from e

    def get_regulation(self, regulation_id: str) -> Optional[Dict]:
        """Fetch one regulation incl. its cached ``requirements``.

        ``None`` means the row genuinely doesn't exist (callers 404 on it) — a
        transient network failure raises instead, so "couldn't reach the store"
        never masquerades as "this regulation was deleted"."""
        try:
            result = _retry_transient(
                lambda: (
                    self.service_client.table("regulations")
                    .select("*")
                    .eq("id", regulation_id)
                    .limit(1)
                    .execute()
                ),
                "get_regulation",
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error("get_regulation failed: %s", e)
            if _is_transient_net_error(e):
                raise CustomException(f"Could not load regulation: {e}") from e
            return None

    def save_compliance_check(
        self, user_id: str, policy_label: str, regulation_id: Optional[str],
        summary: Dict, rows: List,
    ) -> Dict:
        """Persist a completed gap-check result for *user_id*."""
        try:
            result = (
                self.service_client.table("compliance_checks")
                .insert({
                    "user_id": user_id,
                    "policy_label": policy_label,
                    "regulation_id": regulation_id,
                    "summary": summary,
                    "rows": rows,
                })
                .execute()
            )
            return result.data[0] if result.data else {}
        except Exception as e:
            logger.error("save_compliance_check failed: %s", e)
            raise CustomException(f"Could not save compliance check: {e}") from e

    def list_compliance_checks(self, user_id: str) -> List[Dict]:
        """List *user_id*'s past checks, newest first (omits the big ``rows``)."""
        try:
            result = (
                self.service_client.table("compliance_checks")
                .select("id, policy_label, regulation_id, summary, created_at")
                .eq("user_id", user_id)
                .order("created_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as e:
            logger.error("list_compliance_checks failed: %s", e)
            return []

    def get_compliance_check(self, user_id: str, check_id: str) -> Optional[Dict]:
        """Fetch one persisted check incl. its ``rows``, scoped to *user_id*."""
        try:
            result = (
                self.service_client.table("compliance_checks")
                .select("*")
                .eq("id", check_id)
                .eq("user_id", user_id)
                .limit(1)
                .execute()
            )
            return result.data[0] if result.data else None
        except Exception as e:
            logger.error("get_compliance_check failed: %s", e)
            return None
