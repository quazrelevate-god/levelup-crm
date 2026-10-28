# Handoff — pending work, M6 to M10

**Written 20 Aug 2026, after M5 landed (`19f05e7`).**

This document is for a session starting fresh on this repository. It is not a
substitute for the specs — `00-milestones.md` says *what* to build and
`02-api-contract.md` says *what shape*. This says **where the code actually is**,
**what is already broken**, and **which traps in M6–M10 have already bitten
someone**.

Read `CLAUDE.md` and `03-configuration-model.md` first. Then this.

---

## 0. Read this before writing any code

Two defects are in `main` right now. Both are small to fix and both are the kind
that get worse the longer they sit.

> **Update, 21 Aug 2026 — both are fixed.** §0.1 in `4c07390`, §0.2 in `5f14730`.
> The diagnoses below are left as written because they still explain *why* the
> code looks the way it does; only the "Fix:" instructions are spent. Backend
> suite is 381 green. Two things found while fixing §0.2 are worth carrying
> forward, both now handled in `app/workers/indexing.py`:
>
> - `CREATE INDEX CONCURRENTLY` waits for **every** transaction older than
>   itself. One client sitting idle-in-transaction stalls the build
>   indefinitely rather than failing it. There is a `lock_timeout` on the build
>   connection now, so a blocked build lands in `FAILED` where an admin can see
>   it. The same rule bites in tests: a fixture holding an open transaction
>   makes an index test *hang*, not fail.
> - A failed build leaves the index behind marked `INVALID`, and the statement
>   says `IF NOT EXISTS` — so without cleanup every retry would skip creation
>   and report success over an index Postgres will not use.

### 0.1 `deactivate` no longer protects the pipeline — **fix this first**

M1's headline guarantee is *"deactivating a member requires reassignment; never
orphan a pipeline."* It is currently **inert**.

`app/services/lead_ownership.py` defines a `LeadOwnership` protocol with a
`NullLeadOwnership` that honestly returns `0` open leads. That was correct in M1,
when `leads` did not exist. M5 created the table and **never registered a real
implementation** — `set_lead_ownership` is not called anywhere in `app/`.

So today: deactivating a member with 500 assigned leads returns `200`, and those
500 leads keep an `assignee_id` pointing at an inactive membership. The
`409 reassignment_required` path is unreachable, and
`test_deactivation_refuses_when_the_member_holds_open_leads` passes only because
the test injects a fake.

**Fix:** implement `LeadOwnership` against `leads` (open = assigned to the member
and `stage.kind NOT IN ('WON','LOST')` and `deleted_at IS NULL`), register it at
startup, and add a test using real leads rather than the fake. Roughly an hour.
Do it before M6 — M6 makes leads far easier to create in bulk, which makes the
orphaning easier to hit.

### 0.2 The indexed-field worker is written but never called

`app/workers/indexing.py` is complete and correct: `CREATE INDEX CONCURRENTLY`,
generated safe names, `AUTOCOMMIT`. Nothing invokes it. `FieldService
.declare_indexed` writes an `indexed_fields` row with `status='PENDING'` and
returns; the status never becomes `READY` and no index is ever built.

This matters for M6 specifically: sorting is restricted to indexed fields, so
M6's sort path will pass its unit tests against a `PENDING` row while the
underlying index does not exist, and the 50k-lead performance target will not be
met.

**Fix:** wire the worker. There is no `arq` worker process yet, so M6 either
brings one up (preferred — M8 needs a scheduler anyway) or calls `build_index`
from a background task. Either way `status` must reach `READY`/`FAILED`, and
`undeclare_indexed` must call `drop_index`.

### 0.3 Smaller carried-forward items

| Item | Where | Note |
|---|---|---|
| `POST /auth/refresh` rate limiting | `app/routers/auth.py` | Implemented — this one is **done**, listed so it is not re-reported |
| Hierarchy narrowing is partial | `app/routers/members.py` | `/members` narrows to `visible_membership_ids`; `/members/{id}`, `/availability-log` and `/hierarchy` do not. Intra-workspace only, no cross-tenant leak. A product decision, not a bug — decide it. |
| `PATCH /members/{id}` renames a global user | `app/routers/members.py` | `full_name` lives on `users`, so an admin of A renames someone visible in B. Not a read leak. Decide it. |
| Frontend cannot edit template/manager | `web/src/routes/MembersPage.tsx` | The API supports both; the UI exposes neither |
| `GET /recurring-dates/occurrences` | absent | Spec'd in M2, needs `leads`, never built. M8's greetings depend on it |
| Field `config` editing | `web/.../FieldDrawer.tsx` | Registry serves `config_schema` per type; the drawer ignores it. So `NUMBER` min/max and `DEPENDENT_DROPDOWN.parent_field_id` cannot be set from the UI |
| "Set up your lead view" editor | absent | `useLeadView`/`useSetLeadView` hooks and the API exist; no drag surface |
| `FILE` action-field upload | absent | Type and validator exist; no `POST /actions/{id}/attachments` |
| E2E runs against a stub | `web/e2e/fixtures/api.ts` | 52 tests, all hermetic. The frontend↔backend seam has never been exercised |

