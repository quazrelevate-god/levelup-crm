# 13 — Post-call automation: Bolna result → AI summary → CRM call log

When a Bolna AI call ends, the CRM receives Bolna's call result, stores it,
summarises it, and puts **one "🤖 AI Call" entry** on the lead's timeline. Any
corrected lead details the agent extracted are written back. No human action is
involved after pressing **🤖 AI Call** in the lead panel.

Builds on `docs/10` (the Bolna integration), `docs/11` (the real-call runbook)
and `docs/12` (extraction mappings). Requires migration **`0016_voice_post_call`**.

---

## 1. The flow

```
Lead panel ─ 🤖 AI Call ─▶ POST /workspaces/{ws}/voice/calls
                             │  voice_call_executions row (QUEUED → DISPATCHED)
                             │  execution_id returned by Bolna
                             ▼
                     Bolna places the call
                             │
                             ▼  (one delivery per status change)
POST /api/v1/voice/bolna/{CRM_API_KEY}
   1. authenticate the key (hashed CRM API key → its workspace)
   2. normalise the payload           app/services/voice_postcall.py
   3. match the lead                  execution id → crm_lead_id → phone (§3)
   4. non-terminal status?            store status + body, answer "pending"
   5. terminal: store transcript, duration, summary on the execution row
   6. summarise                       Bolna's AI summary, or safe fallback (§5)
   7. write ONE CALL_LOGGED action    source=AI_CALL, body=summary
   8. update last_call_summary        only with a real AI summary
   9. extraction write-back           successful calls only (docs/12)
  10. completed_at set                every later delivery is a duplicate
                             │
                             ▼
Lead panel polls the timeline every 5 s (max 15 min) until the AI Call entry
for that execution_id appears, then refreshes the lead.
```

Steps 5–10 happen in one database transaction.

## 2. The endpoint

```
POST /api/v1/voice/bolna/{CRM_API_KEY}
Content-Type: application/json
<Bolna execution object>
```

- **Authentication.** The path segment is an ordinary CRM API key (Settings →
  Integrations → API keys). It is stored Argon2-hashed and is revocable.
  Its permission template also governs the extraction write-back. Bolna's agent
  config accepts only a bare `webhook_url`, which is why the key is in the path.
- **The key never reaches the logs.** Both the structured request log and
  uvicorn's access log print the path as `/api/v1/voice/bolna/<redacted>`, and
  Sentry events are redacted the same way (`app/observability.py::redact_path`).
- A header-authenticated equivalent exists for adapters:
  `POST /api/v1/workspaces/{ws}/voice/executions` with `X-API-Key`.

| Response | Meaning |
|---|---|
| `200 accepted` | First terminal delivery; everything in §1 steps 5–10 was written. |
| `200 pending` | Non-terminal status (`queued`, `ringing`, `in-progress`); status and body stored. |
| `200 duplicate` | This execution already completed. Nothing written. The body repeats the stored summary and call-log id. |
| `401` | Bad or revoked key. |
| `404` | `BOLNA_WEBHOOK_ALLOWED_IPS` is set and the source is not on it. |
| `422 missing_execution_id` | No `id` / `execution_id` in the body. |
| `422 unknown_execution` | Could not be matched to exactly one lead (§3). The body includes `reference`, which also appears in the server log. |
| `422` (validation) | The body is not a JSON object. |

`200` bodies carry `call_summary`, `summary_source` (`AI` or `FALLBACK`),
`call_log_id`, `call_id`, `lead_id`, and the extraction report fields.

## 3. Matching the result to a lead

Tried in order. The first rule that matches wins:

1. **The execution row the CRM created** when it placed the call. This is the
   normal path for every CRM-triggered call, and it cannot be redirected by
   anything else in the payload.
2. **`crm_lead_id`**, from `user_data` or from `context_details.recipient_data`.
   Bolna echoes the trigger's `user_data` in the second place. The lead is
   resolved inside the key's workspace only.
3. **The phone number**, if `BOLNA_MATCH_BY_PHONE=true` (the default). The match
   is against the workspace's **phone field**, never the identity field and
   never the name. The number is normalised with the workspace's own country
   code. **Exactly one lead must match**: two leads sharing a number is
   ambiguous and is refused, never guessed.
4. **Create a lead**, only if `BOLNA_CREATE_MISSING_LEADS=true`. **Default:
   false.** Even when enabled, a lead is created only when the phone field *is*
   the workspace's identity field.

Anything else is refused with `422 unknown_execution` and a reference. The log
line records the reference, workspace, execution id, status and **the last four
digits** of the number. It never records the payload.

## 4. Idempotency

- `voice_call_executions.external_id` is unique per workspace, and
  `completed_at` is the gate. The first terminal delivery writes; every later
  one answers `duplicate` and writes nothing: no second call log, no second
  summary, no second extraction pass, and `call_count` is unchanged.
- The execution row is read `SELECT … FOR UPDATE`, so two retries of the same
  delivery arriving together are serialised: one writes, the other sees
  `completed_at` and becomes a duplicate.
- `last_call_summary` is additionally idempotent on the execution id (see
  `VoiceContextService.update_last_call_summary`).

## 5. The summary

