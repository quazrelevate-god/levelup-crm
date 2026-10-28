# 15 — Personalised calls you can check before anyone is called

**Status: additive** on docs/09–14. One column (`voice_call_executions.is_test`,
revision `0018_voice_test_calls`), two read endpoints, one option on the call
trigger; and the per-lead approval switch (§4: two columns on `leads`, revision
`0019_voice_approval`, one endpoint). A workspace that uses none of it behaves as before, except that empty
fields now reach the agent as blanks (§1).

## 1. What the agent is told

Every lead field the caller may view is sent on every call, under its key —
`{name}`, `{program}`, `{age}`, `{application_fee_status}` — **empty when the
lead has no value**.

This deliberately changes contract §4 of docs/06 ("absent means not granted or
not set"). *Not granted* is unchanged: a field the caller's template cannot
View is still absent, everywhere. *Not set* is now an empty string, because
Bolna leaves a missing variable in the prompt as the literal `{age}`, which a
model can read as a blank to fill in. An empty value reads as "not known", and
the agent's script must say so rather than guess (`test_voice_preview_and_test_calls.py`).

The CRM's own `crm_*` variables (last call's summary, call count, why this call
is being made…) are unchanged: present when known, absent otherwise.

**Settings → Agent variables** (`GET /voice/variables`) lists every variable a
script can use, from one catalogue (`app/services/voice_variables.py`). A test
asserts that every key a real call sends is in it, so the page and the calls
cannot drift apart.

Calendar dates (`DATE` fields) are spoken exactly as stored: `20 October 2026`,
in every timezone. They used to be read as midnight UTC and converted, which
invented "at 05:30" in India and moved the date back a day west of UTC.

## 2. Preview — "What the agent will know"

`POST /voice/calls/preview` `{lead_id | phone, followup?}` returns the exact
`user_data` a call to that lead would send, built by the same code, plus the
list of variables that would arrive blank. Nothing is written, Bolna is not
contacted, and no wallet, calling-hours or configuration check applies.
Gated on `calling.log_calls`, like placing the call.

## 3. Test calls

`POST /voice/calls` with `test_phone` dials that number instead of the lead's,
carrying the lead's full context — the tester hears exactly what the lead
would hear. The number is normalised with the workspace's country code.

A test call is a real call in every way but one:

| | Test call |
|---|---|
| Recorded (summary, transcript, timeline entry) | yes — the timeline entry is marked `test: true` ("AI Call · Test") |
| Feeds the lead's call memory | yes — call twice to hear the agent remember |
| Extraction write-back | yes — that is part of what is being tested |
| Charged to the wallet | yes — Bolna bills it |
| Calling hours | enforced — the test number could be anyone's |
| **Moves the lead's stage** | **never** — it was a tester on the phone, not the lead |

Use a test lead with made-up details for testing; a test call on a real lead
writes its summary into that lead's memory.

On the lead panel: **Test this call** → *What the agent will know*, and
*Test call to my phone*.

## 4. Approval for AI calling

A batch reaches many people nobody has looked at one by one. The switch on
the lead panel, **Approved for AI calling**, is that look: open the lead, check
*What the agent will know*, and switch it on.

- `PUT /voice/approvals/{lead_id}` `{approved: true | false}` →
  `{lead_id, approved, approved_at, approved_by_id, changed}`. Gated on
  `calling.log_calls`, like placing a call, and only on a lead the member can
  see. A Caller sees their own leads and unassigned ones.
- Every lead read carries `voice_approved_at` (NULL = not approved) and
  `voice_approved_by_id`. New and imported leads start unapproved.
- Each flip is one timeline note ("Approved for AI calling." / "Removed from
  AI calling.") in its own changeset. Setting it to where it already is writes
  nothing (`changed: false`).
- **Batches dial approved leads only.** The pilot runner
  (`tools/call_batch.py`) skips unapproved leads and says how many it skipped.
- **The AI Call button does not need it.** Pressing it is choosing that lead
  there and then.
