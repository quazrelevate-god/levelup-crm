# Data model

Postgres 16. Multi-tenant, configuration-driven.

**Read `03-configuration-model.md` first.** This document is the storage
consequence of that spec.

Design principle: **the only enums in the database are product concepts. Every
business concept is a row.** Field types are an enum (we ship 13). Stage types
are an enum (4 structural kinds). Statuses, fields, dispositions, action types
and permissions are all data.

---

## 1. Extensions and product enums

```sql
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS citext;

-- Structural pipeline kinds. NOT customer statuses.
CREATE TYPE stage_kind AS ENUM ('INITIAL', 'ACTIVE', 'WON', 'LOST');

-- Lead field types (03-configuration-model.md §1.3)
CREATE TYPE lead_field_type AS ENUM (
  'TEXT', 'DROPDOWN', 'TAGS', 'EMAIL', 'PHONE', 'CHECKBOX', 'DATE',
  'MONEY', 'NUMBER', 'WEBSITE', 'DEPENDENT_DROPDOWN', 'RECURRING_DATE', 'LOCATION'
);

-- Action field types (§4.3) — deliberately a different, smaller set
CREATE TYPE action_field_type AS ENUM (
  'TEXT', 'NUMBER', 'DATE', 'DROPDOWN', 'TAGS', 'USER', 'FILE', 'MEDIA_LINK'
);

CREATE TYPE action_direction AS ENUM ('INBOUND', 'OUTBOUND', 'INFORMATION');

-- System timeline events. Custom action types live in a table.
CREATE TYPE system_action_kind AS ENUM (
  'LEAD_CREATED', 'FIELD_CHANGE', 'STAGE_CHANGE', 'ASSIGNMENT_CHANGE',
  'RATING_CHANGE', 'NOTE', 'CALL_LOGGED', 'WHATSAPP_SENT', 'EMAIL_SENT',
  'SMS_SENT', 'TASK_CREATED', 'TASK_COMPLETED', 'CUSTOM'
);

CREATE TYPE permission_grant AS ENUM ('VIEW', 'EDIT', 'IMPORT', 'EXPORT');
CREATE TYPE task_status      AS ENUM ('PENDING', 'DONE', 'CANCELLED');
CREATE TYPE outbox_status    AS ENUM ('PENDING', 'DELIVERED', 'FAILED', 'DEAD');
```

That is the **complete** list of business-ish enums. If you are tempted to add
`ProductType` or `ApplicationStatus`, you have misunderstood the product.

---

## 2. Tenancy and identity

```sql
CREATE TABLE workspaces (
  id                 uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  name               text NOT NULL,
  slug               citext NOT NULL UNIQUE,

  -- Preferences (03-configuration-model.md §5)
  default_country_code text NOT NULL DEFAULT '91',
  timezone             text NOT NULL DEFAULT 'Asia/Kolkata',
  currency             char(3) NOT NULL DEFAULT 'INR',
  connected_call_min_seconds int NOT NULL DEFAULT 1,
  session_timeout_minutes    int,                    -- null = never

  leaderboard_metrics  jsonb NOT NULL DEFAULT '{"stage":true,"rating":false}',
  features             jsonb NOT NULL DEFAULT '{}',  -- feature flags, §5
  settings             jsonb NOT NULL DEFAULT '{}',  -- forward-compat

  -- which field is the unique identifier (§1.1). Nullable during provisioning.
  identity_field_id  uuid,
  primary_field_1_id uuid,   -- H1
  primary_field_2_id uuid,   -- H2

  is_active   boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  email         citext NOT NULL UNIQUE,
  full_name     text NOT NULL,
  password_hash text NOT NULL,
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE memberships (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id       uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  template_id   uuid NOT NULL REFERENCES permission_templates(id),
  manager_id    uuid REFERENCES memberships(id),   -- reporting hierarchy
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, user_id)
);
```

A user may belong to several workspaces. **Permissions attach to the membership,
not the user.**

### 2.4 Indexed fields

Because customer fields live in JSONB, sorting and filtering need help. Each
workspace may designate up to **8** fields as indexed. Marking one enqueues a job
that creates a Postgres **expression index** on `(workspace_id, (values->>'<id>'))`.

