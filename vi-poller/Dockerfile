# syntax=docker/dockerfile:1
#
# MomentLens vi-poller: a manually triggered Azure Container Apps job that polls Azure AI Video Indexer
# until a video is processed and stores the insights JSON in Blob Storage. Build from the repository root:
#
#   docker build -f vi-poller/Dockerfile -t momentlens/vi-poller:local .
ARG PYTHON_IMAGE=python:3.12-slim
FROM ${PYTHON_IMAGE}

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1
WORKDIR /vi-poller

COPY vi-poller/requirements.txt .
RUN pip install -r requirements.txt

COPY vi-poller/main.py .

RUN useradd --system --uid 10001 --no-create-home appuser
USER appuser

CMD ["python3", "/vi-poller/main.py"]
