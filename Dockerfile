FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install . \
 && useradd --system --uid 1000 --no-create-home app \
 && mkdir -p /app/data /app/config \
 && chown app /app/data /app/config

USER app
VOLUME ["/app/data"]
CMD ["jobalerts", "--config", "/app/config/config.yaml", "run"]
