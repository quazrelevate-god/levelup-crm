# 08 — Handoff: the voice-agent branch, on Windows

For the developer picking up the Bolna integration on a fresh Windows machine.
Everything below has been run; where a step is untested on Windows it says so
rather than pretending.

**Branch: `feat/voice-agent`.** It is identical to `main` today — both sit at
`b672277` — and exists so integration work does not have to wait on, or collide
with, whatever happens next on `main`.

---

## 1. What you are joining

A multi-tenant configurable CRM, **M0–M11 complete**. Roughly:

- Every lead field, pipeline stage, call disposition, custom action and
  permission set is created by a workspace admin at runtime. The codebase ships
  no business vocabulary of its own — that is the product, and the thing most
  easily broken by accident. `CLAUDE.md` is the standing rulebook.
- Field-level permissions (View / Edit / Import / Export) apply to **every** read
  and write path, including exports and webhooks.
- Every mutation opens a changeset, so it can be undone as a unit.
- 706 backend tests, 139 Playwright, plus 5 live ones that need a real database.

**Your job:** the CRM half of `docs/06-voice-integration-contract.md`. Read that
document before any code. Sections 3–6 are frozen — they are the agreement
between the Bolna side and this side.

Everything the integration attaches to already exists: API keys carrying a
permission template, a transactional outbox with HMAC signing and retry, an
intake API, and `ChangesetSource.AUTOMATION` sitting unused so an AI call's
write-back is undoable in one click.

---

## 2. Prerequisites

| Tool | Version | Notes |
|---|---|---|
| **Docker Desktop** | current | With the WSL 2 backend. Postgres, Redis and MinIO all run in it. |
| **Git** | current | Enable **"Checkout as-is, commit Unix-style"** during install, or set `core.autocrlf=false`. |
| **Python** | 3.12.x | Pinned `>=3.12,<3.13`. `uv` can install it for you. |
| **uv** | current | `powershell -c "irm https://astral.sh/uv/install.ps1 \| iex"` |
| **Node** | ≥ 22 | |
| **pnpm** | 11.22.0 | `corepack enable` then `corepack prepare pnpm@11.22.0 --activate` |

`.gitattributes` already forces LF on every text file, including the shell
scripts, so the usual CRLF trap is closed **provided you did not set
`core.autocrlf=true` globally before cloning.** If the API container ever fails
with `no such file or directory` on a file that plainly exists, that is this,
and the fix is `git config core.autocrlf false` then a fresh clone.

### Shell scripts

`ops/*.sh` are POSIX. Run them from **Git Bash** or WSL, not PowerShell. They are
operational conveniences — backups, the E2E fixture — not required to develop.

---

## 3. Getting it running

```bash
git clone https://github.com/sakthivel-2007-eng/configurable-crm.git
cd configurable-crm
git checkout feat/voice-agent
```

### 3.1 Environment

```bash
cp .env.example .env
```

Then **edit `.env`**:

- `JWT_SECRET_KEY` — replace it. Compose refuses to start without one, on
  purpose: an unset signing key must stop the container rather than sign tokens
  with something predictable.
- `POSTGRES_PORT` — the default 5432 collides with a natively installed
  Postgres, which is common on Windows. If `docker compose up` reports the port
  in use, change **both** `POSTGRES_PORT` and the port inside `DATABASE_URL`.

**Also copy it to `api/.env`:**

```bash
cp .env api/.env
```

Not a symlink — those do not survive a Windows checkout without developer mode.
This second copy exists because `pydantic-settings` resolves `env_file=".env"`
relative to the working directory, and you will be running `uv run` from `api/`.
Two copies is a wart; keep them in step.

### 3.2 The stack

```bash
docker compose up -d --build --wait
curl http://localhost:8000/health
```

Expect `"status": "ok"` with `database`, `redis` and `object_storage` all `ok`.
Anything else names the failing dependency directly.

That gives you the API on `:8000` and the web app on `:5173` — both with live
reload against your working tree.

### 3.3 Your first account

**There is no sign-up.** The workspace model is invite-only: an account exists
because somebody with an account invited it. A fresh database has nobody, so one
command opens the loop:

```bash
cd api
uv sync
uv run alembic upgrade head
uv run python -m app.bootstrap --email you@example.com --name "Your Name" --workspace "Dev"
```

It prints a single-use link. Open it, choose a password, sign in at
`http://localhost:5173`.

It refuses if the database already has users; `--force` is for a genuine second
workspace.

### 3.4 Demo data (optional, recommended)

```bash
cd api
ENVIRONMENT=local uv run python -m app.seed
```

50,000 leads across a fictional tutoring business, in about 15 seconds. **It is a
fixture, not a default** — its vocabulary ("Demo Booked", "Guardian") is there to
exercise the engine and must never be mistaken for something the product ships.
Seeded users cannot log in; use your bootstrap account.

---

## 4. Running the tests

