# 10 — Phase 2: the CRM ↔ Bolna integration

**Status: implemented and verified locally.** Phase 1 (`docs/09`) built the
durable per-lead voice context. This phase closes the loop around it: the CRM
hands Bolna a lead's context before a call, and folds the result of that call
back onto the *same* lead afterwards, so every subsequent call continues the
conversation instead of restarting it.

---

## 1. Files changed

Six existing files, all additive — 78 inserted lines, zero deletions.

| File | Change |
|---|---|
| `api/app/config.py` | Five `BOLNA_*` settings |
| `api/app/models/enums.py` | `VoiceCallStatus` |
| `api/app/models/__init__.py` | Export `VoiceCallExecution`, `VoiceCallStatus` |
| `api/app/main.py` | Mount `voice_router.executions_router` |
| `api/tests/test_migrations.py` | Expect revision `0013`; allow the new enum |
| `.env.example` | Document the `BOLNA_*` variables |

Two Phase 1 files were extended in place, both additive:
`api/app/models/voice.py` (added `VoiceCallExecution` alongside the untouched
`VoiceCallContext`) and `api/app/schemas/voice.py` (added the Phase 2 request /
response shapes). `VoiceCallContext`, `VoiceContextService` and migration `0012`
were **not modified**.

Nothing outside the voice surface was touched: no lead, auth, permission, field,
pipeline, dashboard or reporting behaviour changed.

## 2. New files

| File | What it is |
|---|---|
| `api/app/integrations/__init__.py` | New package for third-party clients |
| `api/app/integrations/bolna.py` | The Bolna client: `Protocol` seam, `HttpxBolnaClient`, `RecordingBolnaClient` |
| `api/app/fields/rendering.py` | `render_for_voice` — stored values → speakable strings |
| `api/app/services/voice_calls.py` | `VoiceCallService`: trigger and webhook handling |
| `api/alembic/versions/20260827_1600-0013_voice_call_executions.py` | Migration `0013` |
| `api/tests/test_bolna_integration.py` | 38 tests |

`api/app/routers/voice.py` gained the trigger endpoint and a second router for
the webhook; its Phase 1 endpoints are unchanged.

## 3. Database migrations

**One: `0013_voice_executions`** (parent `0012_voice_context`). Creates
`voice_call_executions` — one row per *call attempt*, where
`voice_call_contexts` is one row per *lead*.

Columns: `lead_id`, `external_id` (Bolna's `execution_id`), `agent_id`,
`recipient_phone`, `idempotency_key`, `status`, `bolna_status`, `context_sent`,
`attempts`, `last_error`, `dispatched_at`, `completed_at`.

Constraints that carry weight:

- **Partial unique** on `(workspace_id, external_id) WHERE external_id IS NOT
  NULL` — one Bolna execution belongs to exactly one CRM row, but many rows may
  legitimately have no external id at once (every call still in flight).
- **`completed_at`** is the idempotency gate. Not a boolean and not a status
  check: the vendored `setup-webhook` skill is explicit that one execution
  produces several deliveries as its status transitions, so the CRM must
  distinguish "we have seen this execution" from "we have already written it
  back".

Migration `0012` is untouched. `0013` was verified reversible: `upgrade head` →
`downgrade 0012` → `upgrade head`, with `voice_call_contexts` intact throughout.

## 4. New API endpoints

**`POST /api/v1/workspaces/{workspace_id}/voice/calls`** — place a call.
Session-authenticated, gated on the existing `calling.log_calls` capability.
Body takes exactly one of `lead_id` or `phone`, plus an optional `agent_id`
override.

**`POST /api/v1/workspaces/{workspace_id}/voice/executions`** — Bolna's
completion webhook. Authenticated by `X-API-Key`, which carries a permission
template, so the write-back it drives passes through the same field matrix a
person's would. The path's workspace must match the key's own (404 otherwise —
403 would confirm the workspace exists).

The Phase 1 endpoints (`GET /voice/context/{lead_id}`, `GET /voice/context?phone=`,
`PUT /voice/context/{lead_id}/summary`) are unchanged.

## 5. Bolna operations integrated

`POST {BOLNA_BASE_URL}/call` with `Authorization: Bearer <key>`, sending
`agent_id`, `recipient_phone_number` and `user_data`; reading `execution_id` and
`status` back. That is the only outbound operation.

Inbound, the CRM consumes Bolna's execution payload as its webhook body. Both
spellings of the id are accepted — `execution_id` (webhook) and `id`
(`GET /executions/{id}`) — so the same receiver handles a replayed
reconciliation fetch.

Not integrated, and deliberately: agent creation, batches, knowledge bases,
phone-number management. Those are dashboard/configuration concerns, not part of
per-call continuity.

