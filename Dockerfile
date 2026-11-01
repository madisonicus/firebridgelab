# Self-hosted web UI + API for firebridgelab.
#   podman build -t firebridgelab .      (or docker build)
#   podman run -d -p 8000:8000 firebridgelab
# then open http://127.0.0.1:8000/
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    FIREBRIDGELAB_DATA=/data

WORKDIR /app

# Library + web dependencies (pinned in requirements.txt).
COPY requirements.txt pyproject.toml README.md LICENSE ./
COPY firebridgelab ./firebridgelab
RUN pip install -r requirements.txt && pip install ".[web]"

# Download the freely redistributable data and build the staged series at
# build time. The optional non-redistributable classes are skipped.
RUN firebridgelab-fetch-data

COPY api ./api
COPY web ./web

# Run unprivileged; saved scenarios go to /app/scenarios (mount a volume there
# to keep them across container rebuilds).
RUN useradd --create-home --uid 10001 modeler \
    && mkdir -p /app/scenarios \
    && chown modeler /app/scenarios
USER modeler

EXPOSE 8000
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8000"]