---

## 1. Where the code is

### Shipped: M0–M5

| | Status | Commit |
|---|---|---|
| M0 scaffold | done | `82cb725` |
| M1 tenancy, auth, user lifecycle | done (see 0.1) | `a205554` |
| M2 field engine | backend done, UI done | `541398b` / `19f05e7` |
| M3 pipeline & taxonomy | backend done, UI done | same |
| M4 permissions | backend done, UI done | same |
| M5 leads, actions, changesets | backend done, UI done | same |

**22 tables. 5 Alembic revisions, one per milestone, linear, rebuilds from base
with zero drift. 95 API operations, 86 of them tenant-scoped. 374 backend tests
(146 cross-workspace isolation). 52 Playwright tests.**

### The four things that already exist and must be reused, not rebuilt

1. **`ScopedSession`** (`app/tenancy/session.py`) — a session that cannot address
   another tenant. Tenant repositories take it; they never take `AsyncSession`.
   Three enforcement layers: the type, `get()` matching id *and* workspace, and a
   `do_orm_execute` listener applying `with_loader_criteria` to every
   `TenantScoped` model including lazy loads. **Do not add an `AsyncSession`
   path into tenant code.**

2. **The type registries** (`app/fields/registry.py`) — 13 lead types, 8 action
   types, each declaring validation, normalisation, JSONB shape, **filter
   operators** and a renderer contract. M6's filter builder must read
   `spec.operators` rather than hardcoding which operators a type offers.

3. **`FieldProjectionService` / `FieldWriteFilter`** (`app/permissions/`) — the
   single read and write chokepoints. Every new read path in M6–M10 goes through
   projection: list, export, webhook payload, report row, template render. There
   is no "internal" caller that skips it.

4. **`ActionWriter`** (`app/services/actions.py`) — opens changesets and appends
   actions. Every mutation in M7/M8/M10 opens a changeset through this. It does
   not commit; the caller owns the transaction boundary, which is what lets a
   lead update and its four actions land atomically.

### Already built for later milestones

- **`actions_status_change_idx` and `actions_assignment_idx`** — the expression
  indexes over `STAGE_CHANGE` / `ASSIGNMENT_CHANGE` payloads. Built in M5's
  migration precisely so M6 need not retrofit them. M6's transition filters
  **must** query `payload->>'old_stage_id'` / `'new_stage_id'` to use them.
- **`changesets`** — every M5 mutation already opens one and stamps every action
  with its id. M7's undo has the record it needs from day one.
- **`score_applied`** — snapshotted per action, so M9's scoring reports read
  history rather than current settings.
- **`message_templates`** — M8's greetings render through these.
- **`Capabilities`** (`app/permissions/capabilities.py`) — all 10 Access and 3
  View groups. Nine Access groups and all 3 View groups are marked `proposed`
  because §8 lists them "Not inspected". M7–M10 should **check the proposed
  contents against the real product** before relying on a capability name.

---

## 2. Running it

Docker is unreliable on the machine this was built on; the local path is the one
that works.

```bash
# Backing services (Postgres 16, Redis, MinIO)
cd configurable-crm && cp .env.example .env   # then set JWT_SECRET_KEY
docker compose up -d postgres redis minio minio-init
```

If Docker will not start, use a local PostgreSQL. **Note the port:** a local
PostgreSQL on 5432 will shadow the container, so `.env` here sets
`POSTGRES_PORT=5433` and `DATABASE_URL` to match.

```bash
cd api
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload      # http://localhost:8000

cd ../web
pnpm install
pnpm dev                                  # http://localhost:5173
```

**`api/.env` is required and separate from the repo-root `.env`** —
pydantic-settings resolves `env_file` relative to the process CWD, and the API
runs from `api/`. Worth fixing properly at some point.

**There is no signup endpoint until M11.** `POST /workspaces` needs an
authenticated caller, so the first user must be created directly. Insert a
`users` row with an argon2 hash, then call `WorkspaceProvisioner.provision`.

