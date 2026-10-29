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
