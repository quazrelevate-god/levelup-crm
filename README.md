# Configurable CRM

A multi-tenant, fully configurable sales CRM. Every workspace defines its own
lead fields, pipeline stages, call dispositions, custom actions and permission
templates through the settings UI at runtime — **the codebase ships no business
taxonomy of its own.**

New to the project? Read [`docs/03-configuration-model.md`](docs/03-configuration-model.md),
the authoritative product spec. The rules that must never be broken live in
[`CLAUDE.md`](CLAUDE.md). To get it running on a fresh machine, follow
[`docs/08-handoff-voice-agent.md`](docs/08-handoff-voice-agent.md) §2–3 — it is
written for Windows but the steps are the same everywhere.

**Current status: M0–M11 complete.** Every milestone in
[`docs/00-milestones.md`](docs/00-milestones.md) has landed: tenancy, the field
engine, the pipeline, permissions, leads, filters, tasks and undo, assignment and
scheduling, dashboards, the intake API and event bus, and hardening. 706 backend
tests and 139 Playwright specs.

Deployment is deliberately **not** built — both Dockerfiles carry a "deployment
images land in M11" comment that now outlives the milestone. Local development is
fully working.

Next up is the Bolna voice-agent integration on `feat/voice-agent`, specified in
[`docs/06-voice-integration-contract.md`](docs/06-voice-integration-contract.md).

---

## Stack

| Layer | Choice |
|---|---|
| Backend | Python 3.12 · FastAPI · Pydantic v2 · pydantic-settings |
| ORM / migrations | SQLAlchemy 2.0 (async) + Alembic |
| Database | PostgreSQL 16 |
| Cache / queue | Redis 7 (background work lands with `arq` later) |
| Object storage | S3-compatible — MinIO locally |
| Frontend | React 18 · TypeScript · Vite · Tailwind CSS v4 · shadcn/ui |
| Data fetching | TanStack Query v5 |
| Backend tests | pytest · pytest-asyncio · httpx · testcontainers |
| E2E | Playwright |
| Lint / format | ruff · mypy (strict) · eslint · prettier · pre-commit |

Dependencies are managed with **uv** (Python) and **pnpm** (frontend).

---

## Prerequisites

