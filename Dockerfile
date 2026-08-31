# The game server and the driver. Build context is the PARENT directory, so
# the engine at ../magic-cards-model (a path dependency) is in scope.
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    UV_PYTHON_DOWNLOADS=never

RUN pip install --no-cache-dir uv

WORKDIR /app
COPY magic-cards-model /magic-cards-model
COPY magic-cards-model-api /app

RUN uv sync --frozen --no-dev || uv sync --no-dev

ENV PATH="/app/.venv/bin:${PATH}"

EXPOSE 8000
CMD ["uvicorn", "magic_cards_model_api.main:app", "--host", "0.0.0.0", "--port", "8000"]
