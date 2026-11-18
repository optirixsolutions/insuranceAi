# syntax=docker/dockerfile:1.7
# Build context: ./backend
FROM ghcr.io/astral-sh/uv:0.11.19 AS uv

FROM python:3.12-slim-trixie AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH=/opt/venv/bin:$PATH

COPY --from=uv /uv /usr/local/bin/uv

# OCR engine for the document worker: Tesseract with English and orientation/script detection.
RUN --mount=type=cache,target=/var/cache/apt,sharing=locked \
    --mount=type=cache,target=/var/lib/apt/lists,sharing=locked \
    rm -f /etc/apt/apt.conf.d/docker-clean \
    && apt-get update \
    && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-eng tesseract-ocr-osd \
    && tesseract --version

WORKDIR /app

COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-project

COPY . .

# Source stays root-owned and read-only; the service runs as an unprivileged user.
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin claimos
USER 10001

EXPOSE 8000
CMD ["uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-access-log", "--timeout-graceful-shutdown", "20"]