## 6. Environment variables

| Variable | Default | Notes |
|---|---|---|
| `BOLNA_API_KEY` | unset | Unset ⇒ voice endpoints answer `422 voice_not_configured`. The API still boots. |
| `BOLNA_BASE_URL` | `https://api.bolna.ai` | |
| `BOLNA_AGENT_ID` | unset | Overridable per call |
| `BOLNA_REQUEST_TIMEOUT_SECONDS` | `15` | |
| `BOLNA_SUMMARY_DISPOSITION` | unset | Which disposition holds the summary, if the agent uses one |

`BOLNA_SUMMARY_DISPOSITION` has **no default on purpose**. A disposition name is
the customer's own vocabulary, and `CLAUDE.md` names shipping a guess at one as
the first trap in this codebase.

The key is a *deployment* credential, so it lives in configuration rather than a
workspace row: `api_keys` is for machine callers authenticating **into** the
CRM, which is the opposite direction.

## 7. Outbound flow

```
POST /voice/calls  {lead_id | phone}
  → capability check: calling.log_calls
  → resolve the EXISTING lead  (LeadService.get_lead | VoiceContextService.find_lead_by_phone)
  → VoiceContextService.get_context(lead)        ← values already View-projected
  → render_for_voice(...)                        ← dates, money, option labels
  → build user_data  =  rendered fields + crm_* reserved keys
  → write voice_call_executions row (QUEUED) and COMMIT   ← durable before the call
  → POST {base}/call
  → record execution_id, bolna_status, DISPATCHED  (or FAILED + last_error)
  → return execution_id + the exact user_data sent
```

The row is committed *before* the HTTP call — `app/events/dispatcher.py`'s
claim-then-send shape. A process that dies mid-request leaves a visible row, not
a call that may or may not have been placed with no trace either way.

**One deliberate departure from architecture rule 8.** Rule 8 governs the event
bus — fan-out to endpoints a workspace registered, where nobody is waiting on the
answer. A call trigger is a command whose `execution_id` the caller needs
synchronously to correlate the webhook, and returning it was an explicit
requirement. The durability rule 8 protects is kept by the committed row; only
the latency differs. A failed row is recorded with `status=FAILED` and its error,
visible and redrivable — **automatic retry is not built** (see §16).

## 8. Inbound flow

```
POST /voice/executions   (X-API-Key)
  → key resolves workspace + permission template
  → path workspace must match the key's        → else 404
  → execution_id required                      → else 422 missing_execution_id
  → resolve the lead:
       1. the stored voice_call_executions row      (normal path)
       2. else user_data.crm_lead_id via LeadService (workspace-scoped)
       3. else 422 unknown_execution
  → already completed_at?      → 200 {"status":"duplicate"}, nothing written
  → non-terminal status?       → record bolna_status, 200 {"status":"pending"}
  → terminal:
       summary present, call not failed
         → VoiceContextService.update_last_call_summary(external_id=execution_id)
       otherwise
         → one AUTOMATION changeset + a timeline note recording the outcome
  → set completed_at, COMPLETED | FAILED
```

**The lead is never resolved by phone number here.** Contract §5 forbids it
explicitly: a spoofed payload could otherwise write to an arbitrary lead. This is
the one place the implementation deliberately does *less* than the Phase 2 brief
asked for, and it is a security decision, not an omission.

Idempotency holds at two independent levels: `completed_at` on the execution row,
and `update_last_call_summary`'s own guard on `external_id`. Either alone covers
the ordinary retry; together they also survive out-of-order delivery, which a
single "last external id" column could not.

## 9. Context payload sent to Bolna

Two namespaces. Bare keys are the workspace's own lead-field keys, rendered for
speech, and only those the caller's template grants **View** — contract §4's PII
control. `crm_*` is reserved so CRM context can never shadow a customer's field.

```json
{
  "name": "Test Customer",
  "email": "test.customer@example.com",
  "phone": "+919876543210",
  "crm_lead_id": "24b3cfae-2372-49b8-bfc5-d9cf13c022f3",
  "crm_workspace_id": "9373a0b4-b773-4500-a41b-4c1f687908a0",
  "crm_idempotency": "7aa3f8fb-8205-4326-8554-4171492cf386",
  "crm_call_count": "1",
  "crm_is_repeat_caller": "yes",
  "crm_last_call_summary": "Customer is interested in the Python course and prefers weekend classes.",
  "crm_last_call_at": "2026-08-27T15:46:46.984228+00:00",
  "crm_stage": "New",
  "crm_recent_notes": "Customer is interested in the Python course and prefers weekend classes."
}
```

`crm_owner` appears when the lead has an assignee. Absent means not granted or
not set — never an empty string, so a prompt referencing `{budget}` degrades
rather than saying "blank".

