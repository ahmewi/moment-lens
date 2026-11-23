# syntax=docker/dockerfile:1
#
# MomentLens web app: builds the React UI, then bakes it into the FastAPI image
# (FastAPI serves it as static files). Build from the repository root:
#
#   docker build -f api/Dockerfile -t momentlens/api:local .
#
# The base images are build arguments so that a private mirror (for example an
# Azure Container Registry cache, see scripts/deploy.sh) can replace the public
# images without editing this file.
ARG NODE_IMAGE=node:20-alpine
ARG PYTHON_IMAGE=python:3.12-slim

FROM ${NODE_IMAGE} AS ui-build
WORKDIR /ui
COPY ui/package.json ui/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ui/ ./
RUN npm run build -- --logLevel error

FROM ${PYTHON_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    DEBIAN_FRONTEND=noninteractive
WORKDIR /app

# ffmpeg extracts still frames from the source video (visual enrichment, search-time confirmation).
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && ffmpeg -version | head -n 1

COPY api/pyproject.toml ./pyproject.toml
COPY api/app ./app
RUN pip install .

# Built UI, served as static files by FastAPI.
COPY --from=ui-build /ui/dist ./app/static

RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

EXPOSE 8000
# A single worker: job state is mirrored to Azure Table Storage, but search-time work is parallelised in-process.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
