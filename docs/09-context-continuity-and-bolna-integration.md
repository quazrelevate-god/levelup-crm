# 09 — Automatic context capture and conversation continuity (Bolna)

This is the concrete build guide for the two things asked for on top of
`docs/06-voice-integration-contract.md`:

1. **Automatic context collection** — every call automatically carries the
   lead's current CRM data into Bolna, with no manual re-entry.
2. **Conversation continuity** — calling the same lead a second time carries
   forward what happened on the first call, so the agent doesn't re-introduce
   itself or re-ask what it already knows.

Nothing here changes the frozen sections of `06-voice-integration-contract.md`.
Continuity is an application of the same mechanism (`user_data` in, mapped
extraction out) — not a new Bolna primitive. Bolna itself is stateless between
calls; there is no session Bolna remembers on its side. Continuity is entirely
a CRM responsibility: store a compact summary after each call, and feed it back
in as context on the next one.

Repo state as of this doc: `feat/voice-agent` is identical to `main`
(`b672277`), M0–M11 complete, nothing voice-specific written yet. Everything
below is new work.

---

## 1. The mental model, in one picture

```
Lead in CRM                                          Same lead, second call
─────────────                                         ──────────────────────
 full_name: Asha R.                                    full_name: Asha R.
 course: Foundations                                    course: Foundations
 budget: (unset)                                        budget: 45000          ← written back from call 1
 last_call_summary: (unset)                             last_call_summary:     ← written back from call 1
                                                          "Asked about EMI options,
                                                           call back after 6pm"
        │                                                        │
        │ build user_data                                        │ build user_data
        ▼                                                        ▼
 POST /call { user_data: {                              POST /call { user_data: {
   full_name, course,                                      full_name, course, budget,
   crm_lead_id, ... } }                                     last_call_summary,
                                                              crm_lead_id, ... } }
                                                                    │
                                                    agent's welcome message / prompt
                                                    reads {last_call_summary} and
                                                    opens the call acknowledging it
                                                    instead of starting cold
```

`last_call_summary` is not a special Bolna concept — it is an ordinary
workspace lead field, created by the admin like any other, populated by a
Bolna disposition mapped through `voice_extraction_mappings`, and included in
`user_data` because the Voice Agent permission template grants it `View`. The
whole continuity feature is one field plus one disposition plus one mapping
row. That's deliberate — it keeps the "no hardcoded taxonomy" rule intact.

---

## 2. What's already built vs. what this adds

From `docs/08-handoff-voice-agent.md` §5, already in place and reusable
as-is:

| Piece | Where |
|---|---|
| API keys carrying a permission template | `app/auth/api_keys.py`, `app/models/integration.py` |
| Field projection (`FieldProjectionService.project_values`) | `app/permissions/projection.py` |
| Write filtering (`FieldWriteFilter`) | same file |
| Changesets + undo | `app/services/actions.py`, `app/services/undo.py` |
| Transactional outbox pattern (claim → send → retry → DEAD) | `app/events/outbox.py`, `app/events/dispatcher.py` |
| Settings UI shell for keys/webhooks | `web/src/routes/IntegrationsPage.tsx` |
| 19 Bolna skills for Claude Code | `.claude/skills/` |

Three things from §6 of the contract are genuinely unbuilt, plus one gap the
contract doesn't mention:

1. `voice_extraction_mappings` table, service, and settings screen.
2. The trigger: lead → `user_data` → Bolna.
3. The receiver: `POST /voice/executions`.
4. **A value renderer.** The contract says outbound values must be "rendered,
   not raw" — dates in workspace tz, money with currency, dropdown option ids
   as their label. I checked `app/fields/registry.py` and `app/fields/values.py`:
   they hold `_norm_*` functions (parsing/storing input) but no `_render_*`
   counterpart. Nothing today turns a stored dropdown option id or an epoch
   timestamp into the string a voice agent should say. This needs to be
   written — it's the one piece with no existing code to lean on.

---

## 3. Data model additions

### 3.1 `voice_extraction_mappings` (from the contract, §6.1 — build as specified)

```sql
CREATE TABLE voice_extraction_mappings (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    workspace_id      UUID NOT NULL REFERENCES workspaces(id),
    disposition_name  TEXT NOT NULL,
    target_kind       TEXT NOT NULL,   -- LEAD_FIELD | STAGE | ACTION_FIELD | IGNORE
    target_key        TEXT,
    value_map         JSONB NOT NULL DEFAULT '{}',
    min_confidence    NUMERIC(3,2) NOT NULL DEFAULT 0.70,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (workspace_id, disposition_name)
);
```

Follow the existing model conventions (`TenantModel` mixin, one Alembic
revision, `app/models/integration.py` as the style reference since it's the
sibling M10 table).

