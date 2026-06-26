"""documents.py — Document management routes for DocuMind.

Endpoints:
    POST   /api/documents/upload        — upload file, ingest, embed
    GET    /api/documents/              — list user's documents
    DELETE /api/documents/{filename}    — delete from storage + Pinecone
"""

import asyncio
from pathlib import Path

from fastapi import (
    APIRouter,
    Depends,
    File,
    HTTPException,
    Request,
    Response,
    UploadFile,
    status,
)

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.api.error_utils import log_and_get_ref
from src.api.limiter import limiter
from src.components.database import SupabaseManager
from src.logger import get_logger
from src.pipeline.pipeline import RAGPipeline
from src.utils import sanitize_filename

logger = get_logger(__name__)

router = APIRouter(prefix="/api/documents", tags=["documents"])

_UPLOAD_READ_CHUNK_BYTES = 1024 * 1024  # 1MB


# ──────────────────────────────────────────────────────────────────────────────
#  Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _validate_extension(filename: str, supported: tuple) -> None:
    ext = Path(filename).suffix.lstrip(".").lower()
    if ext not in supported:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type '.{ext}'. Supported: {', '.join(supported)}",
        )


async def _read_upload_within_limit(file: UploadFile, max_bytes: int) -> bytes:
    """Read *file* in chunks, aborting as soon as *max_bytes* is exceeded.

    SEC-6: the previous `await file.read()` buffered the entire upload into
    memory regardless of size, with no cap — a few large uploads could
    exhaust server memory/CPU (DoS). Reading in bounded chunks caps actual
    memory use at roughly max_bytes instead of the attacker's chosen size.
    """
    chunks = []
    total = 0
    while True:
        chunk = await file.read(_UPLOAD_READ_CHUNK_BYTES)
        if not chunk:
            break
        total += len(chunk)
        if total > max_bytes:
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=f"File exceeds the {max_bytes // (1024 * 1024)}MB upload limit",
            )
        chunks.append(chunk)
    return b"".join(chunks)


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/upload", status_code=status.HTTP_201_CREATED)
@limiter.limit("10/minute")
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
    pipeline: RAGPipeline = Depends(get_pipeline),
):
    """Upload a document, store in Supabase Storage, then ingest into Pinecone."""
    config = pipeline.config

    # SEC-2: file.filename is the raw client-supplied multipart filename — it
    # can contain "../" segments with no validation from FastAPI/Starlette
    # (unlike a URL path param, there's no routing-layer protection here).
    # Reduce it to a basename before it's used to build any local path or
    # storage key.
    try:
        safe_filename = sanitize_filename(file.filename)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid filename: {file.filename!r}",
        )

    _validate_extension(safe_filename, config.SUPPORTED_FILE_TYPES)

    user_id = str(current_user["user"].id)
    file_bytes = await _read_upload_within_limit(file, config.MAX_UPLOAD_SIZE_BYTES)
    file_size = len(file_bytes)
    ext = Path(safe_filename).suffix.lstrip(".").lower()

    # ── 1. Save to Supabase Storage ──────────────────────────────────────
    try:
        # Latency Optimization #7: db.upload_file is a blocking Supabase
        # Storage call -- without to_thread it blocks the event loop for
        # the whole upload, the slowest step in this route.
        await asyncio.to_thread(
            db.upload_file,
            user_id=user_id,
            file_bytes=file_bytes,
            filename=safe_filename,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        ref = log_and_get_ref(logger, "Storage upload failed", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Storage upload failed. (ref: {ref})",
        )

    # ── 2. Write to temp file for ingestion ──────────────────────────────
    tmp_dir = Path(config.UPLOAD_DIR)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / safe_filename
    tmp_path.write_bytes(file_bytes)

    # ── 3. Ingest → embed → upsert ───────────────────────────────────────
    try:
        # Latency Optimization #7: pipeline.ingest_file does real parsing +
        # OpenAI embedding + Pinecone upsert network calls, all blocking.
        chunk_count = await asyncio.to_thread(
            pipeline.ingest_file,
            str(tmp_path),
            user_id=user_id,
            namespace=user_id,
        )
    except Exception as e:
        # BUG-10 fix: ingestion failing here used to leave the storage
        # object from step 1 orphaned — only the local temp file got
        # cleaned up. Best-effort delete it too; a cleanup failure is
        # logged but must not mask the original ingestion error.
        try:
            await asyncio.to_thread(db.delete_file, user_id=user_id, filename=safe_filename)
        except Exception:
            logger.warning("Cleanup after failed ingestion also failed for %s", safe_filename)
        ref = log_and_get_ref(logger, "Ingestion failed", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed. (ref: {ref})",
        )
    finally:
        # Clean up temp file
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    # ── 4. Record metadata ───────────────────────────────────────────────
    # BUG-10 fix: this return value used to go unchecked. On failure, the
    # storage object and Pinecone vectors from steps 1+3 would still exist
    # with no metadata row — invisible in the UI (which lists from this
    # table), yet still consuming quota and still answerable by chat,
    # while the client was told 201 success regardless.
    # Latency Optimization #7: db.record_upload is a blocking Supabase call.
    upload_record = await asyncio.to_thread(
        db.record_upload,
        user_id=user_id,
        filename=safe_filename,
        file_type=ext,
        size_bytes=file_size,
    )
    if not upload_record:
        try:
            await asyncio.to_thread(db.delete_file, user_id=user_id, filename=safe_filename)
        except Exception:
            logger.warning("Storage rollback after failed record_upload failed for %s", safe_filename)
        try:
            await asyncio.to_thread(pipeline.delete_document, filename=safe_filename, namespace=user_id)
        except Exception:
            logger.warning("Pinecone rollback after failed record_upload failed for %s", safe_filename)
        ref = log_and_get_ref(
            logger, "Failed to record upload metadata", Exception("record_upload returned None")
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Upload failed. (ref: {ref})",
        )

    return {
        "message": "Document uploaded and ingested successfully",
        "filename": safe_filename,
        "chunks_ingested": chunk_count,
        "size_bytes": file_size,
    }


@router.get("/")
async def list_documents(
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Return all documents uploaded by the current user."""
    user_id = str(current_user["user"].id)
    # Latency Optimization #7: db.get_user_documents is a blocking Supabase call.
    docs = await asyncio.to_thread(db.get_user_documents, user_id)
    return {"documents": docs, "count": len(docs)}


@router.delete("/{filename}", status_code=status.HTTP_200_OK)
async def delete_document(
    filename: str,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
    pipeline: RAGPipeline = Depends(get_pipeline),
):
    """Delete a document from Supabase Storage and remove its Pinecone vectors."""
    user_id = str(current_user["user"].id)

    # SEC-2: defense-in-depth — FastAPI's default (non-":path") string
    # converter already rejects "/" in this segment, and dot-segments
    # ("..") get normalized away before routing ever matches. Still
    # sanitize explicitly rather than relying on that routing behavior as
    # the only safety net.
    try:
        filename = sanitize_filename(filename)
    except ValueError:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid filename: {filename!r}",
        )

    # ── Delete from Storage ───────────────────────────────────────────────
    # Latency Optimization #7: these are all blocking Supabase/Pinecone calls.
    storage_deleted = await asyncio.to_thread(db.delete_file, user_id=user_id, filename=filename)
    if not storage_deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete '{filename}' from storage. "
                   "Metadata and vectors were NOT removed to prevent orphaned data.",
        )

    # ── Delete metadata record ────────────────────────────────────────────
    await asyncio.to_thread(db.delete_document_record, user_id=user_id, filename=filename)

    # ── Delete Pinecone vectors ───────────────────────────────────────────
    try:
        await asyncio.to_thread(pipeline.delete_document, filename=filename, namespace=user_id)
    except Exception as e:
        ref = log_and_get_ref(logger, "Failed to delete vectors from Pinecone", e)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete vectors from Pinecone. (ref: {ref})",
        )

    return {"message": f"'{filename}' deleted successfully"}


@router.get("/page-image/{filename}/{page}")
async def get_page_image(
    filename: str,
    page: int,
    current_user: dict = Depends(get_current_user),
    db: SupabaseManager = Depends(get_db),
):
    """Return the rendered snapshot of a document page (B-hybrid) — the chat UI
    uses it to show the actual page a multimodal answer read from."""
    user_id = str(current_user["user"].id)
    try:
        filename = sanitize_filename(filename)
    except ValueError:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid filename")

    data = await asyncio.to_thread(db.download_page_image, user_id, filename, page)
    if not data:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Page image not found")
    return Response(content=data, media_type="image/png")
