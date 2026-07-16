# DocuMind — the FastAPI backend (the gap-analysis engine, auth, uploads).
# The Next.js UI has its own image (frontend-next/Dockerfile); the legacy
# Streamlit app is not deployed, so it is intentionally not copied in here.
FROM python:3.13.7-slim

WORKDIR /app

# curl: used by the HEALTHCHECK below to actually hit /health over HTTP.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

# Non-root user (Part C hardening). Created up here so the model bake below can
# chown in its own layer — a late `chown -R` over /opt/hf would duplicate all
# 420MB into a new layer on every rebuild.
RUN useradd --create-home --uid 1000 documind

# LAYER ORDER IS LOAD-BEARING: nothing above `COPY src/` may depend on src/, so
# a code-only change reuses the deps + model layers instead of re-downloading
# ~2GB of wheels and the 420MB model on every bug fix. requirements.txt's `-e .`
# is the reason this is not just "copy requirements, install": it needs src/
# present for find_packages() to resolve. So strip it here and install it below,
# after the source lands, with --no-deps (seconds, re-resolves nothing).
COPY requirements.txt setup.py ./
RUN sed '/^-e[[:space:]]*\./d' requirements.txt > /tmp/deps.txt \
    && pip install --no-cache-dir -r /tmp/deps.txt \
    && rm /tmp/deps.txt

# Bake the local embedding model (~420MB) into the image so the container never
# downloads it from huggingface.co at startup. Without this, every cold start /
# new revision would re-fetch it — slow, and a hard dependency on an external
# host some deployment networks block. HF_HOME points the cache at a path the
# non-root runtime user can read; load_local_embeddings() reads it offline-first
# (local_files_only) and degrades to a runtime download only if it's ever absent.
ENV HF_HOME=/opt/hf
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('sentence-transformers/all-mpnet-base-v2')" \
    && chown -R documind:documind /opt/hf

# ── Everything below rebuilds on any code change — keep it cheap. ────────────
# The app creates its own runtime directories on demand (tmp_uploads/ for
# in-flight uploads, logs/ for the rotating file handler) via mkdir(exist_ok=True)
# — owning /app is enough for documind to create them lazily, no need to
# pre-create or volume-mount them.
COPY src/ ./src/
RUN pip install --no-cache-dir --no-deps -e . \
    && chown -R documind:documind /app
USER documind

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
