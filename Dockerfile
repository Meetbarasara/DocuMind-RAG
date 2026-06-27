# DocuMind — one shared image for both the FastAPI backend and the Streamlit
# frontend. They have identical Python dependencies and live in the same repo,
# so docker-compose runs this image twice with a different `command:` per
# service (see docker-compose.yml) instead of maintaining two near-duplicate
# Dockerfiles.
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
COPY frontend/ ./frontend/

RUN pip install --no-cache-dir -r requirements.txt

# Non-root user (Part C hardening). The app creates its own runtime
# directories on demand (tmp_uploads/ for in-flight uploads, logs/ for the
# rotating file handler) via mkdir(exist_ok=True) — owning /app is enough for
# documind to create them lazily, no need to pre-create or volume-mount them.
RUN useradd --create-home --uid 1000 documind \
    && chown -R documind:documind /app
USER documind

EXPOSE 8000 8501

# Targets the API's /health; docker-compose overrides this for the frontend
# service, which has no listener on 8000 at all.
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD curl -f http://localhost:8000/health || exit 1

# docker-compose overrides this per service; this default just makes
# `docker run` on the image alone do something sane (start the API).
CMD ["uvicorn", "src.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
