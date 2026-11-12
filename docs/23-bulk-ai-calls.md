# 23 — Bulk AI calls from the lead list

Tick leads on **Leads**, press **AI call N** (right of the quick filters),
confirm. The leads are queued and the background worker (docs/21) calls them.

## How it runs

- **10 at a time.** `voice_max_parallel_calls` (default 10) is how many queued
  AI calls may be going at once, **across every company together** — they all
  share one Bolna account, and Bolna refuses calls over the account's
  concurrency limit (10 on the current plan). The rest wait; each worker pass
  (every minute) starts as many as there are free lines, in the order the
  leads were picked.
- **Same rules as one call.** Each call goes through `VoiceCallService.trigger`:
  calling hours (outside them the queue starts when hours open), the wallet,
  the never-AI-call rule, no phone → skipped with a timeline note.
- **Reported up front.** A lead already waiting for an AI call (a callback, a
  workflow's) keeps that one; leads on the never-AI-call rule are left out; the
  result says how many of each.
- **While it runs**, a counter beside the button shows "N on calls · M waiting"
  and a **Stop** that cancels the waiting ones (calls already going finish).
  Each lead's timeline notes that it was added to, or stopped from, a bulk call.
- At most 500 leads per bulk call, like a bulk edit.

Rows are `voice_scheduled_calls` with `source = BULK`, due now.

## API

| | |
|---|---|
| `POST /voice/scheduled-calls/bulk` | `{lead_ids}` → `{queued, already_waiting, never_call, not_found, starts_at, at_once}` |
| `GET /voice/scheduled-calls/bulk` | `{waiting, on_calls, at_once}` |
| `POST /voice/scheduled-calls/bulk/stop` | `{lead_ids?}` → `{cancelled}` |

All need the calling permission (`calling.log_calls`); a caller who cannot see
every lead only queues and stops the leads they can see.