```bash
cd api && uv run pytest -q                      # 706 tests, ~9 minutes
cd api && uv run pytest -q -m "not performance" # faster edit loop
cd web && pnpm install && pnpm test:e2e         # 139 Playwright
```

Backend tests start their own Postgres through **testcontainers**, so Docker
Desktop must be running. They do not touch your dev database.

> **If testcontainers cannot find Docker on Windows**, set
> `TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE` to the socket Docker Desktop exposes.
> On this project's macOS machine that was `/var/run/docker.sock`; the Windows
> equivalent is usually `npipe:////./pipe/docker_engine` and **has not been
> verified here** — if it bites, that is the knob.

Before committing:

```bash
cd api && uv run ruff check . && uv run ruff format --check . && uv run mypy app
cd web && pnpm lint && pnpm tsc --noEmit && pnpm format:check
```

---

## 5. What is already built for you

| Piece | Where |
|---|---|
| API keys, each carrying a permission template | `app/auth/api_keys.py`, `app/models/integration.py` |
| Transactional outbox, HMAC, retry, DEAD after 8 | `app/events/outbox.py`, `app/events/dispatcher.py` |
| Intake API — unknown fields accepted, never rejected | `app/events/intake.py`, `app/routers/intake.py` |
| Field projection and write filtering | `app/permissions/projection.py` |
| Changesets and undo | `app/services/actions.py`, `app/services/undo.py` |
| The `arq` worker (scheduler + outbox dispatch) | `app/workers/scheduler.py` |
| Settings UI for keys, webhooks, the queue, the intake log | `web/src/routes/IntegrationsPage.tsx` |
| 19 Bolna skills for Claude Code | `.claude/skills/` |

Create an API key and a webhook from **Settings → Integrations** in the running
app. Both secrets are shown exactly once.

---

## 6. What is left to build

From `docs/06-voice-integration-contract.md`:

1. **`voice_extraction_mappings`** (§6.1) — the table mapping a Bolna disposition
   name to a lead field, stage or action field, with a confidence threshold, plus
   its settings screen. This is what keeps the customer's vocabulary out of
   product code, so it is the piece most worth getting right.
2. **The trigger** — build `user_data` from a lead's values *through the
   projection service*, and enqueue via the outbox. Never call Bolna from a
   request handler (architecture rule 8).
3. **The receiver** — `POST /voice/executions`, idempotent on `execution_id`,
   opening one changeset with `source = AUTOMATION` so the whole call's
   write-back undoes in a single click.

### Two things from the contract worth repeating

**Per-lead detail goes in Bolna's `user_data`, not a knowledge base.** A KB is a
RAG store over documents — uploaded, polled until processed, deleted explicitly.
One per lead would mean an async ingest before every call and a durable
third-party copy of that person's data. The KB stays static per workspace.

**Confidence gates the write.** Below the mapped threshold, the extracted value
goes on the timeline, not into the field. A probabilistic extractor silently
overwriting a figure a human typed is the failure that would cost operators'
trust in the whole feature.

### Still undecided (§9)

- What triggers a call — manual, assignment rule, stage entry, or campaign. The
  contract assumes manual-plus-API for v1.
- Per-workspace concurrency cap.
- Whether a failed call creates a follow-up task.

Settled and **out of scope**: the CRM does not store call recordings, and there
is no calling-hours or DNC enforcement.

---

## 7. Things that will look like bugs and are not

Worth knowing before you file one:

- **404, not 403, for another workspace's record.** A 403 would confirm the id
  exists.
- **An unknown field accepted by intake.** Deliberate — a rejected payload at 2am
  is a lost lead. It surfaces as a warning in the intake log.
- **A webhook delivered twice.** Delivery is at-least-once; `X-CRM-Event-Id` is
  stable across retries so consumers dedupe on it.
- **A field missing from an export.** Field permissions apply to every read path.
- **Undo refusing to proceed.** A lead changed since the batch is reported rather
  than clobbered.
- **`POST /settings/stages` ignoring a `kind` you send.** Only ACTIVE stages can
  be created; the response says what you actually got.

---

## 8. Known open items

- **Deployment is not built.** Both Dockerfiles say "deployment images land in
  M11"; that was deferred. Local development is fully working.
- **One CI job is red.** `Stack — docker compose up` fails after building. Four
  causes were found and fixed; one remains unidentified because reading the
  runner's logs needs repository access. The other two jobs pass.
- **`README.md` and `START-HERE.md` are stale** — they describe M0 and link to a
  file that has been deleted. `docs/` is current; those two are not.

---

## 9. Where to read next

| Document | Why |
|---|---|
| `docs/06-voice-integration-contract.md` | **Start here.** Your specification. |
| `CLAUDE.md` | The rules. Read before writing code. |
| `docs/03-configuration-model.md` | The authoritative product spec. |
| `docs/07-runbook.md` | Operating it — logs, the outbox, the intake log. |
| `docs/05-handoff-m6-m10.md` | What each milestone settled, and the traps found. |
| `docs/02-api-contract.md` | Endpoint reference. |