Tests: `TEST_DATABASE_URL` overrides the testcontainers Postgres if Docker is
down. CI leaves it unset and gets the pinned PG16 image.

```bash
cd api && uv run ruff check . && uv run mypy app && uv run pytest
cd web && pnpm tsc --noEmit && pnpm lint && pnpm format:check && pnpm exec playwright test
```

---

## 3. M6 — Lead list, filters, history filters *(8–10 days)*

> **Update, 21 Aug 2026 — M6 has landed.** `3c6b2b4` (backend), `231d845`
> (demo seed + latency harness), `c7db934` (frontend), plus quick filters.
> Revision `0006_m6_filters`. 447 backend tests, 71 Playwright. Both acceptance
> checks work through the builder against the 50k workspace, and p95 is 27–62ms
> across seven query shapes against the 300ms budget.
>
> Four things the section below did not anticipate, all now settled:
>
> - **The DSL cannot express stage or assignee.** §6.1 defines a field rule as
>   referencing `lead_fields.key`, and those two are columns. They are *quick
>   filters* — query parameters beside the filter document, as
>   `02-api-contract.md` has them — not DSL nodes. Do not add a pseudo-field key
>   for them.
> - **The JSONB key must reach SQL as a literal.** Postgres matches an
>   expression index by comparing expression trees, so `values ->> $1` can never
>   use an index built on `(values ->> 'budget')`. `compiler._json_path`
>   re-validates the slug and interpolates it; values stay bound. Without this,
>   declaring a field indexed does nothing.
> - **Searchable is a property of the field *type*, not a toggle.** §1.4 lists
>   exactly four field properties. `spec.searchable` in the registry decides.
> - **`ScopedSession` stamps `session.info` permanently.** Wrapping a session
>   scopes every later query on it, including ones made after the scoped work
>   finishes. Use a fresh session for anything addressing another workspace.
> - **The runtime `ix_lv_…` indexes are invisible to Alembic**, by design —
>   they are the one sanctioned piece of runtime DDL (rule 7). M7 added an
>   `include_object` filter to `alembic/env.py` so autogenerate neither reports
>   them as drift nor writes a `drop_index` for them into the next revision.
>
> Not built, and deliberately: `saved_filters` reorder and duplicate exist on
> the API and have tests, but the UI exposes neither yet — the sidebar that
> would need them is M7's.

The spec is `00-milestones.md` M6 and `PROMPTS.md` M6. What follows is what the
spec does not tell you.

### What makes this milestone hard

`04-feature-coverage.md` calls history filtering **"the worst miss in the
audit"** — the original filter DSL only queried a lead's *current state*, and
four of the ten observed filters query its *history*. Retrofitting that after
the DSL ships means rewriting the compiler, so build both from the start.

### Build order that avoids rework

1. **The DSL types first**, field rules and history predicates together. If field
   rules ship first the compiler will grow a `WHERE`-only shape that history
   predicates then have to break.
2. **Compile to parameterised SQLAlchemy.** Never string interpolation — the DSL
   carries customer-supplied values.
3. **Operators come from `spec.operators`.** A `DATE` field offers temporal
   operators and a `TAGS` field offers set operators because the registry says
   so. The builder must not have its own table.
4. **Every filtered field must pass `projection.filterable(key)`** before it is
   compiled. Filtering on a field the caller cannot View is a read: `stage = X
   AND salary > 100000` returning a count is an oracle over a hidden field.
   `FieldProjectionService.filterable` already exists for this.

### The four history predicates

They compile to `EXISTS` / `NOT EXISTS` over `actions`. Shapes are in
`01-data-model.md` §6.1.

- `action_performed` — kind, action_type_id, actor, `min_count`, `within`
- `action_not_performed` — the "no outgoing call in 14 days" case
- `status_changed` — `from`/`to` both optional, `within`
- `assignee_changed` — same shape

`action_not_performed` is the one that will table-scan. PROMPTS.md asks for
`EXPLAIN ANALYZE` on it over the 50k demo workspace before calling M6 done —
that is a real requirement, not a formality.

### Prerequisites inside M6

- **The indexed-field worker must work** (§0.2). Sorting is restricted to
  built-in columns plus indexed fields, returning `400 field_not_indexed` with a
  message naming the setting that fixes it. That contract is meaningless while no
  index is ever built.