### 3.2 `voice_settings` — not in the contract, needed anyway

Nothing today stores *which* Bolna account/agent a workspace calls through.
`api_keys` is the wrong table for this — those are for machine callers
authenticating *into* the CRM, not the CRM's credential *out* to Bolna. Add a
small one-row-per-workspace table:

```sql
CREATE TABLE voice_settings (
    workspace_id         UUID PRIMARY KEY REFERENCES workspaces(id),
    bolna_api_key         TEXT NOT NULL,      -- encrypted at rest, same treatment as api_keys.hashed_key
    default_agent_id      TEXT NOT NULL,
    concurrency_limit     SMALLINT NOT NULL DEFAULT 1,
    in_flight_calls        SMALLINT NOT NULL DEFAULT 0,
    create_task_on_failure BOOLEAN NOT NULL DEFAULT false,
    updated_at             TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

This resolves two of the contract's "still open" items (§9): the
`concurrency_limit` gives you something to check before enqueuing rather than
discovering Bolna's own cap as errors, and `create_task_on_failure` turns the
"does a failed call create a task?" question into workspace-level
configuration rather than a hardcoded policy, consistent with rule 2.

### 3.3 The continuity fields

Not schema — these are ordinary admin-created lead fields, created through
the settings UI like any other field, then granted `Edit` to the Voice Agent
permission template so extraction can write them, and `View` so the trigger
can read them back:

| Field key | Type | Populated by |
|---|---|---|
| `last_call_summary` | TEXT | The "Call Summary" disposition, every call, overwritten each time |
| `last_call_outcome` | DROPDOWN | The "Call Outcome" disposition |
| `last_contacted_at` | DATE | Set by the trigger itself at call time, not extraction |
| `voice_call_count` | NUMBER | Incremented by the receiver on each successful write-back |

Keep `last_call_summary` a single overwritten field, not an append-only log.
The full history already exists — every call is a `CUSTOM` timeline action
carrying the whole `extracted_data` (contract §6.4) — so the field is a
*digest for the next prompt*, not the system of record. If a scenario needs
more than one prior summary in context, concatenate the last 2–3 `CUSTOM`
voice-call actions' summaries at trigger time rather than growing the field
unboundedly.

None of these four keys may start with `crm_` (reserved, contract §4) or be
industry vocabulary — they're generic enough to ship as a suggested field set
in the Voice Agent template setup, not hardcoded into product code.

---

## 4. The trigger — lead → Bolna

Build `app/services/voice.py`. Sketch:

```python
async def trigger_voice_call(
    session: ScopedSession,
    *,
    lead_id: uuid.UUID,
    api_key: ApiKey,          # the Voice Agent key, resolved by auth dependency
) -> None:
    lead = await session.get(Lead, lead_id)
    settings = await session.get(VoiceSettings, session.workspace_id)
    if settings is None:
        raise api_error(422, "voice_not_configured", "No Bolna credentials for this workspace")

    if settings.in_flight_calls >= settings.concurrency_limit:
        raise api_error(429, "voice_concurrency_exceeded", "Too many calls in flight")

    fields = await load_workspace_fields(session)
    grants = await load_grants(
        session,
        template_id=api_key.permission_template_id,
        is_admin=False,
        all_field_keys={f.id: f.key for f in fields},
    )
    projection = FieldProjectionService(grants)
    visible = projection.project_values(lead.values)          # rule 3, unchanged
    rendered = render_for_voice(visible, fields, workspace=session.workspace)  # NEW — §2 above

    user_data = {
        **rendered,
        "crm_lead_id": str(lead.id),
        "crm_workspace_id": str(session.workspace_id),
        "crm_idempotency": str(uuid.uuid4()),
    }

    # Never call Bolna here (rule 8). Write the trigger row in this transaction;
    # a worker sends it. Mirrors app/events/outbox.py's claim/send/retry shape,
    # but against one fixed target (Bolna) instead of N subscriber endpoints.
    session.add(VoiceCallTrigger(
        lead_id=lead.id,
        agent_id=settings.default_agent_id,
        recipient_phone_number=lead.values[identity_field_key],   # E.164, rule 12
        user_data=user_data,
        status=VoiceTriggerStatus.PENDING,
    ))
