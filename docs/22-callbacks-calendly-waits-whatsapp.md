# 22 — Callbacks, Calendly, waits, AI-call and WhatsApp blocks

Plans 4–7. Everything time-based runs on the background worker (docs/21).
Requires migration **`0025_scheduled_calls_and_waits`** and the `dateparser`
package.

## Scheduled AI calls (Plan 4)

`voice_scheduled_calls`: an AI call to place later. Made three ways:

| Source | How |
|---|---|
| `CALLBACK` | The lead asked on a call ("call me tomorrow at 5:30"). Needs **Settings → AI calling rules → Call back when the lead asks** switched on, with the field the extraction writes the time into (e.g. *Callback Time*). Off by default. |
| `WORKFLOW` | A workflow's **AI call** block. |
| `MANUAL` | A person, from the lead panel's **Next AI call** card. |

- **Reading the time** (`scheduled_calls.parse_callback`): a small translator
  for loose and Hinglish phrasing (`kal shaam 6 baje` → tomorrow 6 pm, `evening`
  → 6 pm, filler words dropped, a bare time means today or else tomorrow), then
  `dateparser` in the company's timezone, relative to when the call ended.
  Must land between 2 minutes and 60 days ahead. **Unreadable → a task for a
  person**, never a guessed call.
- **Calling hours**: a time outside them moves to the next allowed time (the
  timeline says so).
- **One waiting call per lead**: a newer schedule cancels the older one.
- **The worker** (`scheduled_call_runner.place_due_calls`, every minute) places
  what is due, **one call per company at a time** (skips a company whose last
  call is still running), through `VoiceCallService.trigger` — calling hours,
  wallet and the never-AI-call rule all apply. Outside hours → waits; never-call
  rule or no phone → SKIPPED with a timeline note; other failures → retried
  every 30 minutes, FAILED after 5 tries.
- Screens: lead panel **Next AI call** (book, change, cancel); **Call details →
  Upcoming AI calls**.

API: `GET|POST /voice/scheduled-calls`, `DELETE /voice/scheduled-calls/{id}`,
`GET|PUT /settings/voice-auto-callback`.

## Calendly (Plan 5)

Apps → Calendly. The company pastes a **personal access token** (Calendly →
Integrations & apps → API and webhooks). The CRM then:

1. reads who the token belongs to (`GET /users/me`);
2. creates a webhook subscription for `invitee.created` and `invitee.canceled`
   (organisation scope, or the user's own calendar if not an org admin), signed
   with a key only the CRM knows. Calendly webhooks need a **paid Calendly plan**.

Deliveries arrive at `POST /api/v1/hooks/calendly/{token}`, verified with
`Calendly-Webhook-Signature` (HMAC of `t.body`, at most 10 minutes old),
matched to the lead by phone (reminder number or a phone-looking answer) or
email, noted on the timeline, and start **Meeting booked** / **Booking
cancelled** workflows. Starter: *When a lead books on Calendly* → change stage.

## Waits and the AI-call block (Plan 6)

New blocks:

| Block | Does |
|---|---|
| **Wait** | Pauses the run for N minutes / hours / days (max 30 days). The run is WAITING; the worker carries it on (`resume_waiting_runs`, every 30 s). If the workflow is switched off meanwhile, the run is CANCELLED. |
| **AI call** | Books a scheduled call (`WORKFLOW`) with a purpose — ordinary, follow-up, or *paid but not booked* (`followup_booking`, sent to the agent as `crm_call_purpose`). Never dials from the webhook itself. |

Starter: **Paid but not booked → AI call after 10 minutes** — payment → fee
check → change stage and wait 10 min → *Still not booked?* (an "If the lead…"
check the admin fills in, e.g. stage is not Interview Scheduled) → AI call.

A test run shows "Would wait 10 minutes, then carry on" and continues, so the
whole path is visible without waiting.

## WhatsApp and Call API (Plan 7)

| Block | Does |
|---|---|
| **Send WhatsApp** | An approved template from the company's own number, via Meta's WhatsApp Cloud API (`app/integrations/whatsapp.py`). To `{{lead.phone}}` by default; `{{1}}`, `{{2}}` filled from any variable. Logged on the timeline. |
| **Call an API** | GET/POST/PUT/PATCH to any **https, public** address, with headers and a JSON body using variables — for Interakt, AiSensy, n8n or anything else. |

Both only ever run **in the worker** — a live run pauses them for under a
minute — so a payment webhook or a call result never waits on an outside
system. Apps → WhatsApp: phone number ID + access token (write-only), and a
**Send test** (Meta's `hello_world`).

## The "AI call finished" trigger

Every real (non-test) AI call result starts `voice.call_finished` workflows,
after the summary and extracted fields are written. Variables: `answered`
(yes/no), `duration_seconds`, `summary`, `status`, `hangup_by`, `hangup_reason`;
the lead's own fields are `{{lead.<field>}}`. Starter: *After an AI call, send a
WhatsApp*.

## Variables

Any message can use the trigger's details (`{{amount}}`, `{{start_time}}`…)
and the lead's: `{{lead.name}}`, `{{lead.phone}}`, `{{lead.email}}`, or any field
key `{{lead.<key>}}`. Unknown names come out empty, never as raw `{{…}}`.