- **`leads.search_vector` does not exist yet.** `01-data-model.md` §4 specifies
  it (`tsvector` plus a GIN index), but M5 did not add it — the column is absent
  from both the model and revision `0005`. M6 owns adding it, recomputing it on
  write from searchable fields, and the trigram index on `identity_value`. That
  is a migration, so it belongs in M6's revision rather than an edit to `0005`.
- **The demo seed does not exist.** `01-data-model.md` §8 specifies
  `uv run python -m app.seed --workspace demo --leads 50000`, and the p95 target
  is measured against it. Building the seed is part of M6, not optional — a
  fictional "Northwind Tutors" workspace, never used to populate a real one.

### Frontend

`web/src/routes/LeadsPage.tsx` is currently a plain table with a search box. M6
replaces it with TanStack Table (already a dependency), server pagination, a
column picker, and a visual filter builder. **History predicates render as
first-class rules with their own controls — users never see raw JSON.**

`FieldInput` already renders all 13 types from the registry; the filter builder
should reuse the same dispatch for value inputs rather than growing its own.

### Done when

"Status went from HOT to Lost in the last 7 days" and "no outgoing call in 14
days" both work **through the builder**, and p95 < 300ms on the 50k workspace.

---

## 4. M7 — Tasks, bulk, import/export, undo *(7–9 days)*

> **Update, 21 Aug 2026 — M7 has landed.** `26b0e20` (bulk + undo), `3b055ba`
> (tasks, labels, imports), `9339ffa` (export, duplicates, merge), `2958832`
> (frontend). Revision `0007_m7_work`. 522 backend tests, 86 Playwright. Both
> acceptance checks pass: 300 leads bulk-edited and fully undone, and a
> historical action import producing a coherent timeline.
>
> Five things this milestone settled that the section below does not say:
>
> - **Conflict detection is per-value, not per-timestamp.** "Is the lead's
>   current value still the one the changeset set?" rather than "was this lead
>   touched since". Cheaper, more precise, and it correctly *permits* an undo
>   where somebody changed a value and changed it back.
> - **An imported action may only be a contact kind.** A sheet must not be able
>   to write a FIELD_CHANGE or STAGE_CHANGE — their payloads carry old/new
>   values that undo would replay against a history that never happened.
> - **`GET /leads/duplicates` does not group on identity.** `leads_identity_uq`
>   makes identity duplicates impossible, so the contract's literal wording
>   ships an always-empty screen. It groups on every phone and email the
>   workspace holds instead.
> - **`admin_access` only grants within groups a template names.** Root and
>   Admin named no `tasks` group, so every task endpoint 403'd — and Root is
>   `is_readonly`, so nobody could fix it from the UI. Provisioning is fixed and
>   0007 backfills existing workspaces. **Any future milestone adding a
>   capability group must do both.**
> - **`alembic autogenerate` wanted to drop the runtime `ix_lv_*` indexes**,
>   which would delete a live customer's index on deploy. `env.py` now filters
>   them, which also retires the drift caveat noted under M6.


### The part that carries risk: undo

Everything undo needs already exists — `changesets`, `changeset_id` on every
action, and old/new values in every `FIELD_CHANGE` payload. What M7 adds is the
replay.

The rule that is easy to get wrong: **a lead edited after the changeset is a
conflict.** Report it and let the operator choose. Never silently clobber a later
edit. `POST /changesets/{id}/preview-undo` returns per-lead status —
reversible / conflicted / already-undone — and `undo` takes `skip_conflicts`.

Only `FIELD_CHANGE`, `STAGE_CHANGE`, `ASSIGNMENT_CHANGE` and `RATING_CHANGE` are
reversible. An undo is itself a changeset with `undo_of_id` set — so undoing an
undo is just another undo, not a special case.

### Import

- Mapping offers only fields with **both** `Import` grant and `show_in_import`.
  `FieldWriteFilter.check_import` already exists.
- Dry-run preview shows create-versus-update counts before committing.
- The whole run is **one changeset**, so a bad import is one undo.
- **When the lead first came in.** Leads moved from another CRM arrive with a
  history. Mapping options `created_at_column` (a column in the file) and
  `created_at_format` (`iso`, `day_first` for `DD/MM/YYYY HH:MM:SS`, or
  `month_first`) date each *new* lead from that column instead of the import
  day. A value without a timezone is read in the workspace's timezone. An
  empty, unreadable or future date is a row error in the preview. A lead that
  already exists is never re-dated. Without the option, imports are dated as
  before. This is what the voice agent's "Enquiry received" line reads.
- **A hidden field cannot be imported into.** Hidden means retired: it is off
  the lead screen and refuses writes. To keep a column out of the CRM, leave it
  unmapped.