```

Key points, all consequences of rules already in `CLAUDE.md`:

- **Read path unchanged.** This is `FieldProjectionService` doing exactly what
  it does for every other read — the only new code is `render_for_voice` and
  the reserved-key merge.
- **Never POST to Bolna inside the request handler.** Add a `voice_call_triggers`
  table (or a `PENDING` row shape on a reused outbox-style table) and a worker
  loop modeled on `dispatcher.py`'s claim → send → record → retry pattern,
  running in `app/workers/scheduler.py` alongside the existing outbox
  dispatch. Increment/decrement `voice_settings.in_flight_calls` around the
  send so the concurrency cap is enforced at enqueue and at completion.
- **`crm_idempotency` is generated once, per trigger, here.** It travels
  through Bolna and comes back unread on the webhook (contract only specifies
  `crm_lead_id` as load-bearing there) — keep it anyway, it's the field you'll
  want the first time two triggers race.

`render_for_voice` is the new piece: given the projected `values` dict (keyed
by field key, raw stored form) and the field definitions, produce
TTS-appropriate strings — `DATE` → formatted in the workspace timezone (rule
10), `MONEY` → amount + workspace currency code stripped to something speakable
(rule 11), `DROPDOWN`/`DEPENDENT_DROPDOWN` → the option's label, not its id,
`CHECKBOX` → "yes"/"no" rather than `true`/`false`. Absent/null values are
dropped from the dict entirely, not sent as empty strings — contract §4:
"Absent means not granted or not set," and a prompt referencing `{budget}`
must degrade gracefully when the key just isn't there.

---

## 5. The receiver — Bolna → lead

`POST /api/v1/workspaces/{workspace_id}/voice/executions`, auth via the same
API-key dependency the intake endpoints already use.

```python
async def receive_execution(session, *, api_key, payload: dict) -> dict:
    execution_id = payload["execution_id"]
    if await already_processed(session, execution_id):        # idempotency, contract §5
        return {"status": "duplicate"}

    lead_id = payload.get("user_data", {}).get("crm_lead_id")
    lead = await session.get(Lead, lead_id) if lead_id else None
    if lead is None or lead.workspace_id != session.workspace_id:
        raise api_error(422, "unknown_lead", "crm_lead_id missing or cross-workspace")

    mappings = await load_mappings(session)          # voice_extraction_mappings, keyed by disposition_name
    recorder = ActionRecorder(session, actor_id=None)  # AUTOMATION changesets have no human actor
    changeset = await recorder.open_changeset(
        source=ChangesetSource.AUTOMATION,
        summary=f"Voice call {execution_id}",
    )

    deltas, ignored, below_threshold = [], [], []
    for category in payload.get("extracted_data", {}).values():
        for disposition_name, result in category.items():
            mapping = mappings.get(disposition_name)
            if mapping is None or mapping.target_kind == "IGNORE":
                ignored.append(disposition_name)
                continue
            if result.get("validation", {}).get("is_valid") is False:
                below_threshold.append((disposition_name, result))   # never write invalid values
                continue
            if result.get("confidence", 0) < float(mapping.min_confidence):
                below_threshold.append((disposition_name, result))   # confidence gate, contract §6.2
                continue

            value = mapping.value_map.get(result.get("objective") or result.get("subjective"),
                                           result.get("objective") or result.get("subjective"))
            if mapping.target_kind == "LEAD_FIELD":
                if not write_filter.can_write(mapping.target_key):   # rule 4 — reject by name
                    below_threshold.append((disposition_name, "rejected: no Edit grant"))
                    continue
                deltas.append(FieldDelta(mapping.target_key, label=..., old=lead.values.get(mapping.target_key), new=value))
            elif mapping.target_kind == "STAGE":
                await apply_stage_change(session, lead, target_stage_id=mapping.target_key, recorder=recorder)

    if deltas:
        recorder.record_field_changes(lead, deltas)
    recorder.record_custom(
        lead,
        action_type_id=voice_call_action_type_id,     # admin-configured custom action, contract §6.4
        payload={
            "execution_id": execution_id, "status": payload["status"],
            "duration_seconds": payload.get("duration_seconds"), "cost": payload.get("cost"),
            "recording_url": payload.get("recording_url"),
            "extracted_data": payload.get("extracted_data"),
            "below_threshold": below_threshold,
        },
    )
    await session.commit()
    return {"status": "processed"}
