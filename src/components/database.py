"""database.py — Supabase Auth + File Storage manager for DocuMind.

Provides:
  - User authentication (sign-up, sign-in, JWT validation, sign-out)
  - File storage in Supabase Storage under ``documents/{user_id}/{filename}``
  - File metadata persistence in the ``user_documents`` Supabase table
"""

from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from supabase import Client, create_client

from src.components.config import Config
from src.exception import CustomException
from src.logger import get_logger

logger = get_logger(__name__)


# ══════════════════════════════════════════════════════════════════════════════
#  SupabaseManager — auth + storage + metadata
# ══════════════════════════════════════════════════════════════════════════════


class SupabaseManager:
    """Thin wrapper around the Supabase Python client for DocuMind operations."""

    def __init__(self, config: Config):
        self.config = config

        if not config.SUPABASE_URL or not config.SUPABASE_ANON_KEY:
            raise CustomException("SUPABASE_URL or SUPABASE_ANON_KEY is not set.")

        # Public (anon) client — used for auth operations
        self.client: Client = create_client(
            config.SUPABASE_URL,
            config.SUPABASE_ANON_KEY,
        )

        # Service-role client — used for storage + admin metadata operations
        # Falls back to anon client when service role key is absent
        if config.SUPABASE_SERVICE_ROLE_KEY:
            self.service_client: Client = create_client(
                config.SUPABASE_URL,
                config.SUPABASE_SERVICE_ROLE_KEY,
            )
        else:
            logger.warning(
                "SUPABASE_SERVICE_ROLE_KEY not set — using anon key for storage. "
                "Some operations may fail due to RLS policies."
            )
            self.service_client = self.client

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

    def get_current_user(self, access_token: str) -> Optional[Dict]:
        """Validate a JWT and return the user payload, or *None* if invalid."""
        try:
            response = self.client.auth.get_user(access_token)
            return response.user
        except Exception as e:
            logger.warning("get_current_user: invalid token — %s", e)
            return None

    def sign_out(self, access_token: str) -> bool:
        """Invalidate a user session.

        Returns:
            ``True`` on success, ``False`` otherwise.
        """
        try:
            # Set the session so the sign-out targets the right token
            self.client.auth.set_session(access_token, "")
            self.client.auth.sign_out()
            logger.info("sign_out: session invalidated")
            return True
        except Exception as e:
            logger.error("sign_out failed: %s", e)
            return False

    # ─────────────────────────────────────────────────────────────────────────
    #  File Storage
    # ─────────────────────────────────────────────────────────────────────────

    def _storage_path(self, user_id: str, filename: str) -> str:
        """Build the storage key: ``{user_id}/{filename}``."""
        return f"{user_id}/{filename}"

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
                "uploaded_at": datetime.utcnow().isoformat(),
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
