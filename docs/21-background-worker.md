# 21 — The background worker

Everything that happens **on a clock** runs in the worker, a second process
next to the API: `arq app.workers.scheduler.WorkerSettings`. Without it the CRM
still works, but nothing time-based happens.

| Job | When | What |
|---|---|---|
| `tick` | every minute | Scheduled reports that are due, in each workspace's timezone. |
| `dispatch` | every 10 s | Outgoing webhooks from the outbox. Events older than `OUTBOX_MAX_AGE_HOURS` (48) are marked DEAD with a reason instead of sent, so an outage does not end in a flood of stale events; an operator can redrive them from Settings → Integrations. |
| `heartbeat` | every minute (and on start) | Writes `crm:worker:heartbeat` in Redis. The **Operator console** shows "Background worker running · checked in Ns ago", or "not running" in red. |
| `settle_numbers` | hourly at :17 | Charges calling-number rent that fell due (docs/16), even for a company that places no calls. |

Coming next on the same worker: timed callbacks ("call me tomorrow at 5:30")
and workflow delays ("wait 10 minutes, then…").

## Migrations

Only the API migrates. `scripts/entrypoint.sh` skips `alembic upgrade head`
when `SKIP_MIGRATIONS=1`, which the worker sets — two services migrating one
database at the same moment can collide.

## Running it on Railway

A second service from the same repository:

1. Railway → the `levelup-crm` project → **+ Create → GitHub Repo →
   quazrelevate-god/levelup-crm**.
2. In the new service → **Settings**:
   - **Service name**: `worker`
   - **Source → Root Directory**: `api`
   - **Source → Branch**: `feat/voice-agent` (the same as the API)
   - **Deploy → Custom Start Command**: `arq app.workers.scheduler.WorkerSettings`
   - **Networking**: no public domain (it serves nothing).
3. **Variables**: the same as the API's (Raw Editor → copy from `api`, paste
   into `worker`), plus `SKIP_MIGRATIONS=1`. Remove `PORT` if present.
4. Deploy. Within a minute the Operator console shows the green light.

Locally `./start-levelup.sh` already starts it (`--watch app`). After changing
the list of jobs, restart it fully: `--watch` reloads code but keeps the job list
it started with.
