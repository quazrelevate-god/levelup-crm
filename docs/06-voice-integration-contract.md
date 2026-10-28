# 06 — Voice agent integration contract

**Status: draft for review. Frozen sections are marked.**
Owner of the CRM side: developer 2. Owner of the Bolna side: developer 1.

This document exists so both sides can be built at the same time without
meeting in the middle. It is the *only* coordination artifact — if something is
not written here, it is not agreed.

---

## 0. What is being integrated

A Bolna voice agent calls a lead, holds a conversation, and the outcome lands
back on that lead in the CRM as structured data and a timeline entry.

```
CRM lead  ──1── lead context ──▶ Bolna agent ──2── places call
                                                      │
CRM lead  ◀──4── extraction ◀──3── transcript ────────┘
```

1. **Context out** — the lead's field values become the agent's per-call variables
2. **Call** — Bolna dials and runs the agent
3. **Extraction** — Bolna's dispositions turn the transcript into typed values
4. **Write-back** — those values update lead fields, stage, and the timeline

Steps 1 and 4 are CRM work. Steps 2 and 3 are Bolna work. The contract is the
payload at the boundary.

---

## 1. Correction to the original framing — read this first

The plan as first described was *"dynamic allocation of knowledge base from
lead details."* That maps onto the wrong Bolna primitive. Bolna has two, and
they are not interchangeable:

| | Knowledge base | `user_data` |
|---|---|---|
| What it is | RAG vector store over PDFs / URLs | Variables substituted into the prompt |
| Scope | Per **agent**, static | Per **call** |
| Created by | Upload, then poll `status` until `processed` | Passed inline when placing the call |
| Latency to create | Seconds to minutes, asynchronous | None |
| Lifecycle | Must be deleted explicitly (`rag_id`) | Dies with the call |

**Per-lead detail goes in `user_data`, not a knowledge base.** Building a
knowledge base per lead would mean an async document ingest and a polling loop
before every single call, a vector store per lead holding that person's data,
and a deletion obligation for each one. It would also put lead PII into a
durable third-party store, which the field-permission model is specifically
designed to control.

The knowledge base stays **static per workspace** — the customer's course
catalogue, fee structure, policies, FAQ. It changes when the business changes,
not when the lead changes. One `rag_id`, attached to the agent.

So the first feature is: **dynamic `user_data` assembled from lead field
values, resolved through the field-permission matrix.**

---

## 2. Where this attaches — and what blocks it

| Capability | Depends on | Status |
|---|---|---|
| Read lead → build context | `FieldProjectionService` | **Built** (M5) |
| Authenticate a machine caller | `api_keys` | **M10** |
| Trigger the call | transactional outbox | **M10** (rule 8 forbids calling out from a request handler) |
| Receive the extraction webhook | inbound API-key auth | **M10** |
| Write extraction back | write filter, changesets, actions | **Built** (M5–M7) |

The write-back half already exists. The transport half is M10. Note that
`CLAUDE.md:31` still says the event bus is M9 — that is stale; the milestone
list was revised from 11 to 12 and the bus moved to M10.

Nothing in this integration requires a new milestone. It is a consumer of M10.

---

## 3. Identity — what the voice agent *is*, permission-wise **[FROZEN]**

Rules 3 and 4 are absolute: every lead read projects through the field matrix,
every lead write filters through it. A machine caller is not exempt, so it needs
a resolved grant set.

**An API key carries a `permission_template_id`.** That is the whole mechanism.

```
api_keys
  id, workspace_id, name, hashed_key, prefix,
  permission_template_id  -> permission_templates(id)   # NOT NULL
  created_by, created_at, last_used_at, revoked_at
```

`load_grants(session, template_id=..., is_admin=False, all_field_keys=...)`
already takes exactly this and returns a `FieldGrants`. Nothing downstream
changes.

Consequences the admin needs to understand, and which the settings UI must say
out loud:

- A field the voice template cannot **View** never reaches Bolna. Not redacted —
  absent. This is the PII control.
- A field it cannot **Edit** is *rejected by name* on write-back, not silently
  dropped (rule 4).