```sql
CREATE TABLE indexed_fields (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  field_id     uuid NOT NULL REFERENCES lead_fields(id) ON DELETE CASCADE,
  index_name   text NOT NULL,
  status       text NOT NULL DEFAULT 'PENDING',  -- PENDING|READY|FAILED
  created_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, field_id)
);
```

This is the **one** place index DDL is emitted, from a background worker using
`CREATE INDEX CONCURRENTLY`, with a generated safe name. Never from a request
handler, and never `ALTER TABLE`.

---

## 3. Configuration tables

### 3.1 Lead fields

```sql
CREATE TABLE lead_fields (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  key           text NOT NULL,          -- stable slug used as the JSONB key
  label         text NOT NULL,          -- 1..40 chars
  field_type    lead_field_type NOT NULL,
  description   text,

  is_builtin    boolean NOT NULL DEFAULT false,  -- Name/Phone/Email/Alt Phone
  is_hidden     boolean NOT NULL DEFAULT false,  -- hidden, never deleted
  is_required   boolean NOT NULL DEFAULT false,
  sort_order    int NOT NULL DEFAULT 0,
  field_group   text,

  -- properties (§1.4)
  show_in_import   boolean NOT NULL DEFAULT true,
  show_in_quick_add boolean NOT NULL DEFAULT false,
  lock_after_create boolean NOT NULL DEFAULT false,
  can_use_variable  boolean NOT NULL DEFAULT false,

  -- type-specific config: parent_field_id for DEPENDENT_DROPDOWN,
  -- recurrence rule for RECURRING_DATE, precision for LOCATION, etc.
  config        jsonb NOT NULL DEFAULT '{}',

  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, key)
);

CREATE TABLE field_options (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  field_id      uuid NOT NULL REFERENCES lead_fields(id) ON DELETE CASCADE,
  parent_option_id uuid REFERENCES field_options(id),  -- DEPENDENT_DROPDOWN
  code          text NOT NULL,
  label         text NOT NULL,          -- 1..70 chars
  color         text,
  sort_order    int NOT NULL DEFAULT 0,
  is_archived   boolean NOT NULL DEFAULT false,
  UNIQUE (field_id, code)
);
```

`key` is generated from the label on create and then **immutable** — renaming the
label must not orphan stored values.

### 3.2 Stages and lost reasons

```sql
CREATE TABLE stages (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  kind         stage_kind NOT NULL,
  label        text NOT NULL,           -- max 28 chars
  color        text NOT NULL DEFAULT '#6b7280',
  sort_order   int NOT NULL DEFAULT 0,
  is_archived  boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);

-- Cardinality: exactly one live INITIAL, WON, and LOST per workspace.
CREATE UNIQUE INDEX stages_singleton_uq
  ON stages (workspace_id, kind)
  WHERE kind IN ('INITIAL', 'WON', 'LOST') AND is_archived = false;

CREATE TABLE lost_reasons (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  label        text NOT NULL,
  sort_order   int NOT NULL DEFAULT 0,
  is_default   boolean NOT NULL DEFAULT false,
  is_archived  boolean NOT NULL DEFAULT false
);
```

Cap of **25 live lost reasons** per workspace — enforce in the service layer with
a clear error, not a constraint violation.

### 3.3 Call dispositions

```sql
CREATE TABLE call_dispositions (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  label        text NOT NULL,
  is_default   boolean NOT NULL DEFAULT false,
  is_system    boolean NOT NULL DEFAULT false,  -- system: archivable, not editable
  is_archived  boolean NOT NULL DEFAULT false,
  sort_order   int NOT NULL DEFAULT 0
);

CREATE UNIQUE INDEX call_dispositions_default_uq
  ON call_dispositions (workspace_id)
  WHERE is_default = true AND is_archived = false;
```

### 3.4 Custom action types