- Four distinct flows the audit found missing, all still missing: Excel Advance
  Distribution, Owner Specific Assignment, Excel Bulk Update, and Import Existing
  Actions (historical timeline migration — every customer switching CRMs needs
  it).

### Export

`projection.project_export` and `assert_can_export` exist and are unit-tested.
The endpoint does not. A template granting no Export field is refused outright
rather than producing an empty file — the observed default is `Export (0) None`,
a deliberate exfiltration control.

### Bulk edit

Capped at 500, permission-checked **per field**, one changeset per run.

### Done when

Bulk-edit 300 leads, undo it fully, and show the conflict path when one of them
was edited in between.

---

## 5. M8 — Assignment engine, sales groups, scheduler *(6–8 days)*

> **Update, 21 Aug 2026 — M8 has landed.** `8bd790e` (engine), `6ce7cdf`
> (groups, rules, distribution), `ef9ac65` (scheduler), `799a258` (frontend).
> Revision `0008_m8_assignment`. 588 backend tests, 100 Playwright.
>
> Acceptance verified live against the 50k Northwind Tutors workspace: with one
> rep marked On Leave, four consecutive leads all routed past them; with the rep
> back at WORKING the cursor alternated correctly; a scheduled report rendered
> and sent with no error. The intake-API half of the stated criterion is M10 —
> the rule runs in `create_lead`, which intake will call, so nothing further is
> needed there.
>
> Six things this milestone settled that the section below does not say:
>
> - **The concurrency test took three attempts to become real.** Racing two
>   asyncio tasks proves nothing — they ping-pong at await points and serialise
>   by accident. And with no cursor row yet, the `ON CONFLICT DO NOTHING` insert
>   takes its own lock and serialises them anyway. Both versions passed against
>   a deliberately broken engine. What works: seed the cursor, then assert the
>   property directly — a second transaction *blocks*, and on release picks the
>   next rep. **The safety is the locked upsert and `FOR UPDATE` together**, not
>   either alone.
> - **Rules compile with system grants, not the caller's.** Otherwise a rule on
>   a field the lead's creator cannot View silently stops matching, and identical
>   leads route differently depending on who typed them in.
> - **Unlicensed members are skipped unconditionally**, whatever
>   `skip_unavailable` says. A lead assigned to somebody who cannot log in looks
>   assigned on every report and is never called.
> - **Root and Admin named 4 of the 10 capability groups.** The M7 `tasks` trap,
>   still loaded for `automations` (M8, M10), `reports` (M9) and `integrations`
>   (M10). Root is `is_readonly`, so it was unfixable from the UI. Root now names
>   all ten, Admin all but `billings`, and two tests assert the coverage against
>   `ACCESS_GROUPS` so no future milestone can reintroduce it.
> - **`arq` is now a real process**, not a function call — `docker compose` gains
>   a `worker` service. M9 and M10 extend `app/workers/scheduler.py` rather than
>   adding another.
> - **The schedulable report catalogue is `leads` only.** M9 owns the rest and
>   extends `REPORT_TYPES`; the write path refuses an unknown type by name,
>   because a schedule for a report that does not exist is a row that fails every
>   morning.


`04-feature-coverage.md`: *"A telecalling CRM without automatic distribution is a
spreadsheet."* This is a subsystem, not a screen.

### The concurrency trap

Round-robin state lives in `assignment_cursors`. Under concurrent inserts a
read-then-write loses assignments — two leads arriving together both read the
same cursor and go to the same rep. **Use a row lock
(`SELECT ... FOR UPDATE`), not a read-then-write.** This is the single most
likely defect in M8 and it is invisible in single-threaded tests; write a
concurrent one.

### Everything else

- Rules are priority-ordered, first match wins, conditions use **the M6 filter
  DSL** evaluated against the new lead. Do not write a second condition language.
- `skip_unavailable` skips members whose `availability` is not `WORKING`. M1
  already models availability with a full log.
- Rules run on **every** create path: UI, import, intake. One place, called from
  three.
- `POST /settings/assignment-rules/preview` is a dry run that assigns nothing.
- `POST /leads/distribute` writes one changeset so redistribution can be undone.

### The scheduler

`arq` cron, evaluated in **the workspace timezone**, not the server's. Two
consumers: scheduled report email and recurring-date greetings.

Scheduled reports render **as the creating member**, so field permissions govern
what reaches the inbox. That is a projection call with someone else's grants —
`load_grants` takes a `template_id`, so this is supported, but it is easy to
forget and mail someone a column they cannot see in the UI.