- The agent is not a member. It has no availability, holds no leads, and does
  not appear in a leaderboard.

The recommended setup is a dedicated `Voice Agent` permission template with the
narrowest matrix that works: View on the handful of fields the script needs,
Edit only on the fields extraction is allowed to fill.

---

## 4. Outbound — lead context to Bolna **[FROZEN]**

> **Amended (October 2026), see docs/15 §1:** a field the caller *may* view but
> the lead has no value for is now sent as an empty string rather than omitted.
> "Not granted" is unchanged — still absent.

`POST https://api.bolna.ai/call`

```jsonc
{
  "agent_id": "<from workspace voice settings>",
  "recipient_phone_number": "<lead identity value, E.164>",
  "user_data": {
    // Every key here is a workspace lead-field key. There is no fixed list.
    // Only keys the API key's template grants View are present.
    "full_name":   "Asha R.",
    "course":      "Foundations",
    "budget":      "45000",
    "last_contact":"2026-08-14",

    // Reserved keys, always present, prefixed to avoid colliding with a
    // customer's own field keys:
    "crm_lead_id":      "3f2b…",
    "crm_workspace_id": "9a11…",
    "crm_idempotency":  "<uuid, generated per trigger>"
  }
}
```

Rules:

- **Values are rendered, not raw.** Dates in the workspace timezone, money with
  the workspace currency, dropdowns as their label — the same rendering the UI
  shows. An agent reading `1755129600` aloud is a defect.
- **Absent means not granted or not set.** Bolna must treat every non-reserved
  key as optional. A prompt referencing `{budget}` when Budget is not granted
  must degrade, not break.
- **`crm_*` keys are reserved.** The CRM rejects a lead field whose key starts
  with `crm_` at field-creation time (to be added in the M8 field validator).
- Phone normalisation uses the workspace's default country code (rule 12).

---

## 5. Inbound — extraction back to the CRM **[FROZEN]**

`POST /api/v1/workspaces/{workspace_id}/voice/executions`
Auth: `Authorization: Bearer <api key>`

Bolna's own webhook body is forwarded as-is. The parts the CRM reads:

```jsonc
{
  "execution_id": "exec_…",          // idempotency key
  "agent_id": "…",
  "status": "completed",             // completed | failed | busy | no-answer
  "transcript": "…",
  "recording_url": "…",
  "duration_seconds": 184,
  "cost": 3.2,
  "user_data": { "crm_lead_id": "3f2b…", "crm_idempotency": "…" },
  "extracted_data": {
    // keyed by DISPOSITION NAME, exactly as Bolna emits it
    "Call Outcome": {
      "value": "follow_up",
      "confidence": 0.92,
      "confidence_label": "High",
      "validation": { "is_valid": true }
    },
    "Budget": {
      "value": "45000",
      "confidence": 0.61,
      "confidence_label": "Medium",
      "validation": { "is_valid": true }
    }
  }
}
```

- `execution_id` is the idempotency key. A repeat delivery is a 200 with
  `{"status": "duplicate"}` and writes nothing. Bolna retries; the CRM must not
  double-write.
- `crm_lead_id` from `user_data` identifies the lead. If it is missing or names
  a lead in another workspace, reject 422 — never fall back to matching on
  phone number, which would let a spoofed payload write to an arbitrary lead.
- `validation.is_valid: false` means Bolna's own type check failed. The raw
  value survives in `subjective`. **Do not write it to a typed field.**

---

## 6. How extraction becomes CRM data

### 6.1 The mapping is configuration, not code **[FROZEN]**

Disposition names are the customer's vocabulary. `Call Outcome`, `Budget`,
`Handover Needed` are examples from one business and must never appear in
product code, an enum, or a migration (CLAUDE.md, "Known traps").

```
voice_extraction_mappings
  id, workspace_id,
  disposition_name  text NOT NULL,      -- as Bolna emits it
  target_kind       text NOT NULL,      -- LEAD_FIELD | STAGE | ACTION_FIELD | IGNORE
  target_key        text,               -- lead field key, or stage id
  value_map         jsonb DEFAULT '{}', -- Bolna value -> CRM value, for dropdowns
  min_confidence    numeric(3,2) DEFAULT 0.70,
  UNIQUE (workspace_id, disposition_name)
)
```

