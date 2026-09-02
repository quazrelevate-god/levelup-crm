# syntax=docker/dockerfile:1

# Local development image. Deployment images land in M11.
FROM python:3.12-slim-bookworm AS base

COPY --from=ghcr.io/astral-sh/uv:0.12 /uv /uvx /bin/

# The virtualenv lives outside /app because docker-compose bind-mounts the
# source over /app — a venv inside it would be shadowed at runtime.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app


FROM base AS dev

# Dependencies first, so edits to application code do not bust the layer cache.
COPY pyproject.toml uv.lock ./
# No BuildKit cache mount: Railway's builder requires the mount id to carry a
# platform-specific `s/<service id>-` prefix, and hardcoding one deployment's
# service id into a shared Dockerfile is worse than the rebuild it saves.
# Docker's layer cache still skips this whenever the lockfile is unchanged.
RUN \
    uv sync --frozen --no-install-project

COPY . .

RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--reload"]


# The deployment image the header comment promised. Identical to `dev` in how
# it installs, and different in exactly two ways that matter:
#
# **No `--reload`.** The reloader watches the filesystem and forks a child per
# change — useful against a bind mount, pure overhead against a baked image,
# and a second process for a platform's health checker to get confused by.
#
# **`$PORT`, in shell form.** Railway (and Fly, Render, Cloud Run) inject the
# port to bind rather than letting the image choose. JSON-form CMD does not
# expand variables, so this is `sh -c`; `entrypoint.sh`'s `exec "$@"` hands
# over to it without leaving a shell in the process tree.
#
# The entrypoint still runs `alembic upgrade head` before the server starts,
# so a deploy migrates itself. That was already true; it just matters more here.
FROM base AS prod

COPY pyproject.toml uv.lock ./
# No BuildKit cache mount: Railway's builder requires the mount id to carry a
# platform-specific `s/<service id>-` prefix, and hardcoding one deployment's
# service id into a shared Dockerfile is worse than the rebuild it saves.
# Docker's layer cache still skips this whenever the lockfile is unchanged.
RUN \
    uv sync --frozen --no-install-project

COPY . .

RUN chmod +x /app/scripts/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/scripts/entrypoint.sh"]
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
