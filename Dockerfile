# DocuMind — the FastAPI backend (the gap-analysis engine, auth, uploads).
# The Next.js UI has its own image (frontend-next/Dockerfile); the legacy
# Streamlit app is not deployed, so it is intentionally not copied in here.
FROM python:3.13.7-slim

WORKDIR /app

# curl: used by the HEALTHCHECK below to actually hit /health over HTTP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# requirements.txt's `-e .` (editable install) needs setup.py + the real
# package source already present to resolve, so there's no dependency-only
# layer to cache separately here — copy everything up front, then install.
COPY requirements.txt setup.py ./
COPY src/ ./src/

RUN pip install --no-cache-dir -r requirements.txt

# Bake the local embedding model (~420MB) into the image so the container never
# downloads it from huggingface.co at startup. Without this, every cold start /
# new revision would re-fetch it — slow, and a hard dependency on an external
# host some deployment networks block. HF_HOME points the cache at a path the
# non-root runtime user can read; load_local_embeddings() reads it offline-first
# (local_files_only) and degrades to a runtime download only if it's ever absent.
ENV HF_HOME=/opt/hf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')"

# Non-root user (Part C hardening). The app creates its own runtime
# directories on demand (tmp_uploads/ for in-flight uploads, logs/ for the
# rotating file handler) via mkdir(exist_ok=True) — owning /app is enough for
# documind to create them lazily, no need to pre-create or volume-mount them.
RUN useradd --create-home --uid 1000 documind \
    && chown -R documind:documind /app /opt/hf
USER documind

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