Greetings need `GET /recurring-dates/occurrences`, which does not exist (§0.3).
The `next` derivation it depends on **does** exist and is tested — see
`_norm_recurring_date` in the registry.

If M6 stood up an `arq` worker for indexing, M8 extends it rather than adding a
second process.

---

## 6. M9 — Dashboards and reports *(6–8 days)*

> **Update, 21 Aug 2026 — M9 has landed.** `033d4a8` (backend), `80fa95f`
> (frontend). Revision `0010_m9_dashboards`. 665 backend tests, 131 Playwright.
> **All milestones through M10 are now done; only M11 remains.**
>
> Acceptance verified live against the 50k Northwind Tutors workspace: a
> dashboard bound to the Caller template, seen by a caller; and every one of the
> seven stage cells checked against the filtered list behind it — all seven
> agreeing exactly.
>
> Six things this milestone settled that the section below does not say:
>
> - **`leads-by-stage` and `funnel` take no date range.** A departure from the
>   contract's blanket "all take from/to", because that and "the drill-through
>   is exact" cannot both hold — the lead list cannot express "created between
>   these dates". Windowing a pipeline also changes what it means: "leads
>   created in the last 30 days that are now Won" is a cohort metric wearing a
>   pipeline metric's clothes. The range still governs activity, the leaderboard
>   and breakdowns.
> - **The chart and the list disagreed, twice over.** `ReportService` restated
>   the visibility rule instead of reusing it and dropped the "unassigned leads
>   are visible to everyone" half; and the drill-through navigated to a URL the
>   lead list never read. Both fixed — the rule is one function now, and
>   `LeadsPage` seeds its quick filters from the query string. **Neither was
>   caught by any test; both were caught by driving the real app.**
> - **The Caller template promised a dashboard its capabilities forbade.** The
>   View group has always had `show_personal_dashboard` while the Access groups
>   named no `reports` capability, so every dashboard endpoint would 403 for a
>   caller. Invisible until something was finally gated on `reports`. This is
>   the third milestone running to find a gap of this shape — **check both
>   halves when adding a capability.**
> - **Colour was computed, not chosen.** The dataviz validator rejected a
>   Won-green/Lost-red funnel at deutan ΔE 4.1. Every chart is one hue, because
>   every chart plots one series whose job is magnitude.
> - **Looking at the rendered page found three defects no test did**: a toggle
>   that read as a column header, bars with no accessible name, and
>   all-lowercase labels. Run the app; the suite does not have eyes.
> - **`REPORT_TYPES` grew**, so a schedule can finally name something other than
>   `leads`.


**Read the `dataviz` skill before writing any chart code.** PROMPTS.md says so
explicitly.

### The one that is easy to get wrong

`GET /reports/breakdown?field_key=` is parameterised **because which field
represents "source" is a per-workspace decision.** There is no sources report.
If you find yourself writing one, that is the hardcoded-taxonomy mistake in a new
costume.

Likewise the leaderboard honours `workspaces.leaderboard_metrics`, which already
exists as a column.

### Build one pivot widget

One generic component with a configurable row dimension and stage-kind columns.
PROMPTS.md: *"If you write a second similar component, stop and generalise the
first."*

### Custom and role-specific dashboards

- `dashboards` table is **not yet created** — `01-data-model.md` §5.5 has the
  schema, M9 owns the migration.
- `GET /dashboards/widgets` returns the widget catalogue **with each widget's
  config schema**, so the frontend does not hardcode it — the same pattern as
  `/settings/field-types`, which the M2 UI already consumes. Copy that shape.
- Binding a dashboard to a `template_id` gives it to every member on that
  template.

### Performance

Every report endpoint under 500ms on the demo workspace, **with tests asserting
it**. If a query cannot hit that, precompute rather than shipping something slow.

### Done when

Build a dashboard as admin, bind it to Caller, log in as a caller and see it.
Then click a cell showing N and confirm the list says exactly N.

---

## 7. M10 — Intake and event bus *(5–7 days)*