```sql
CREATE TABLE custom_action_types (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  code          int NOT NULL,            -- workspace-sequential from 1001
  name          text NOT NULL,
  icon          text,
  score         int NOT NULL DEFAULT 0 CHECK (score BETWEEN -1000 AND 1000),
  direction     action_direction NOT NULL DEFAULT 'INFORMATION',
  description   text,
  allow_predated boolean NOT NULL DEFAULT false,
  is_archived   boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, code)
);

CREATE TABLE action_fields (
  id                uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  action_type_id    uuid NOT NULL REFERENCES custom_action_types(id) ON DELETE CASCADE,
  key               text NOT NULL,
  label             text NOT NULL,
  field_type        action_field_type NOT NULL,
  description       text,
  is_required       boolean NOT NULL DEFAULT false,
  is_hidden         boolean NOT NULL DEFAULT false,
  sort_order        int NOT NULL DEFAULT 0,
  config            jsonb NOT NULL DEFAULT '{}',
  UNIQUE (action_type_id, key)
);

CREATE TABLE action_field_options (
  id              uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  action_field_id uuid NOT NULL REFERENCES action_fields(id) ON DELETE CASCADE,
  code            text NOT NULL,
  label           text NOT NULL,
  color           text,
  sort_order      int NOT NULL DEFAULT 0,
  UNIQUE (action_field_id, code)
);
```

### 3.5 Permission templates

```sql
CREATE TABLE permission_templates (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name         text NOT NULL,
  is_system    boolean NOT NULL DEFAULT false,   -- the 5 defaults
  is_readonly  boolean NOT NULL DEFAULT false,   -- Root
  capabilities jsonb NOT NULL DEFAULT '{}',      -- Access + View groups, §6.2
  created_by   uuid REFERENCES users(id),
  updated_by   uuid REFERENCES users(id),
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, name)
);

-- The field-level matrix (§6.4). One row per (template, field, grant).
CREATE TABLE template_field_grants (
  template_id uuid NOT NULL REFERENCES permission_templates(id) ON DELETE CASCADE,
  field_id    uuid NOT NULL REFERENCES lead_fields(id) ON DELETE CASCADE,
  grant       permission_grant NOT NULL,
  PRIMARY KEY (template_id, field_id, grant)
);

CREATE INDEX tfg_template_idx ON template_field_grants (template_id, grant);
```

Presence of a row = granted. Absence = denied. **Deny by default**, which matches
the observed `Export (0) None` default.

`capabilities` shape:

```jsonc
{
  "leads": {
    "admin_access": false,
    "create_from_whatsapp_and_calls": true,
    "manually_add_lead": true,
    "bulk_edit": true,
    "actions": true,
    "merge_leads": false,
    "search": true
  },
  "team": {...}, "calling": {...}, "reports": {...},
  "automations": {...}, "tasks": {...}, "integrations": {...},
  "salesform": {...}, "permissions": {...}, "billings": {...},
  "view": { "lead": {...}, "dashboard": {...}, "leads_table": {...} }
}
```

Validate it against a Pydantic model — a JSONB blob nobody validates becomes a
JSONB blob nobody understands.

---

## 4. Lead data

```sql
CREATE TABLE leads (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,

  identity_value text NOT NULL,        -- normalised value of the identity field
  values         jsonb NOT NULL DEFAULT '{}',   -- keyed by lead_fields.key

  stage_id       uuid REFERENCES stages(id),
  lost_reason_id uuid REFERENCES lost_reasons(id),
  assignee_id    uuid REFERENCES memberships(id),
  rating         smallint CHECK (rating BETWEEN 1 AND 5),
  score          int NOT NULL DEFAULT 0,        -- rollup of action scores

  last_action_at timestamptz,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  created_by     uuid REFERENCES memberships(id),
  deleted_at     timestamptz
);

CREATE UNIQUE INDEX leads_identity_uq
  ON leads (workspace_id, identity_value) WHERE deleted_at IS NULL;

CREATE INDEX leads_ws_created_idx  ON leads (workspace_id, created_at DESC) WHERE deleted_at IS NULL;
CREATE INDEX leads_ws_stage_idx    ON leads (workspace_id, stage_id)        WHERE deleted_at IS NULL;
CREATE INDEX leads_ws_assignee_idx ON leads (workspace_id, assignee_id)     WHERE deleted_at IS NULL;
CREATE INDEX leads_ws_score_idx    ON leads (workspace_id, score)           WHERE deleted_at IS NULL;
CREATE INDEX leads_values_gin      ON leads USING gin (values jsonb_path_ops);
CREATE INDEX leads_identity_trgm   ON leads USING gin (identity_value gin_trgm_ops);
```

`identity_value` is a denormalised copy of whichever field the workspace
designated. Changing that setting triggers a backfill job.

**Search** over arbitrary customer fields uses a maintained tsvector:

