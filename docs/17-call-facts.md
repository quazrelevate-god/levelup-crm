# 17 — Call facts: reference details the agent is told, chosen by rule

**Status: additive.** One table (`call_fact_sets`, revision `0022_call_facts`),
four endpoints, one settings page. A workspace with no fact sets behaves exactly
as before.

## The problem

Some things an agent must say are not on the lead: when a batch starts, who
mentors it, the fee for that intake. They belong to the business, change on the
business's schedule, and differ by lead — a lead for batch 6 must hear batch 6's
dates. Technically this is *reference data with a conditional lookup* (in AI
terms, *dynamic context injection*): a table of facts, a rule picking the row,
and the row handed to the agent before the call.

## How it works

A **fact set** is a name, a **rule** (the filter DSL saved filters and
assignment rules use — empty means every lead), a **check order** (lowest
first) and up to 30 **facts**, each a label and a value.

On every call (and every preview), the first active set whose rule the lead
matches supplies its facts:

- as **variables**: each fact's key, derived from its label —
  "Batch start date" → `{{batch_start_date}}`. Every key used by any active set
  is sent, blank when this lead's set does not have it (docs/15);
- as **lines in `crm_lead_context`**, after the lead's own fields, so an agent
  uses them without any script change: `Batch start date: 12 December 2026`.

All sets' rules are evaluated in one query over the lead's row, as for
assignment rules, with system grants: a rule is the workspace's configuration,
not a read on behalf of whoever placed the call.

A fact's key may not collide with a lead field's key, a `crm_*` variable or
`call_opening` (`422 fact_name_in_use`).

## API (workspace admins)

| Method | Path | |
|---|---|---|
| GET | `/settings/call-facts` | Active sets in check order, with the rule in words |
| POST | `/settings/call-facts` | `{name, priority, conditions, facts: [{label, value}]}` |
| PATCH | `/settings/call-facts/{id}` | Any of the above, plus `is_active` |
| DELETE | `/settings/call-facts/{id}` | Archive (soft delete) |

`GET /voice/variables` lists fact keys with `kind: "fact"`.

In the app: **Settings → Call facts**. Facts are the client's own business data,
so a client's admins manage them, like fields and stages.
