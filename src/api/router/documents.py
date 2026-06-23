"""documents.py — Document management routes for DocuMind.

Endpoints:
    POST   /api/documents/upload        — upload file, ingest, embed
    GET    /api/documents/              — list user's documents
    DELETE /api/documents/{filename}    — delete from storage + Pinecone
"""

from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status

from src.api.dependencies import get_current_user, get_db, get_pipeline
from src.components.database import SupabaseManager
from src.pipeline.pipeline import RAGPipeline
from src.utils import sanitize_filename

router = APIRouter(prefix="/api/documents", tags=["documents"])


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


# ──────────────────────────────────────────────────────────────────────────────
#  Routes
# ──────────────────────────────────────────────────────────────────────────────


@router.post("/upload", status_code=status.HTTP_201_CREATED)
async def upload_document(
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
    file_bytes = await file.read()
    file_size = len(file_bytes)
    ext = Path(safe_filename).suffix.lstrip(".").lower()

    # ── 1. Save to Supabase Storage ──────────────────────────────────────
    try:
        db.upload_file(
            user_id=user_id,
            file_bytes=file_bytes,
            filename=safe_filename,
            content_type=file.content_type or "application/octet-stream",
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Storage upload failed: {e}",
        )

    # ── 2. Write to temp file for ingestion ──────────────────────────────
    tmp_dir = Path(config.UPLOAD_DIR)
    tmp_dir.mkdir(parents=True, exist_ok=True)
    tmp_path = tmp_dir / safe_filename
    tmp_path.write_bytes(file_bytes)

    # ── 3. Ingest → embed → upsert ───────────────────────────────────────
    try:
        chunk_count = pipeline.ingest_file(
            str(tmp_path),
            user_id=user_id,
            namespace=user_id,
        )
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Ingestion failed: {e}",
        )
    finally:
        # Clean up temp file
        if tmp_path.exists():
            tmp_path.unlink(missing_ok=True)

    # ── 4. Record metadata ───────────────────────────────────────────────
    db.record_upload(
        user_id=user_id,
        filename=safe_filename,
        file_type=ext,
        size_bytes=file_size,
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
    docs = db.get_user_documents(user_id)
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
    storage_deleted = db.delete_file(user_id=user_id, filename=filename)
    if not storage_deleted:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete '{filename}' from storage. "
                   "Metadata and vectors were NOT removed to prevent orphaned data.",
        )

    # ── Delete metadata record ────────────────────────────────────────────
    db.delete_document_record(user_id=user_id, filename=filename)

    # ── Delete Pinecone vectors ───────────────────────────────────────────
    try:
        pipeline.delete_document(filename=filename, namespace=user_id)
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to delete vectors from Pinecone: {e}",
        )

    return {"message": f"'{filename}' deleted successfully"}