```sql
ALTER TABLE leads ADD COLUMN search_vector tsvector;
CREATE INDEX leads_search_idx ON leads USING gin (search_vector);
```

Recomputed on write from the values of fields flagged searchable.

### 4.1 Actions

```sql
CREATE TABLE actions (
  id             uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  lead_id        uuid NOT NULL REFERENCES leads(id) ON DELETE CASCADE,
  changeset_id   uuid REFERENCES changesets(id),   -- see §4.2 — enables undo
  kind           system_action_kind NOT NULL,
  action_type_id uuid REFERENCES custom_action_types(id),  -- when kind='CUSTOM'
  actor_id       uuid REFERENCES memberships(id),          -- null = system
  payload        jsonb NOT NULL DEFAULT '{}',
  body           text,
  score_applied  int NOT NULL DEFAULT 0,
  is_pinned      boolean NOT NULL DEFAULT false,
  performed_at   timestamptz NOT NULL DEFAULT now(),
  created_at     timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX actions_lead_time_idx ON actions (lead_id, performed_at DESC);
CREATE INDEX actions_ws_kind_idx   ON actions (workspace_id, kind, performed_at DESC);

CREATE TABLE action_attachments (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  action_id    uuid NOT NULL REFERENCES actions(id) ON DELETE CASCADE,
  field_key    text NOT NULL,
  storage_key  text NOT NULL,
  filename     text NOT NULL,
  content_type text NOT NULL,
  size_bytes   bigint NOT NULL,
  created_at   timestamptz NOT NULL DEFAULT now()
);
```

`score_applied` is copied at write time so editing a custom action type's score
does not silently rewrite history. A lead's `score` is the sum of
`score_applied`, maintained by trigger or in the action-writing service.

Payload shapes: `FIELD_CHANGE` → `{field_key, label, old, new}`;
`STAGE_CHANGE` → `{old_stage_id, new_stage_id, lost_reason_id}`;
`CALL_LOGGED` → `{direction, disposition_id, duration_seconds, notes}`;
`CUSTOM` → `{<action_field.key>: value, ...}` validated against `action_fields`.

---

### 4.2 Changesets — the undo mechanism

Every mutation batch — a single PATCH, a 500-lead bulk edit, an import run, a
redistribution — opens a changeset. Every action it produces carries the id.
Undo replays inverse field changes for the whole set atomically.

```sql
CREATE TYPE changeset_source AS ENUM (
  'SINGLE_EDIT', 'BULK_EDIT', 'IMPORT', 'DISTRIBUTION', 'AUTOMATION', 'INTAKE'
);

CREATE TABLE changesets (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  source        changeset_source NOT NULL,
  actor_id      uuid REFERENCES memberships(id),
  summary       text NOT NULL,          -- "Set Stage to HOT on 312 leads"
  lead_count    int NOT NULL DEFAULT 0,
  is_undone     boolean NOT NULL DEFAULT false,
  undone_at     timestamptz,
  undone_by     uuid REFERENCES memberships(id),
  undo_of_id    uuid REFERENCES changesets(id),   -- the undo is itself a changeset
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX changesets_ws_created_idx ON changesets (workspace_id, created_at DESC);
```

**Design this into the action-writing service in M5.** Retrofitting a batch id
across every mutation path later means touching every one of them again.

Undo rules: only `FIELD_CHANGE`, `STAGE_CHANGE`, `ASSIGNMENT_CHANGE` and
`RATING_CHANGE` are reversible. If a lead changed again after the changeset,
flag the conflict and let the operator choose — never silently clobber.

---

## 5. Assignment, groups, templates

### 5.1 Licensing and availability

```sql
CREATE TYPE availability_status AS ENUM ('WORKING', 'ON_LEAVE', 'INACTIVE');

ALTER TABLE workspaces ADD COLUMN seat_limit int NOT NULL DEFAULT 3;

ALTER TABLE memberships
  ADD COLUMN has_license  boolean NOT NULL DEFAULT false,
  ADD COLUMN availability availability_status NOT NULL DEFAULT 'WORKING';

CREATE TABLE availability_log (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  membership_id uuid NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
  status        availability_status NOT NULL,
  note          text,
  changed_by    uuid REFERENCES memberships(id),
  changed_at    timestamptz NOT NULL DEFAULT now()
);
```

