# 12 — Voice extraction mappings

**Status: additive on top of the M0–M11 base and the voice work in
`docs/09`–`docs/11`. Nothing here modifies the outbound trigger, the webhook
authentication, the idempotency gate, call logging, call summary, call_count,
phone matching, transcript/recording storage, or conversation continuity.**

The problem this closes: today, Bolna's `extracted_data` — its post-call
structured extraction — arrives on every completed webhook, is stored verbatim
in `voice_call_executions.raw_payload`, and never reaches the lead's own
fields. Operators can see what Bolna heard by reading the JSON; they cannot
have it update the lead automatically.

This document describes the configurable layer that closes that gap and its
safety rules.

---

## 1. The mapping

One row per `(workspace, disposition_name)`. An operator names a Bolna
disposition — the customer's vocabulary, always — and picks the CRM lead
field it should update.

```sql
CREATE TABLE voice_extraction_mappings (
  id                UUID PRIMARY KEY,
  workspace_id      UUID NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  disposition_name  TEXT NOT NULL,        -- as Bolna emits it
  target_field_key  TEXT NOT NULL,        -- LeadField.key
  min_confidence    NUMERIC(3,2) NOT NULL DEFAULT 0.70,
  is_enabled        BOOLEAN      NOT NULL DEFAULT true,
  created_at        TIMESTAMPTZ  NOT NULL,
  updated_at        TIMESTAMPTZ  NOT NULL,
  UNIQUE (workspace_id, disposition_name),
  CHECK (min_confidence BETWEEN 0 AND 1)
);
```

**Deliberately narrower than the frozen contract text in `docs/06`.** The
contract's §6.1 also lists `target_kind ∈ {LEAD_FIELD, STAGE, ACTION_FIELD,
IGNORE}` and a `value_map` column for dropdown value translation. Both are
follow-ons if the STAGE/ACTION_FIELD targets are wired up — adding a column
later is a smaller migration than deleting an unused one, and rolling out
with fewer knobs means the confidence gate's behaviour is easier to reason
about while the feature is new.

---

## 2. How mappings work

Bolna sends `extracted_data` on every completed webhook, keyed by disposition
name:

```json
"extracted_data": {
  "Customer Name": {"value": "Asha R.",       "confidence": 0.94},
  "Email":         {"value": "asha@x.com",    "confidence": 0.61},
  "Budget":        {"value": "45000",         "confidence": 0.99}
}
```

After the completed-webhook idempotency gate lets the delivery through, the
CRM walks every entry and decides one of four outcomes per disposition:

| State | What happens |
|---|---|
| **Enabled mapping, above threshold, valid, non-empty, different from current** | Field updated, one `FIELD_CHANGE` action on the timeline plus one `NOTE` recording the extraction and confidence |
| **Enabled mapping, above threshold, same as current** | Nothing. No write, no note. |
| **Enabled mapping, below threshold / invalid / no confidence / hidden field / summary conflict / identity conflict** | No field write, one `NOTE` on the timeline explaining why |
| **Enabled mapping, empty value** | Nothing. No write, no note. (An empty extraction is not a signal.) |
| **Disabled mapping** | Nothing. No write, no note. |
| **No mapping** | Nothing. Recorded in the webhook response's `extraction_unmapped` for observability. |

Everything the extraction pass writes lives in **one AUTOMATION changeset** so
an operator can undo the whole pass in one click, separate from the call
summary and the call log (each of which has its own AUTOMATION changeset).

---

## 3. Configuring a mapping

**Settings → Voice extraction** in the app, or the API:

```
POST /api/v1/workspaces/{workspace_id}/voice/extraction-mappings
{
  "disposition_name": "Customer Name",
  "target_field_key": "name",
  "min_confidence":   0.70,
  "is_enabled":       true
}
```

The router validates that `target_field_key` names a live, non-hidden
`LeadField` in the workspace and that `disposition_name` does not equal the
deployment's configured summary disposition (see §5). Uniqueness on
`(workspace_id, disposition_name)` is enforced at both the router and the DB.

The built-in field keys every workspace ships with are `name`, `phone`,
`email`, and `alternate_phone`. Every other field an admin has created is
also a valid target — the write goes through the same `LeadService` write
filter that every other lead write does, so a field-level permission still
governs it.

### Worked examples

Two mappings that cover the requested "Customer Name → name, Email → email,
Company → company" cases:

```
POST /voice/extraction-mappings { "disposition_name":"Customer Name",
                                  "target_field_key":"name",
                                  "min_confidence":0.70 }