Never sent: passwords, password hashes, JWT secrets, CRM API keys, the Bolna key,
or any internal credential. Structurally impossible rather than filtered — the
payload is built only from projected field values and the reserved ids, and the
credential exists solely inside `app/integrations/bolna.py`.

## 10. CALL 1

```
GET /voice/context/{lead}    → last_call_summary: null, call_count: 0

POST /voice/calls {"lead_id": "24b3cfae-…"}
  ← status: DISPATCHED, bolna_status: queued
    execution_id: de1a3056-71b5-4ca8-8b81-43bb0635fe10
    previous_call_summary: null
    user_data.crm_is_repeat_caller: "no"      ← no crm_last_call_summary key at all

POST /voice/executions
  {"execution_id": "de1a3056-…", "status": "completed",
   "summary": "Customer is interested in the Python course and prefers weekend classes."}
  ← {"status": "accepted", "written": true, "call_count": 1}
```

## 11. CALL 2

Triggered **by phone number**, to prove identity resolution rather than id
passing:

```
POST /voice/calls {"phone": "+919876543210"}
  ← lead_id: 24b3cfae-…                       ← the same lead
    execution_id: eac39a78-…                  ← a different execution
    previous_call_summary: "Customer is interested in the Python course…"
    user_data.crm_last_call_summary: "Customer is interested in the Python course…"
    user_data.crm_call_count: "1"
    user_data.crm_is_repeat_caller: "yes"

POST /voice/executions
  {"execution_id": "eac39a78-…", "status": "completed",
   "summary": "Confirmed Saturday 10am batch; will pay the deposit on Friday."}
  ← {"status": "accepted", "written": true, "call_count": 2}
```

## 12. Proof CALL 2 reused CALL 1's context

Asserted at the boundary that matters — not "the CRM stored it" but "the payload
Bolna received contained it". The stub server's own request log, read back:

```
$ tail -1 /tmp/bolna_requests.jsonl | jq -r '.body.user_data.crm_last_call_summary'
Customer is interested in the Python course and prefers weekend classes.
```

Same lead id on both calls, different execution ids, and CALL 1's summary
present in CALL 2's outbound body over the wire.
`test_call_two_carries_call_one_summary_into_the_bolna_payload` asserts the same
thing against the client object, so a bug that only decorated the HTTP response
could not pass.

## 13. Idempotency verification

Seven webhook deliveries were sent for two calls. Final database state:

```
 external_id     | status    | bolna_status | attempts | written_back
 de1a3056-…      | COMPLETED | completed    |        1 | t
 eac39a78-…      | COMPLETED | completed    |        1 | t

 leads for +919876543210 = 1
 voice_call_contexts     = 1
 call_count              = 2

 kind         | body
 LEAD_CREATED |
 NOTE         | Customer is interested in the Python cours…
 NOTE         | Confirmed Saturday 10am batch; will pay th…

 changesets: AUTOMATION = 2, SINGLE_EDIT = 1
```

Three redeliveries of CALL 1 each returned `{"status": "duplicate", "written":
false, "call_count": 1}`. A **late** redelivery of CALL 1 *after* CALL 2 had
completed returned `duplicate` with `last_call_summary` still CALL 2's — the
out-of-order case a single "last external id" column would have got wrong.

Two AUTOMATION changesets mean an operator can undo everything one AI call wrote,
in one click, per contract §6.3.

## 14. Test results

| Suite | Result |
|---|---|
| `test_bolna_integration.py` (new) | **38 passed** |
| `test_voice_context_api.py` (Phase 1) | 16 passed |
| `test_migrations.py` | 6 passed |
| `test_leads_api.py` | 26 passed |
| `test_intake_api.py` | 14 passed |
| `isolation/test_cross_workspace_m10.py` | 15 passed |
| `test_permissions.py` / `test_permission_matrix.py` | 25 + 10 passed |
| `test_config.py`, `test_outbox.py`, `test_worker_entrypoint.py` | 23 passed |
| `ruff check` / `ruff format --check` | clean, 170 files |
| `mypy app` (strict) | clean, 124 source files |

**173 tests run, all passing.** The new suite covers every scenario the brief
listed: phone resolution, context before the call, payload contents, credential
non-exposure (three separate tests), external id storage, same-lead write-back,
repeated webhook, multiple calls, call 2 receiving call 1's context, unknown
phone, invalid phone, unauthorized, cross-workspace isolation on both halves, no
duplicate lead creation, and persistence across a fresh session. Plus
non-terminal deliveries, failed calls, out-of-order redelivery, unknown vendor
fields, and the speech renderer.

**The full 706-test suite was not run** — see §16.

## 15. Verification status

Requirement 23 asks these to be kept apart, so they are:

