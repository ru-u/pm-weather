# Lean image for the always-on market logger and the small cron jobs.
# Installs ONLY the core dependencies (httpx, pyarrow, pydantic, typer, ...).
# No pandas, xarray, cfgrib or herbie: those stay on the laptop.
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
WORKDIR /app
RUN useradd --create-home --uid 1000 pmw
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/src /app/src
COPY configs ./configs
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 PMW_CONFIG_DIR=/app/configs \
    MALLOC_ARENA_MAX=2
USER pmw
# Default process: the market logger. Cron services override the start command.
CMD ["pmw", "log"]