> **Update, 21 Aug 2026 — M10 has landed, ahead of M9.** `324a755` (backend),
> `90056ef` (frontend), `cb893a2` (a defect found in live verification).
> Revision `0009_m10_intake`. 632 backend tests, 117 Playwright.
>
> Taken out of order deliberately: the event bus is the seam the planned
> voice-agent integration attaches to (`docs/06-voice-integration-contract.md`
> §2), it is the shorter of the two remaining milestones, and M9's dashboards
> depend on nothing in it. **M9 is now the only milestone left before M11.**
>
> Acceptance verified live against a real HTTP consumer computing HMACs
> independently: a delivery refused with 500 and recorded FAILED with backoff;
> the row then left in `DELIVERING` holding a claim its worker could never
> refresh; a later pass reclaimed and delivered it; the consumer saw the **same
> `event_id` on both deliveries** and verified the signature against the real
> secret. An unknown field posted through intake arrived in the payload, having
> been accepted, stored and warned about.
>
> Six things this milestone settled that the section below does not say:
>
> - **The claim is committed before the HTTP call.** That is the whole recovery
>   mechanism — a process that dies during the request leaves a `DELIVERING`
>   row with a stale `claimed_at`, and any claim older than five minutes is
>   reclaimed. No coordination, no leader election, just a timeout only a live
>   worker can beat.
> - **Delivery is at-least-once on purpose.** A worker can die after the
>   consumer accepted and before the commit. That is why `event_id` is stable
>   across retries; at-most-once loses events, and a lost lead event is worse
>   than a duplicate.
> - **Publishing lives in `ActionWriter`, not at call sites.** Every mutation
>   already writes an action, so the event rides the same chokepoint and bulk
>   edits, imports and undo all publish without knowing the bus exists.
>   Subscriptions load in `open_changeset`, which is already async and always
>   called, so `_append` stays synchronous.
> - **`httpx` was a dev-only dependency.** The dispatcher would have been an
>   `ImportError` in production and nowhere else. Now a runtime dependency.
> - **`run_dispatch` read ORM state after committing**, which only worked
>   because every sessionmaker in the codebase sets `expire_on_commit=False`.
>   The first script that did not crashed immediately. Fixed in `cb893a2` with
>   a regression test that builds its session the naive way.
> - **`/settings/webhooks/{id}/test` is the one synchronous outbound call.**
>   Rule 8 exists so a *user's* action never depends on a third party being up;
>   an operator asking "is my endpoint reachable" is not that, and an answer
>   that arrived later in a log would not answer the question.


### The rule that is not negotiable

**Unknown keys are accepted, stored, and returned in `warnings` — never
rejected.** `02-api-contract.md` puts it plainly: *a rejected payload at 2am is a
lost lead.*

`ValueValidator` already does this: unknown keys pass through into
`ValidatedValues.unknown_keys` rather than raising. Do not add strictness on the
intake path.

Note the interaction with variable quarantine: a `{{...}}` template arriving from
a webhook is quarantined, not stored. Surface both `warnings` and quarantine in
`intake_log`.

### The outbox

- `outbox_events` written **in the same transaction as the state change**. Never
  call an external webhook from a request handler (architecture rule 8).
- HMAC-SHA256 signing; `X-CRM-Event-Id` stable across retries so consumers
  dedupe.
- Backoff `2^attempts` minutes capped at 60; `DEAD` after 8; manual redrive.
- **Webhook payloads pass through `FieldProjectionService`** using the endpoint's
  configured role. A webhook is a read path like any other.

### Done when

Kill the worker mid-delivery and show a retry rather than a lost event. Post an
unknown field and show it stored with a warning.

---

## 8. M11 — Hardening *(6–8 days)*

> **Update, 22 Aug 2026 — M11 has landed, less deployment.** `80bcaf8`
> (accounts, observability, matrix, backups), `1604776` (bootstrap + the
> empty-workspace drill), `461412c` (the 500k pass). Revision
> `0011_m11_credentials`. 706 backend tests, 139 Playwright plus 5 live.
>
> **Production images and hosting are deliberately deferred** at the owner's
> direction; both Dockerfiles still say "deployment images land in M11", and
> that comment now outlives the milestone. Everything else was exercised
> locally, which closes eight of the ten items.
>
> Five things this milestone found rather than built:
>
> - **The invite flow was a dead end.** `invite_member` created an account with
>   an unguessable password and a docstring promising a password reset that did
>   not exist. Every invited user held an account they could never sign in to.
> - **Invite-only had no first account.** An account exists because somebody
>   invited it, and a fresh deployment has nobody to do the inviting.
>   `python -m app.bootstrap` opens the loop from outside — a command, not an
>   endpoint, because an endpoint that mints the first owner is one an attacker
>   races the operator to.
> - **A lead field you had not added as a table column was uneditable.** The
>   list narrows `values` to the chosen columns; the detail panel was fed that
>   narrowed row and blamed a permission template for the absence. On a fresh
>   workspace, where every column is a built-in, that was every field. Found by
>   the first honest run of the empty-workspace E2E, which is the entire
>   argument for writing it — 139 stubbed specs never could, because a stub is
>   where that assumption hides.
> - **The tenant scope is sticky on the SQLAlchemy session**, not on the
>   `ScopedSession` wrapper. Provisioning a second workspace through an
>   already-scoped session read the first one's rows. Harmless in a request,
>   which is why it survived eleven milestones; fatal to anything that walks
>   tenants on one session.
> - **The demo seeder could only run once per database**, because `users.email`
>   is unique database-wide and its cast list was fixed.
>
> Capacity, measured rather than assumed: **500,000 leads across 5 workspaces**,
> every shape inside budget from a tenant in the middle of the five. The numbers
> and the command are in `docs/07-runbook.md` §8a.