An admin builds this in settings. An unmapped disposition is recorded on the
timeline and ignored — never guessed at.

### 6.2 Confidence gates the write **[FROZEN]**

Below `min_confidence`, the value is **not written to the field**. It is
recorded on the call's timeline action, where a human can see it and act.

This is the single most important safety property in the integration. A voice
model that half-heard "forty-five thousand" must not overwrite a figure a human
typed. Silent overwrite by a probabilistic extractor is the failure mode that
would make operators distrust the whole system.

### 6.3 One call, one changeset, fully undoable **[FROZEN]**

Every write-back opens **one changeset** with `source = AUTOMATION` — a value
already present in `ChangesetSource` and so far unused. Every field change,
stage change and assignment it produces carries that `changeset_id`.

The consequence is worth stating plainly: **an operator can undo everything one
AI call wrote, in one click**, from the M7 edit report, with the M7 conflict
detection protecting any human edit made in the meantime. This is not extra
work — it falls out of obeying rule 5a.

### 6.4 The call itself is a timeline action **[FROZEN]**

`SystemActionKind.CUSTOM` with `action_type_id` pointing at a
workspace-configured custom action type. The admin creates it, names it,
gives it a score and a direction — exactly like any other custom action.

**No new `SystemActionKind` value.** `VOICE_CALL` in that enum would be
hardcoded taxonomy.

The action's payload carries `execution_id`, duration, cost, recording URL,
the full `extracted_data`, and every value that failed the confidence gate.

---

## 7. Failure and ordering

| Case | Behaviour |
|---|---|
| Bolna webhook arrives twice | Second is a no-op keyed on `execution_id` |
| Webhook arrives before the trigger commits | Reject 409; Bolna retries |
| Call fails / no answer | Action written with the status, no field writes |
| Lead deleted between trigger and webhook | Record the execution, skip the write, log it |
| Extraction maps to a field the template cannot Edit | Reject that key by name; other keys still apply |
| Bolna unreachable on trigger | Outbox retries with backoff, DEAD after 8 (M10) |

---

## 8. What each side can build today

**Developer 1 — blocked on nothing:**

- The agent itself: system prompt, welcome message, voice, transcriber, latency
  tuning, interruption handling
- The disposition set, and its `extracted_data` shape
- The static workspace knowledge base (§1)
- An adapter that consumes §4 and emits §5, against fixture JSON

**Developer 2 — after M8:**

- M10: `api_keys`, outbox, webhook endpoints, intake
- `voice_extraction_mappings` and its settings UI
- The trigger, the receiver, and the write-back
- Reserved-prefix validation on `crm_*` field keys (folded into M8)

Fixture payloads for both sides live in `docs/fixtures/voice/` — to be added
with the first implementation.

---

## 9. Decisions

**Settled 21 Aug 2026:**

- **The CRM does not store call recordings.** Bolna hosts them; operators
  retrieve them there. §5's `recording_url` is passed through to the timeline
  and nothing is copied into this product's object storage. This is the smaller
  surface: no retention period to honour, no erasure request to service, and no
  recordings in a breach.
- **Calling-hours and DNC enforcement are out of scope for the product.** No
  calling window, no suppression list, no consent model. Whoever operates a
  workspace remains responsible for how and when it dials; the CRM neither helps
  nor hinders. Worth writing down because an autonomous trigger has no judgement
  about the hour, where a human telecaller does — so this is a deliberate
  choice, not an oversight to be rediscovered later.

**Still open:**

1. **What triggers a call?** Manual button, assignment rule, stage entry, or a
   campaign. Assume manual-plus-API for v1 unless decided otherwise.
2. **Concurrency cap per workspace** — Bolna enforces one; the CRM should
   refuse to enqueue beyond it rather than discovering it as errors.
3. Does a failed call create a task for a human to follow up? Probably, but it
   is a policy, so it belongs in configuration.
