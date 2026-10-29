# 16 — The platform operator, seats and calling numbers

**Status: additive.** One deployment setting (`PLATFORM_OPERATOR_EMAILS`), one
migration (`0020_voice_calling_numbers`), two endpoints, and locks on three
existing writes.

## Who controls what

```
Platform operator (whoever sells this deployment)
 └── Workspace (one client company)
      └── Members, each holding a licence = a seat
```

| Change | Who |
|---|---|
| Create a workspace | **Operator only** |
| A workspace's `seat_limit` | **Operator only** |
| A workspace's paid modules (`features`) | **Operator only** — on `PATCH /workspaces/{id}` and on `PATCH /settings/preferences` |
| A workspace's calling numbers | **Operator only** |
| Wallet top-ups, discounts, adjustments | Operator, from the server only (`python -m app.wallet`, docs/14) |
| Inviting members, within the seats bought | Workspace Root/Admin (a licence past `seat_limit` is a 409) |
| Fields, stages, lost reasons, dispositions, actions, templates, permissions, saved filters, name, timezone | Workspace Root/Admin |

A refused change answers `403 operator_only`. The test suite
(`tests/test_platform_operator.py`) tries each one as a client admin.

## Naming the operator

```ini
PLATFORM_OPERATOR_EMAILS=you@example.com,partner@example.com
```

Comma-separated or a JSON list; matched case-insensitively. **Empty means
nobody**, so a deployment that forgets the setting gives nothing away. Nothing
inside the app can grant operator status. `GET /me` returns
`is_platform_operator` so the web app can show operator controls.

The operator acts on a workspace's settings as a member of it. Creating a
workspace through `POST /workspaces` makes the operator its Root member; a
workspace created with `python -m app.bootstrap` does not, so invite the
operator into it before changing its seats over the API.

## Calling numbers

The numbers a workspace's AI calls go out from, bought in the operator's Bolna
account (Phone numbers; Indian numbers come from Vobiz or Plivo).

- `GET /voice/calling-numbers` → `{numbers, editable}` — workspace admins.
- `PUT /voice/calling-numbers` `{numbers: [...]}` — operator only. Numbers are
  normalised with the workspace's country code, de-duplicated, at most 20.
- Calls rotate through the list in order, one number per call, so no single
  number places every call. Empty list: Bolna's default number.
- Each call records the number it used (`voice_call_executions.caller_phone`),
  shown on the call page as "Called from". Replacing a number flagged as spam is
  one save in **Settings → Calling numbers**; the next call uses the new list.

Inbound is separate: in Bolna, one number answers with one agent, and re-mapping
it to another agent is a single change there.

## Number requests: clients rent numbers from the wallet

A client never adds a number; they ask for one.

- `POST /voice/number-requests` `{region?, note?}` — workspace admin. Takes the
  first 30 days' rent from the wallet at once (`NUMBER_RENTAL`, a debit) and
  records a `PENDING` request. `402 wallet_insufficient_balance` when the wallet
  cannot cover it (rent never takes a balance below zero); `404
  wallet_not_configured` with no wallet. The operator is emailed.
- The price is `VOICE_NUMBER_MONTHLY_PRICE` (default `850.00`, in the workspace
  currency: about $10 against Bolna's $5). A rented number keeps the price it
  was requested at.
- `POST /voice/number-requests/{id}/fulfil` `{phone_number}` — **operator only**.
  The number bought in Bolna joins the workspace's calling numbers; `ACTIVE`,
  paid for 30 days from now.
- `POST /voice/number-requests/{id}/cancel` — **operator only**. A pending
  request is refunded (an `ADJUSTMENT`); a rented number is released from the
  rotation, without a refund.
- `GET /voice/number-requests` — workspace admin: requests, price, and whether
  the caller is the operator.

**Renewals.** Production runs no background worker, so rent that has fallen due
is charged when the workspace next lists its numbers or places a call. A period
the wallet cannot cover makes the number `PAST_DUE` and takes it out of the
rotation; once the wallet covers it, it is charged and comes back, paid for 30
days from that day.

## A lead calling back

Calls carry `crm_call_direction`: `outbound` when the CRM placed the call.

For a number that answers inbound calls, point the agent's caller
identification (`ingest_source_config`, Internal API) at
`GET /api/v1/voice/bolna/lookup` with a CRM API key as the Bearer token. Bolna
calls it with `contact_number` before the agent speaks; the answer is exactly
what an outbound call to that lead would carry, marked `crm_call_direction:
inbound`, so the agent greets them by name and with their history. An unknown
number gets only `{"crm_call_direction": "inbound"}`. The key's permission
template decides which fields are shared.

## Call outcomes into lead fields

The VE agent (v6) answers five outcome questions after every call, linked to the
agent so they actually run: Fee Link Decision, WhatsApp Number (exactly 10
digits, validated), Callback Time, Reason Not Completed, Goal.
`tools/setup_voice_extraction.py` (`setup-voice-extraction.sh`) links each to a
lead field in a workspace, creating fields that are missing. An answer that
fails its validation is written to the timeline as a note, never into the field.