Out of scope for this document, but note what it inherits: self-serve signup
(there is none — see §2), a 500k-lead perf pass, structlog with request and
workspace ids, Sentry, Prometheus, a **restore drill actually run**, and a
Playwright E2E proving *configure an empty workspace, then use it*.

---

## 9. Conventions a fresh session will otherwise violate

These are the ones that have actually caused rework here.

1. **One Alembic revision per milestone**, named `000N_mN_<slug>`, linear. Never
   edit an applied one. `tests/test_migrations.py::EXPECTED_REVISIONS` asserts
   the list — extend it deliberately.

2. **The guard tests fail on purpose when a milestone lands.** Five of them
   assert the current shape of the world:
   - `test_the_schema_defines_exactly_the_tables_the_landed_milestones_own`
   - `test_there_is_exactly_one_revision_per_milestone`
   - `test_no_enum_in_the_database_encodes_business_taxonomy`
   - `test_the_provisioning_registry_matches_the_spec`
   - `test_the_matrix_covers_every_workspace_scoped_route`

   Extend them to assert the *new* correct state. **Never weaken one to get
   green.** The last one derives coverage from the app's own OpenAPI schema and
   will fail on any tenant route not in the isolation matrix — that is the point,
   and it has already caught 26 routes.

3. **Every new tenant endpoint needs an isolation test.** Member of A gets 404 —
   not 403 — for B's data by direct id, by list, and by foreign reference. Add
   the route to the matrix lists in
   `tests/isolation/test_cross_workspace.py`; the guard will tell you if you
   forget.

4. **Frontend reads configuration from the backend.** No field-type list, stage
   list, disposition list or widget catalogue is hardcoded in `web/src/`. A grep
   for the 13 type names across `src/` returns nothing outside the wire types,
   and a Playwright test counts the type picker's options against the server
   registry. Keep it that way.

5. **Route ordering:** in FastAPI a literal segment must be declared before a
   uuid placeholder, or `/options/reorder` is parsed as an option id and 422s.
   This has bitten twice — once on options, once on stages.

6. **Soft delete everywhere.** Config archives, leads and actions never
   hard-delete. `ScopedSession` deliberately has no `delete()`. The one genuine
   deletion is `indexed_fields`, and it names `workspace_id` explicitly because
   the loader criteria only applies to `SELECT`.

7. **`ruff`, `mypy --strict`, `eslint`, `prettier`, `tsc` all clean before
   commit.** Pre-commit hooks enforce it.

---

## 10. Suggested order

```
0.1 LeadOwnership          done  4c07390
0.2 indexed-field worker   done  5f14730
    demo seed (§8)         done  231d845
M6  list + filters         done  3c6b2b4 · 231d845 · c7db934
M7  tasks, bulk, undo      done  26b0e20 · 3b055ba · 9339ffa · 2958832
M8  assignment, scheduler  done  8bd790e · 6ce7cdf · ef9ac65 · 799a258
M9  dashboards, reports    done  033d4a8 · 80fa95f
M10 intake, outbox         5-7d
```

Do §0.1 and §0.2 before M6. They are small, they are both in the critical path,
and one of them is a live defect.

---

## 11. What "done" looks like per milestone

Every milestone's definition of done, from `00-milestones.md`:

- [ ] `ruff check`, `mypy app`, `pnpm lint`, `pnpm tsc --noEmit` clean
- [ ] `pytest` green; coverage not below the previous milestone
- [ ] Cross-workspace isolation test for every new endpoint
- [ ] Field-permission test for every new lead read/write path
- [ ] Alembic migration applies **and rolls back**
- [ ] No hardcoded business taxonomy in the diff
- [ ] Manually exercised against the demo workspace

The last one is currently the weakest link: there is no demo workspace, and the
frontend has never been driven against the real backend. M6 is the natural place
to fix both.