```

This is a sketch, not a diff — match it to the real `ActionRecorder` API in
`app/services/actions.py` and the real `FieldWriteFilter` in
`app/permissions/projection.py` rather than copying it verbatim. The
non-negotiable shape is: **one changeset, `AUTOMATION` source, every write
gated by confidence and by the Edit grant, unmapped or gated values recorded
on the action payload rather than discarded.** That's what makes "undo this
entire AI call" a single click in the M7 edit report, per contract §6.3.

---

## 6. Wiring up the actual Bolna side

Use the skills already vendored in `.claude/skills/` — this project ships 19
of them, so lean on them rather than hand-rolling API calls.

1. **`create-agent`** — build the agent once per workspace (or once, if all
   workspaces share an agent and differ only by `user_data` — decide this
   based on whether prompts genuinely differ per customer). Reference
   `references/agent-config-fields.md` for the full field set.
2. **`bolna-voice-prompt`** — write the actual `system_prompt` /
   `agent_welcome_message`. This is where continuity becomes an instruction,
   not just data:

   ```
   # Context
   You have {last_call_summary} from a previous conversation with this lead,
   if one exists. If it is present, briefly acknowledge you've spoken before
   and pick up from it rather than re-introducing the company or re-asking
   what {last_call_summary} already answers. If it is absent, this is a first
   contact — use the standard opening.
   ```

   `{last_call_summary}` is a **preloaded** variable (curly braces, per the
   skill's variable notation) because it arrives in `user_data`, exactly like
   `{full_name}` or `{course}`. It is not special to Bolna; it is a lead
   field like any other, which is the point.
3. **`create-disposition`** — create at minimum "Call Outcome" (objective) and
   "Call Summary" (subjective, `text`, question something like *"In 2-3
   sentences, summarize what was discussed and any commitments made, phrased
   so it can brief the agent on the next call with this lead."*). The summary
   disposition is the one that powers continuity — its output is what lands
   in `last_call_summary`.
4. **`setup-webhook`** — point `agent_config.webhook_url` at
   `/api/v1/workspaces/{id}/voice/executions`. Use the local receiver script
   plus a tunnel for dev; whitelist Bolna's source IP `13.203.39.153` in
   whatever's in front of the endpoint in production.
5. **`make-call`** — the trigger built in §4 is a thin wrapper around exactly
   this call shape; the skill's script is useful for testing the Bolna side
   in isolation before the CRM trigger exists.

---

## 7. Build order

Sequenced so each step is independently testable against fixtures before the
next depends on it:

1. `voice_settings` + `voice_extraction_mappings` migrations and models.
2. `render_for_voice` — unit-testable with no Bolna or CRM plumbing at all;
   feed it field definitions + stored values, assert on the strings out.
3. The trigger service + `voice_call_triggers` table + worker dispatch,
   tested against a fake `Transport` (same pattern `dispatcher.py`'s tests
   already use) rather than real Bolna.
4. The receiver, tested against **fixture payloads** — the contract names
   `docs/fixtures/voice/` as the intended location and says it's "to be
   added"; create it now with 2–3 realistic execution payloads (completed
   with high-confidence extraction, completed with a below-threshold value,
   failed/no-answer) so both the receiver and, eventually, the actual Bolna
   agent can be validated against the same fixtures independently.
5. Wire an actual Bolna agent (§6) against a dev tunnel, end to end, on one
   test lead.
6. The settings UI: `voice_extraction_mappings` editor (a table matching
   dispositions to fields — model it on the existing field-settings screens),
   and a place to set `voice_settings` (Bolna key, agent id, concurrency).
7. The "Call via Voice Agent" trigger button on the lead detail page.

Test coverage to add, per the standing rule that field-permission tests are
mandatory on every read and write path: a cross-workspace isolation test for
the receiver (a payload naming a lead in another workspace must 422, never
fall through to a phone-number match — contract is explicit about this), an
idempotency test (same `execution_id` twice → second is a no-op), a
confidence-gate test (a value below `min_confidence` never reaches the field,
but does land in the action payload), and an Edit-grant test (a mapped field
the template can't Edit is rejected by name, other keys in the same payload
still apply).

---

## 8. Decisions this doc is making that the contract left open (§9)

- **Trigger = manual button, plus the API** for now, as the contract already
  assumes. Nothing above blocks adding an assignment-rule or stage-entry
  trigger later — they'd all funnel into the same `trigger_voice_call`.
- **Concurrency cap lives in `voice_settings.concurrency_limit`, enforced at
  enqueue.** Refuse (429) rather than let Bolna's own cap surface as a
  delivery failure.
- **Failed call → task is a per-workspace toggle**
  (`voice_settings.create_task_on_failure`), not a hardcoded behaviour. Keep
  it off by default; turning it on should create an ordinary task the same
  way any other automation would, not a special "voice failure task" type.

---

## 9. What continuity does *not* give you

Worth being explicit about, since "don't start a new conversation" can be
read two ways:

- **The agent will not remember the exact prior conversation verbatim.**
  There's no session Bolna holds open between calls. What it gets is a
  2–3 sentence summary you chose the shape of via the disposition's question.
  If a scenario needs more fidelity than that, put more of the last
  transcript into `user_data` (mind prompt-length and cost — this is a
  tradeoff, not a limitation to work around) or read the last `CUSTOM` voice
  action's stored transcript at trigger time and summarize the last N calls
  into one field before sending.
- **This does not merge multiple calls into one Bolna "conversation."** Every
  call is still a separate `execution_id`, a separate timeline action, and a
  separate changeset. Continuity is *informational continuity* for the agent,
  not a technical merging of calls — which is also what keeps undo working
  per-call rather than needing to unwind a multi-call session.
