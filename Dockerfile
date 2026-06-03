# syntax=docker/dockerfile:1

# GitModel Hub container image.
FROM python:3.13-slim

# Faster, cleaner Python in containers.
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

# Install dependencies first for better layer caching.
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Copy application code.
COPY hub ./hub

# Persist the SQLite DB (Copilot token, admin creds, usage) outside the layer.
ENV HUB_DATA_DIR=/data
VOLUME ["/data"]

# Bind to all interfaces inside the container; map the port on the host.
ENV HUB_HOST=0.0.0.0 \
    HUB_PORT=8088

EXPOSE 8088

CMD ["python", "-m", "hub"]