The CRM has no LLM of its own. The **Bolna agent's LLM** writes the summary:
with summarisation enabled on the agent, Bolna's execution object carries
`summary`. The CRM uses that summary without editing its content. It only
collapses whitespace, strips a leading `Summary:` label and caps the length at
1,500 characters on a sentence boundary. `BOLNA_SUMMARY_DISPOSITION` can name
an extraction that carries the summary instead.

The summariser is an interface (`CallSummarizer` in
`app/services/voice_postcall.py`), so a CRM-side model can replace it later
without touching the webhook.

**Nothing is invented.** When there is no summary:

| Situation | Timeline text | `summary_source` |
|---|---|---|
| Call did not connect (`no-answer`, `busy`, `failed`, …) | `AI call ended without a conversation (status: no-answer).` — the summariser is not called | `FALLBACK` |
| No transcript | `AI call completed. Call data was received, but a transcript was not available.` | `FALLBACK` |
| Transcript but no summary | `AI call completed. Call data and transcript were received, but an automatic summary was not available.` | `FALLBACK` |
| Summariser raised | same as above; `summary_error = "summarizer_failed: <ExceptionType>"` | `FALLBACK` |

A transcript is **never** used as the summary. A fallback text is **never**
written to `last_call_summary`, so it does not feed into the next call's prompt.
A summariser failure never fails the webhook: the call data is still stored and
the call log is still written.

## 6. What is stored

`voice_call_executions` (migration 0016 adds the bold columns):

| Column | |
|---|---|
| `external_id` | Bolna execution id |
| `status` / `bolna_status` | CRM status (`COMPLETED`/`FAILED`) / Bolna's raw status |
| **`transcript`** | as Bolna sent it |
| **`duration_seconds`** | `conversation_duration`, else `telephony_data.duration` |
| **`summary`**, **`summary_source`**, **`summary_error`** | exactly what the timeline shows, and why if it is a fallback |
| **`webhook_received_at`** | the latest delivery |
| **`call_action_id`** | the `CALL_LOGGED` action on the timeline |
| `raw_payload` | the latest delivery body, verbatim (database only, never logged) |
| `completed_at` | the idempotency gate |

The timeline entry is a `CALL_LOGGED` action, written by the same
`ActionWriter.record_call` the manual log-call form uses, so call reports count
AI calls as well. Its payload is the usual `direction`, `disposition_id` and
`duration_seconds`, plus `source: "AI_CALL"`, `execution_id`, `call_id`,
`call_status`, `summary_source` and `has_transcript`. Its body is the summary.
The transcript stays on the execution row, not the timeline.

Disposition: a successful call at or above the workspace's
`connected_call_min_seconds` gets the default disposition, the same rule the
manual form follows. A failed status maps to the system `No Answer` / `Number
Busy` dispositions where they exist.

## 7. Lead updates

Only **extraction mappings** (docs/12) change lead fields. They run on
successful calls only, skip empty or whitespace values, skip values equal to
what is already stored, never rewrite the identity field, and go through the
API key's field permissions. Each pass is its own undoable changeset. The
pipeline stage is **not** changed automatically; stage semantics stay with the
workspace's own configured workflow.

## 8. Configuration

| Setting | Default | |
|---|---|---|
| `BOLNA_API_KEY`, `BOLNA_AGENT_ID` | — | Already required for outbound calls. |
| `BOLNA_MATCH_BY_PHONE` | `true` | §3 step 3. |
| `BOLNA_CREATE_MISSING_LEADS` | **`false`** | §3 step 4. Changed from `true` in this release. |
| `BOLNA_SUMMARY_DISPOSITION` | unset | Only if the agent puts its summary in an extraction. |
| `BOLNA_WEBHOOK_ALLOWED_IPS` | empty | Optional source-IP allowlist. |

**In the Bolna dashboard, for the agent:**

1. **Webhook URL:** `https://<api-host>/api/v1/voice/bolna/<CRM API key>`. Use a
   key created only for this purpose.
2. **Enable call summarisation** in the agent's post-call analytics. Without it,
   every call gets the fallback text.

## 9. Local testing

Automated:

```bash
cd api && uv run pytest tests/test_voice_post_call.py tests/test_bolna_integration.py -q
```

By hand, against the local stack (`docs/11` §0–2 for `$api`, `$key`, `$ws`,
`$access`): trigger a call from the lead panel, or with
`POST $api/workspaces/$ws/voice/calls`, and note the `execution_id`. Then play
Bolna's part:

```powershell
$body = @{
  id = "<execution_id>"; status = "completed"; conversation_duration = 154
  transcript = "assistant: Hello`nuser: Yes, I'm interested"
  summary = "Confirmed interest; asked for an evening follow-up."
  telephony_data = @{ to_number = "+91XXXXXXXXXX"; call_type = "outbound" }
} | ConvertTo-Json -Depth 5
Invoke-RestMethod -Method Post -Uri "http://localhost:8000/api/v1/voice/bolna/$key" `
  -ContentType "application/json" -Body $body
```

The response is `accepted`. The lead panel shows the 🤖 AI Call entry within five
seconds. Posting the same body again returns `duplicate` and adds nothing.
