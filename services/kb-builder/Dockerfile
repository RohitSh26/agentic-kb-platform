# Nightly KB build plane. Owns the Postgres schema: this image is the only
# place migrations run. The nightly build entrypoint is wired by the pipeline
# (GitHub Actions) per docs/runbooks; the default command applies migrations.
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-install-project --no-dev
COPY src ./src
COPY migrations ./migrations
COPY alembic.ini ./alembic.ini
RUN uv sync --frozen --no-dev

FROM python:3.12-slim-bookworm
WORKDIR /app
RUN useradd --create-home appuser
USER appuser
COPY --from=builder /app/.venv /app/.venv
COPY --from=builder /app/src /app/src
COPY --from=builder /app/migrations /app/migrations
COPY --from=builder /app/alembic.ini /app/alembic.ini
ENV PATH="/app/.venv/bin:$PATH"
CMD ["alembic", "upgrade", "head"]