Licensed seats cannot exceed `seat_limit`. An unlicensed member cannot log in.
Deactivating a member requires a reassignment target for their open leads —
enforce it in the service, never orphan a pipeline.

### 5.2 Sales groups

```sql
CREATE TABLE sales_groups (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name         text NOT NULL,
  description  text,
  is_archived  boolean NOT NULL DEFAULT false,
  UNIQUE (workspace_id, name)
);

CREATE TABLE sales_group_members (
  group_id      uuid NOT NULL REFERENCES sales_groups(id) ON DELETE CASCADE,
  membership_id uuid NOT NULL REFERENCES memberships(id) ON DELETE CASCADE,
  weight        int NOT NULL DEFAULT 1,     -- weighted round-robin
  PRIMARY KEY (group_id, membership_id)
);
```

### 5.3 Assignment rules

```sql
CREATE TYPE assignment_strategy AS ENUM (
  'ROUND_ROBIN', 'WEIGHTED', 'FIELD_VALUE', 'SALES_GROUP', 'FIXED', 'UNASSIGNED'
);

CREATE TABLE assignment_rules (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name          text NOT NULL,
  priority      int NOT NULL DEFAULT 0,      -- lowest number wins
  conditions    jsonb NOT NULL DEFAULT '{}', -- same filter DSL, evaluated on the new lead
  strategy      assignment_strategy NOT NULL,
  config        jsonb NOT NULL DEFAULT '{}', -- group_id, field_key→member map, member ids
  skip_unavailable boolean NOT NULL DEFAULT true,
  is_active     boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now()
);

-- round-robin cursor, per rule
CREATE TABLE assignment_cursors (
  rule_id          uuid PRIMARY KEY REFERENCES assignment_rules(id) ON DELETE CASCADE,
  last_membership_id uuid REFERENCES memberships(id),
  updated_at       timestamptz NOT NULL DEFAULT now()
);
```

Rules evaluate in `priority` order on every lead create — UI, import, or intake
API — and the first match wins. `skip_unavailable` skips members whose
`availability` is not `WORKING`. If no rule matches, the lead is unassigned.

### 5.4 Message templates

```sql
CREATE TYPE template_channel AS ENUM ('WHATSAPP', 'SMS', 'EMAIL');

CREATE TABLE message_templates (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  channel       template_channel NOT NULL,
  name          text NOT NULL,
  subject       text,                        -- EMAIL only
  body          text NOT NULL,               -- contains {{field_key}} placeholders
  owner_id      uuid REFERENCES memberships(id),   -- null = shared
  template_id   uuid REFERENCES permission_templates(id),  -- role-scoped, null = all
  is_archived   boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now()
);
```