| | Status |
|---|---|
| Code implementation | **Done.** Lint, format and strict typing clean. |
| Mocked integration verification | **Done.** 38 tests through the `BolnaClient` seam. |
| Live API verification | **Done.** Real `HttpxBolnaClient`, real HTTP, real Postgres, real webhook, verified in the database after a full cold restart. |
| **Real Bolna API verification** | **NOT DONE.** No credential exists in this environment — no `BOLNA_API_KEY`, and none in `.env`, `api/.env` or `.env.example`. No real call was placed and none is claimed. |
| **Docker verification** | **NOT DONE.** No Docker daemon and no `sudo` in this environment. |

The live run used an unprivileged **Postgres 14** built from `.deb` packages in
this session, not the project's Postgres 16 under Docker Compose, and a local
stub on `127.0.0.1:9099` speaking the protocol from
`.claude/skills/make-call/SKILL.md` in place of `api.bolna.ai`. The stub is a
throwaway in `/tmp`, never part of the repository, and the CRM reached it through
the real HTTP client — so everything except Bolna's own internals was genuinely
exercised.

No Docker volume was deleted, no Postgres password changed, and no CRM data of
yours destroyed: the only database touched was the throwaway one created in this
session.

## 16. Limitations

1. **No real Bolna call has ever been placed.** Everything is verified against
   the documented protocol. First contact with the live API is still ahead.
2. **No automatic retry worker.** A trigger that cannot reach Bolna leaves a
   `FAILED` row with its error — durable and visible, but redriving it is manual.
   The natural next step is a cron entry beside `dispatch` in
   `app/workers/scheduler.py`.
3. **`voice_extraction_mappings` is not built** (contract §6.1/§6.2). Disposition
   → field mapping with confidence gates remains outstanding. This phase moves
   the *summary* only; no extracted value writes to a lead field yet, so the
   confidence-gate safety property has nothing to guard.
4. **The call is a `NOTE`, not a `CUSTOM` action** (contract §6.4). The
   contracted shape needs a workspace-configured custom action type; using
   `NOTE` keeps the integration working against a bare workspace. Unchanged from
   Phase 1.
5. **No concurrency cap** (contract §9). Nothing stops a caller exceeding Bolna's
   per-account limit; it will surface as vendor errors.
6. **Webhook auth is `X-API-Key`, not `Authorization: Bearer`** as contract §5
   wrote it. The header is the mechanism M10 actually shipped. If Bolna cannot
   send a custom header, this needs a proxy or a signed-URL scheme.
7. **No settings UI.** Configuration is environment variables only; there is no
   per-workspace Bolna account, so one deployment calls through one Bolna
   account.
8. **Phase 1 and Phase 2 are uncommitted.** Everything shows as untracked or
   modified in `git status`.

## 17. Running it locally

```bash
# 1. Configuration
cp .env.example .env && cp .env .env.tmp && mv .env.tmp api/.env
# edit both: set JWT_SECRET_KEY (32+ chars), BOLNA_API_KEY, BOLNA_AGENT_ID

# 2. Stack
docker compose up -d --build --wait
curl http://localhost:8000/health          # expect "ok"

# 3. Schema and first account
cd api
uv sync
uv run alembic upgrade head                # ends at 0013_voice_executions
uv run python -m app.bootstrap --email you@example.com --name "You" --workspace "Dev"
# open the printed link, choose a password, sign in at http://localhost:5173

# 4. Checks
uv run pytest tests/test_bolna_integration.py -q
uv run ruff check . && uv run ruff format --check . && uv run mypy app
```

Then, with an access token and a lead that has a phone number:

```bash
# Create an API key for the webhook: Settings → Integrations, or
# POST /api/v1/workspaces/{ws}/settings/api-keys

# Place a call
curl -X POST "$API/workspaces/$WS/voice/calls" \
  -H "Authorization: Bearer $ACCESS" -H "Content-Type: application/json" \
  -d '{"phone": "+919876543210"}'

# Point Bolna's webhook at:
#   https://<your-host>/api/v1/workspaces/$WS/voice/executions
# with header  X-API-Key: <the key>
```

Without `BOLNA_API_KEY` set, `/voice/calls` answers `422 voice_not_configured`
and nothing else in the CRM is affected.

---

## Next steps, in the order worth doing them

1. **Place one real call** with a real key against a real agent, and reconcile
   the webhook payload against §9. Everything else is downstream of that.
2. **The retry worker** — a cron entry beside `dispatch`, and limitation 2 closes.
3. **`voice_extraction_mappings`** — the confidence-gated write-back, contract
   §6.1/§6.2. The largest remaining piece, and the one the contract calls "most
   worth getting right".
4. **The settings UI** — Bolna agent id and the extraction mapping table.