POST /voice/extraction-mappings { "disposition_name":"Email",
                                  "target_field_key":"email",
                                  "min_confidence":0.80 }
```

("Company" is not a built-in field; create it under **Settings → Fields**
first, then map to it with `target_field_key: "company"`.)

---

## 4. Confidence gating

`min_confidence` is the single safety mechanism. It is the answer to the
failure mode the contract's §6.2 spells out: a probabilistic extractor
silently overwriting a figure a human typed is exactly the kind of quiet
corruption that costs operators' trust in the whole feature.

Below the threshold, the value is **not** written to the field. It appears on
the lead's timeline as a NOTE:

> Voice extraction 'Email' below threshold: confidence 0.61 < 0.80. Value not
> written.

Above the threshold, and only above, the value is compared with the current
one and — if different — written.

Extractions with no `confidence` at all are treated as if they had failed the
gate: no write, one note.

---

## 5. What the write-back refuses to do

Three refusals that matter, and why:

1. **Never overwrite with an empty value.** An empty / null / whitespace-only
   extraction is Bolna reporting the agent did not learn anything about this
   field — not a signal to clear it. The write-back skips it silently. The
   same rule intake follows (`events/intake.py`).
2. **Never write the lead's identity field.** Rewriting the identity would
   either violate `leads_identity_uq` or, worse, silently reassign the lead
   to a different customer's record. A mapping that targets the identity key
   is refused at write-time regardless of what the CRUD lets you save.
3. **Never write a value from the disposition configured as
   `BOLNA_SUMMARY_DISPOSITION`.** A mapping that named the summary
   disposition would put the whole call recap paragraph into whatever lead
   field it targeted. The router refuses to save such a mapping (or a rename
   to that name) and the service refuses to apply one at runtime — the
   double-check exists because a deployment can change the setting after a
   mapping was already saved.

---

## 6. Overwrite behaviour

When an extraction passes the gate and the current field value is different
(and non-empty), the write happens. This matches every other lead write in
the codebase: the timeline shows the old→new delta, and undo folds it back.

The requirement was "if there is no change, keep the existing CRM value; if
there is a valid change, automatically update". No per-mapping "allow
overwrite" toggle was added because the confidence threshold *is* the
control: raise it for fields you don't want overwritten easily, lower it for
fields you do.

---

## 7. Migration and endpoints

**Migration:** `0015_voice_extraction_mappings` (revision id
`0015_voice_extraction_mappings`, down_revision `0014_voice_raw_payload`).
Additive only — creates one table, one unique constraint, one partial index,
one CHECK. Downgrade drops all of them.

**Endpoints** (all under `/api/v1/workspaces/{workspace_id}`, gated on the
`automations.manage_webhooks` capability — the existing settings-side
integration capability):

```
GET    /voice/extraction-mappings              — list
POST   /voice/extraction-mappings              — create
GET    /voice/extraction-mappings/{id}         — read one
PATCH  /voice/extraction-mappings/{id}         — update (partial)
DELETE /voice/extraction-mappings/{id}         — hard delete
```

**UI:** Settings → Voice extraction (`/settings/voice-extraction`).

The completed-webhook response gains three additive fields:

```
{
  "extraction_written":  ["Customer Name"],
  "extraction_noted":    ["Email"],
  "extraction_unmapped": ["Budget"]
}
```

A workspace with no mappings sees three empty lists — the response shape is
unchanged from what shipped in 0014.

---

## 8. What is not part of this milestone

- Tally → CRM → Bolna auto-triggering. For now, Tally form data is entered
  manually or via the existing `/intake/leads` API; the existing
  `POST /voice/calls` trigger initiates the Bolna call.
- STAGE / ACTION_FIELD extraction targets. Contract §6.1 lists them; only
  LEAD_FIELD is wired now. Adding STAGE would need `voice_extraction_mappings`
  to grow a `target_kind` column and a stage-lookup path.
- The Bolna prompt / agent configuration. The CRM sends the lead's fields in
  `user_data` (see `docs/09` §3), and the Bolna prompt decides which of those
  to reference and when to skip a question because a value is already known.
  That is a Bolna-side change, out of scope here.
