# Pinned base keeps the image reproducible across rebuilds.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /usr/local/bin/uv

# Create the unprivileged user up front and build as that user, so the venv is
# written with the right ownership instead of being duplicated by a later
# recursive chown (which would add a second full copy of the venv as a layer).
RUN useradd --create-home --uid 10001 skyjam
WORKDIR /app
RUN chown skyjam:skyjam /app
USER skyjam

# Dependency layer first so code changes do not invalidate the install cache.
COPY --chown=skyjam:skyjam pyproject.toml uv.lock README.md ./
RUN uv sync --locked --no-install-project --no-dev

COPY --chown=skyjam:skyjam src/ ./src/
RUN uv sync --locked --no-dev

ENV PATH="/app/.venv/bin:$PATH"

# The Python image covers the read-side pipelines (features, and later
# training and inference). Capture runs from services/ingestor, which has its
# own much smaller Go image.
CMD ["skyjam-features"]