Visibility: personal (`owner_id` = caller) **or** shared (`owner_id` null and
`template_id` null) **or** role-scoped (`template_id` = caller's template).

Rendering substitutes `{{field_key}}` from the lead's values — **through
`FieldProjectionService`**, so a template cannot leak a field the sender lacks
View on. Unresolved placeholders render empty and are reported in the preview.

### 5.5 Custom dashboards and scheduled reports

```sql
CREATE TABLE dashboards (
  id           uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name         text NOT NULL,
  owner_id     uuid REFERENCES memberships(id),              -- null = shared
  template_id  uuid REFERENCES permission_templates(id),     -- role-specific
  layout       jsonb NOT NULL DEFAULT '[]',  -- [{widget, x, y, w, h, config}]
  is_default   boolean NOT NULL DEFAULT false,
  created_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE scheduled_reports (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v4(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  report_type   text NOT NULL,               -- leaderboard | activity | funnel | leads
  params        jsonb NOT NULL DEFAULT '{}', -- filter_id, date range mode, field_key
  cron          text NOT NULL,               -- evaluated in the workspace timezone
  recipients    text[] NOT NULL,
  format        text NOT NULL DEFAULT 'csv', -- csv | xlsx | pdf
  is_active     boolean NOT NULL DEFAULT true,
  last_run_at   timestamptz,
  last_error    text,
  created_by    uuid REFERENCES memberships(id)
);
```

Scheduled reports render **as the creating member**, so field permissions apply
to what lands in the recipient's inbox.

---

## 6. Views, tasks, integrations

`saved_filters`, `table_layouts`, `labels`, `lead_labels`, `tasks`,
`outbox_events`, `webhook_endpoints`, `api_keys`, `intake_log` — all carry
`workspace_id` and are otherwise as described in `02-api-contract.md`.

`saved_filters` gains a visibility column: `PERSONAL` · `SHARED` · `ROLE`
(with `template_id`).

### 6.1 Filter DSL — including history predicates

Rules reference `lead_fields.key`; the operator set derives from `field_type`.
Two node kinds beyond field rules:

```jsonc
// "no outgoing call in the last 14 days"
{ "type": "action_not_performed",
  "action_kind": "CALL_LOGGED",
  "payload_match": { "direction": "OUTGOING" },
  "within": { "last_days": 14 } }

// "status went from HOT to Lost in the last 7 days"
{ "type": "status_changed",
  "from_stage_id": "...", "to_stage_id": "...",
  "within": { "last_days": 7 } }

// "assigned away from Priya this month"
{ "type": "assignee_changed",
  "from_membership_id": "...", "to_membership_id": null,
  "within": { "from": "2026-08-01", "to": "2026-08-31" } }

// "at least 3 actions of type X by anyone on the team"
{ "type": "action_performed",
  "action_kind": "CUSTOM", "action_type_id": "...",
  "actor_id": null, "min_count": 3,
  "within": { "last_days": 30 } }
```

These compile to `EXISTS` / `NOT EXISTS` correlated subqueries over `actions`.
For them to be fast, `STATUS_CHANGE` and `ASSIGNMENT_CHANGE` payloads need
expression indexes on their old/new ids:

```sql
CREATE INDEX actions_status_change_idx ON actions (
  workspace_id,
  (payload->>'old_stage_id'), (payload->>'new_stage_id'), performed_at DESC
) WHERE kind = 'STAGE_CHANGE';

CREATE INDEX actions_assignment_idx ON actions (
  workspace_id,
  (payload->>'old_assignee_id'), (payload->>'new_assignee_id'), performed_at DESC
) WHERE kind = 'ASSIGNMENT_CHANGE';
```

**Build these in M5, when the payloads are defined** — not in M6 when the filters
need them.

---

## 7. Provisioning a new workspace

`POST /workspaces` creates **only structure, never taxonomy**:

- 4 built-in lead fields: Name (TEXT) · Phone (PHONE) · Email (EMAIL) ·
  Alternate Phone (PHONE)
- `identity_field_id` → Phone; `primary_field_1_id` → Name; `2` → Phone
- 4 stages: `New` (INITIAL) · `Contacted` (ACTIVE) · `Won` (WON) · `Lost` (LOST)
- Lost reasons: Not interested · Budget · Competitor · No response ·
  Unknown *(default)*
- 7 system call dispositions: Connected *(default)* · Number Busy · No Answer ·
  Switched Off · Wrong Number · Call Later · Redialed
- 5 permission templates: Root *(readonly)* · Admin · Manager · Caller ·
  Marketing
- Preferences from the signup form (country, timezone, currency)

Nothing else. No products, no industry stages, no scoring fields.

---

## 8. Demo seed — a fixture, not a default

`uv run python -m app.seed --workspace demo --leads 50000 --seed 42`

Creates a **fictional** workspace ("Northwind Tutors") that exercises every field
type, several custom actions, and a full permission matrix. Requirements:

- Uses all 13 lead field types, including the three composites
- 3 custom action types with varied scores, directions, and action-field types
- 5 permission templates with genuinely different field grants — at least one
  where a field is View-but-not-Edit and one where Export is empty
- 5 memberships across those templates
- 50k leads, funnel-shaped stage distribution, log-normal action counts,
  causally coherent timelines, ~30% sparse values
- Bulk `COPY`; under 3 minutes

**Never used to populate a real workspace.** It exists so tests have something
non-trivial to run against.

---

## 9. Anti-requirements

Do not build: hardcoded industry taxonomy · runtime `ALTER TABLE` · per-tenant
schemas or databases · telephony · a workflow engine (events out, n8n
orchestrates) · billing · hard deletes.

> **Changed (October 2026): billing, for AI-call minutes only.** Once AI calls
> became something clients pay for by the minute, a prepaid wallet was built:
> `wallets` and `wallet_transactions`, see `docs/14-wallet.md`. Seat licensing,
> invoicing and payment collection remain out of scope; money is recorded by
> the operator, not taken by the product.