| Tool | Version | Needed for |
|---|---|---|
| [Docker](https://docs.docker.com/get-docker/) + Compose | v2+ | the whole stack, and the test containers |
| [uv](https://docs.astral.sh/uv/) | 0.5+ | the API outside Docker |
| [Node.js](https://nodejs.org/) | 22+ | the web app outside Docker |
| [pnpm](https://pnpm.io/) | 11+ | `corepack enable` provides it |

Docker alone is enough for the quick start.

---

## Quick start

```bash
git clone <repo> && cd configurable-crm
cp .env.example .env          # optional — compose has local defaults for every value
docker compose up --build
```

That brings up Postgres, Redis, MinIO, the API and the web app. Wait for the API
health check to go green, then:

| What | Where |
|---|---|
| Web app (system status page) | http://localhost:5173 |
| API health | http://localhost:8000/health |
| API docs (local only) | http://localhost:8000/docs |
| MinIO console | http://localhost:9001 |

A green `/health` looks like this:

```json
{
  "status": "ok",
  "service": "Configurable CRM API",
  "version": "0.1.0",
  "environment": "local",
  "checks": {
    "database":       { "status": "ok", "latency_ms": 3.1,  "error": null },
    "redis":          { "status": "ok", "latency_ms": 1.4,  "error": null },
    "object_storage": { "status": "ok", "latency_ms": 12.7, "error": null }
  }
}
```

If any backing service fails its probe the endpoint returns **503** with
`"status": "degraded"` and the failure detail on the offending component, rather
than a bare error — so the status page can tell you *which* dependency is down.
Error text is redacted outside `local`/`test`, because driver errors can echo a
DSN.

`/health` is mounted twice: at `/health` for container and load-balancer probes,
and at `/api/v1/health` for API clients. Only the versioned one appears in the
OpenAPI schema.

Stop and wipe:

```bash
docker compose down --volumes
```

---

## Developing outside Docker

Keep the infrastructure in Docker and run the app processes on the host.

```bash
cp .env.example .env
docker compose up -d postgres redis minio minio-init
```

**API** — `.env` already points `DATABASE_URL`, `REDIS_URL` and `S3_ENDPOINT_URL`
at `localhost`:

```bash
cd api
uv sync
uv run alembic upgrade head          # M1: tenancy, auth and user lifecycle
uv run uvicorn app.main:app --reload
```

**Web:**

```bash
cd web
pnpm install
pnpm dev
```

---

## Checks

Everything CI runs, runnable locally:

```bash
cd api && uv run ruff check . && uv run ruff format --check . && uv run mypy app && uv run pytest
cd web && pnpm lint && pnpm format:check && pnpm typecheck && pnpm test:e2e
```

`pytest` starts a real **Postgres 16 container** via testcontainers — Docker must
be running. SQLite is not an option here: JSONB and expression indexes, which the
data model leans on from M2 onward, do not behave the same way.

Without Docker, point the suite at any Postgres 16+ you already have:

```bash
createdb crm_test
cd api && TEST_DATABASE_URL=postgresql+asyncpg://user:pass@localhost:5432/crm_test uv run pytest
```

CI leaves `TEST_DATABASE_URL` unset so it always runs against the pinned
Postgres 16 image.

The migration is checked separately — it must apply, match the models, and roll
back:

```bash
cd api
uv run alembic upgrade head
uv run alembic check        # fails if the models and the revision have drifted
uv run alembic downgrade base
```

Playwright needs its browser once per machine:

```bash
cd web && pnpm exec playwright install chromium
```

### Pre-commit hooks

```bash
uvx pre-commit install          # once per clone
uvx pre-commit run --all-files  # ad hoc
```

The hooks shell out to the project's own toolchain, so tool versions have a
single source of truth in `api/pyproject.toml` and `web/package.json`.

### CI

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs three jobs on every
push and pull request:

| Job | What it proves |
|---|---|
| `api` | ruff, ruff format, mypy strict, pytest against a real Postgres |
| `web` | eslint, prettier, `tsc --noEmit`, production build, Playwright |
| `stack` | `docker compose up` from a clean checkout, then `/health` green |

---

## Layout

```
/
├── CLAUDE.md                  rules for contributors and coding agents
├── EXPLAIN-SIMPLE.md          plain-English overview
├── PROMPTS.md · START-HERE.md build process
├── docker-compose.yml         postgres · redis · minio · api · web
├── .env.example               every environment variable, with local defaults
├── docs/                      the specification — 00-milestones is the plan
├── api/
│   ├── app/
│   │   ├── main.py            app factory + lifespan
│   │   ├── config.py          pydantic-settings; no hardcoded URLs or secrets
│   │   ├── db.py              async engine, session factory, declarative base
│   │   ├── cache.py           Redis client
│   │   ├── storage.py         S3-compatible client
│   │   ├── dependencies.py    shared FastAPI dependencies
│   │   ├── routers/           thin HTTP layer
│   │   ├── schemas/           Pydantic request/response models
│   │   └── services/          business logic
│   ├── alembic/               migrations — one revision per milestone
│   └── tests/
└── web/
    └── src/
        ├── api/               fetch client and typed responses
        ├── components/ui/     shadcn/ui primitives
        ├── lib/               helpers
        └── routes/            pages
```

Directories that arrive with later milestones — `tenancy/`, `auth/`, `fields/`,
`models/`, `permissions/`, `events/`, `seed/` on the API side, and `features/` on
the web side — are laid out in `CLAUDE.md`. They are deliberately absent until
the milestone that needs them.

---

## Configuration

All configuration is environment-driven through `pydantic-settings`; there are no
hardcoded URLs or secrets. [`.env.example`](.env.example) documents every
variable. `DATABASE_URL` must use the `postgresql+asyncpg://` driver — the
engine is async, and a sync DSN is rejected at startup rather than failing
confusingly at the first query.

Note what is **not** here: a workspace's country code, timezone, currency and
taxonomy are database rows, not environment variables. Nothing about any
customer's business belongs in configuration files either.

---

## Conventions

- **API paths** — `/api/v1/workspaces/{workspace_id}/{resource}` for tenant data;
  `/api/v1/auth/*` and `/api/v1/me/*` are unscoped
- **Python** — snake_case, full type hints, mypy strict on `app/`
- **TypeScript** — no `any`; types derive from the API's OpenAPI schema
- **Migrations** — one Alembic revision per milestone; never edit an applied one,
  and never generate DDL at runtime
- **Errors** — `HTTPException` with `detail.code` and `detail.message`
- **Commits** — conventional commits, one logical unit each
- **Never commit** `.env`, real customer data, or any real phone number or email

The full set, including the architecture rules that are not negotiable, is in
[`CLAUDE.md`](CLAUDE.md).
