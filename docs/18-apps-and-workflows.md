# 18 — Connected apps and workflows

When something happens outside the CRM (a lead pays on Razorpay), the CRM
hears about it, matches it to the lead, and runs **workflows** the workspace
drew: checks ("is the amount 400?") and actions ("move to Application Fee
Paid"). Modelled on TeleCRM's Integrations + Workflows screens.

Requires migration **`0023_automations`** and the `automations` feature (on for
every workspace by default; the migration switches it on for existing ones).

---

## 1. Screens

| Where | What |
|---|---|
| **Automate → Workflows** (`/automations`) | List: trigger, on/off, total runs, last-24h runs/failures, last run. Create from a starter. |
| **Workflow editor** (`/automations/:id`) | Blocks on the left, canvas in the middle (React Flow), settings of the selected block on the right. **Test** runs it on a real lead with a pretend payment and changes nothing. **Runs** tab lists every run with its steps; *Show path* lights the path on the canvas. |
| **Automate → Apps** (`/apps`) | One card per app. Developer tools (API keys, outgoing webhooks) are linked, not mixed in. |
| **Apps → Razorpay** (`/apps/razorpay`) | Connect, 4 numbered setup steps, options, and "What Razorpay has sent" (every delivery and what became of it). |
| **Settings → AI calling rules** | The never-AI-call rule (§5). |

All of it is workspace-admin only.

## 2. Razorpay setup (what the client does)

1. Apps → Razorpay → **Connect Razorpay**. Copy the address shown.
2. Razorpay Dashboard → **Accounts & Settings → Webhooks → + Add New Webhook**:
   paste the address, make up a **Secret**, tick `payment.captured`,
   `payment.failed` (and `payment_link.paid` if payment links are used).
   Existing webhooks (e.g. TeleCRM's) keep working.
3. Paste the same secret into step 3 on the CRM screen and **Save**.
   Until a secret is saved every delivery is refused (401, logged as *Refused*).
4. Create a workflow (starter: *When a lead pays on Razorpay*), fill in the
   amount and the stage, **Save**, **Test**, switch **On**.

## 3. What a delivery does

`POST /api/v1/hooks/razorpay/{token}` — `app/routers/hooks.py`,
`app/services/apps.py::RazorpayReceiver`.

1. **Token** in the URL finds the workspace (404 if unknown, paused, or the
   feature is off). The token is redacted from logs like the Bolna key.
2. **Signature**: HMAC-SHA256 of the raw body with the saved secret must equal
   `X-Razorpay-Signature`. Otherwise 401, logged as `REJECTED` without the body.
3. **Normalise** into `amount` (rupees, "400" / "400.50"), `currency`, `phone`,
   `email`, `name`, `payment_id`, `order_id`, `method`, `description`,
   `error_reason`, `notes`.
4. **Match the lead**, first hit wins, never a guess:
   `notes.crm_lead_id` / `notes.lead_id` → exactly one lead with that phone
   (any PHONE field, identity first, normalised with the workspace country
   code) → exactly one with that email. Two leads sharing a value = no match.
5. **Claim** the payment id (`INSERT … ON CONFLICT DO NOTHING` on a partial
   unique index). `payment.captured`, `payment_link.paid` and `order.paid` for
   one payment share the trigger, so the workflows run **once**. Later copies
   are logged as `DUPLICATE`.
6. No lead: logged `NO_LEAD` (200, so Razorpay does not retry/disable), or a
   lead is created when *Create a new lead when a payment matches nobody* is on
   (only when the phone is the identity field).
7. A timeline **note** ("💳 Razorpay: payment received — INR 400 (pay_…, upi)"),
   then every switched-on workflow for the trigger runs.

| Razorpay event | Trigger |
|---|---|
| `payment.captured`, `payment_link.paid`, `order.paid` | `razorpay.payment_captured` — *Payment received* |
| `payment.failed` | `razorpay.payment_failed` — *Payment failed* |
| anything else | logged `IGNORED` |

## 4. Workflows

`app/services/workflows.py`. Stored as JSONB in the canvas's own shape:
`{nodes:[{id,type,position,data}], edges:[{id,source,sourceHandle,target}]}`.

| Block | Exits | Settings |
|---|---|---|
| `trigger` | out | which trigger |
| `condition` — *If the payment…* | yes / no | rules `{variable, operator, value}`, all/any. `notes.<key>` reads a payment note. |
| `lead_condition` — *If the lead…* | yes / no | a filter-DSL rule on the lead (stage, any field, owner) |
| `update_fields` | out | `[{field_key, value}]`; `{{amount}}`-style variables allowed |
| `update_stage` | out | stage (and lost reason for the lost stage) |
| `add_note` | out | text with variables |

Operators: equals, does not equal, is more than, is less than, contains, does
not contain, is empty, is not empty. Numbers compare as numbers ("400" equals
"400.00"); text ignores case.

**Running.** From the trigger, follow edges; each block runs once; an exit
linked to several blocks runs all of them, top of the canvas first. Every
change goes through `LeadService` in one `AUTOMATION` changeset per run, so it
is on the timeline and undoable. A step that fails (a value the field refuses)
is recorded as failed and its branch stops; sibling branches continue; the run
is `FAILED`. A variable that comes out blank never wipes a field.

**Draft vs on.** A drawing must always be well-formed to save. It must be
complete (every block filled in, the trigger linked) to switch **on**; the
missing pieces are listed on the editor and the list.

**Test.** `POST /workflows/{id}/test {lead_id, data, graph?}` runs the on-screen
drawing in a savepoint that is always rolled back. Nothing is saved, no run is
recorded.

## 5. Never AI-call rule

`workspace.settings.voice_never_call` — a filter-DSL rule, edited at Settings →
AI calling rules. `VoiceCallService.trigger` checks it before anything is
written or sent, so the lead panel, batches, and anything later all obey it:
`409 lead_excluded_from_ai_calls`. Test calls (to the tester's phone) are not
affected. Typical rule: *Application Fee Status is Paid*.

## 6. API

Tenant routes (admin, `automations` feature):
`GET /automation/catalogue`, `GET /apps`, `POST /apps/{provider}/connect`,
`PATCH /apps/{provider}` (`is_active`, write-only `secret`, `settings`),
`POST /apps/{provider}/rotate-url`, `GET /apps/{provider}/events`,
`GET|POST /workflows`, `GET|PATCH|DELETE /workflows/{id}`,
`POST /workflows/{id}/test`, `GET /workflows/{id}/runs`,
`GET|PUT /settings/voice-never-call`.

Public: `POST /api/v1/hooks/razorpay/{token}`.

## 7. Not yet (later plan steps)

- **Time delay** blocks and anything scheduled need the background worker
  (plan step 3); production has none yet.
- **Call API** (outgoing HTTP) blocks would go through the outbox, which also
  needs the worker.
- More triggers (lead created, stage changed, AI call finished, Calendly
  booked) plug into the same engine: add a `TriggerSpec` and call
  `WorkflowEngine.run_trigger` where the event happens.
