# 11 — Running a real Bolna phone call into the CRM

Windows PowerShell. Every command is copy-pasteable. Placeholders are the only
things you edit, and none of them ever go into source.

Assumes `docs/10` is in place and migrations run through **`0014_voice_raw_payload`**.

---

## 0. One-time setup

### 0.1 Environment variables

Add to **both** `.env` and `api\.env` (the two must stay in step — see `docs/08`):

```ini
BOLNA_API_KEY=<your real Bolna key>
BOLNA_BASE_URL=https://api.bolna.ai
BOLNA_AGENT_ID=<your Bolna agent uuid>
BOLNA_MATCH_BY_PHONE=true
BOLNA_CREATE_MISSING_LEADS=true
BOLNA_WEBHOOK_ALLOWED_IPS=
```

`BOLNA_API_KEY` is only needed to *place* calls from the CRM. Receiving a call
Bolna started needs none of it — leave it blank if you only want inbound.

> Do not put the ngrok URL in either file. It is configured at Bolna's end only,
> and it changes every time you restart the tunnel.

### 0.2 A dedicated webhook key

The webhook authenticates with a CRM API key **in the URL**, because Bolna's
agent config accepts a bare `webhook_url` and nothing else. Create one key used
for nothing else, so revoking it costs you nothing.

In the app: **Settings → Integrations → API keys → Create**. Copy the plaintext
— it is shown once.

Or from PowerShell, after logging in (§2):

```powershell
$key = (Invoke-RestMethod -Method Post `
  -Uri "$api/workspaces/$ws/settings/api-keys" `
  -Headers @{ Authorization = "Bearer $access" } `
  -ContentType "application/json" `
  -Body (@{ name = "Bolna Webhook"; permission_template_id = $tpl } | ConvertTo-Json)).key
```

Keep `$key` in the session variable only. Do not echo it, do not commit it.

---

## 1. Start the CRM API

```powershell
cd D:\voiceagentcrm
docker compose up -d --wait
cd api
.\.venv\Scripts\Activate.ps1
alembic upgrade head
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Leave that window running. Everything below goes in a **second** PowerShell window.

## 2. Verify health, then log in

```powershell
$api = "http://127.0.0.1:8000/api/v1"
Invoke-RestMethod "http://127.0.0.1:8000/health" | ConvertTo-Json -Depth 4
```

Expect `status: ok`. `degraded` naming only `object_storage` is fine for this test.

```powershell
$login = Invoke-RestMethod -Method Post -Uri "$api/auth/login" `
  -ContentType "application/json" `
  -Body (@{ email = "<your email>"; password = (Read-Host "password") } | ConvertTo-Json)

$access = $login.access_token
$ws     = $login.memberships[0].workspace.id
$tpl    = $login.memberships[0].template_id
"workspace: $ws"
```

`$access` is never printed. Do not add `Write-Host $access`.

## 3. Start ngrok

```powershell
$ngrok = "C:\Users\HP\AppData\Local\Microsoft\WinGet\Packages\Ngrok.Ngrok_Microsoft.Winget.Source_8wekyb3d8bbwe\ngrok.exe"
& $ngrok http 8000
```

Leave it running in its own window.

## 4. Find the public URL

In a third window — read it from ngrok's local API rather than the banner, so
you can put it in a variable:

```powershell
$public = (Invoke-RestMethod "http://127.0.0.1:4040/api/tunnels").tunnels `
          | Where-Object proto -eq "https" | Select-Object -First 1 -Expand public_url
$public
```

## 5. Configure the Bolna webhook

```powershell
$hook = "$public/api/v1/voice/bolna/$key"

$bolnaKey = Read-Host "Bolna API key" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($bolnaKey))

Invoke-RestMethod -Method Patch `
  -Uri "https://api.bolna.ai/v2/agent/<YOUR_AGENT_ID>" `
  -Headers @{ Authorization = "Bearer $plain" } `
  -ContentType "application/json" `
  -Body (@{ agent_config = @{ webhook_url = $hook } } | ConvertTo-Json -Depth 5)

Remove-Variable plain
```

You can also paste `$hook` into the dashboard: **agent → Analytics → "Push all
execution data to webhook"**.

Sanity-check the route before spending a call. A wrong key must give 401:

```powershell
try {
  Invoke-RestMethod -Method Post -Uri "$public/api/v1/voice/bolna/crmk_wrong" `
    -ContentType "application/json" -Body '{"id":"probe","status":"queued"}'
} catch { "expected 401 -> $($_.Exception.Response.StatusCode.value__)" }
```

## 6. Make a real phone call

```powershell
$target = "+91XXXXXXXXXX"   # a number you control

$bolnaKey = Read-Host "Bolna API key" -AsSecureString
$plain = [Runtime.InteropServices.Marshal]::PtrToStringAuto(
  [Runtime.InteropServices.Marshal]::SecureStringToBSTR($bolnaKey))

$call = Invoke-RestMethod -Method Post -Uri "https://api.bolna.ai/call" `
  -Headers @{ Authorization = "Bearer $plain" } `
  -ContentType "application/json" `
  -Body (@{ agent_id = "<YOUR_AGENT_ID>"; recipient_phone_number = $target } | ConvertTo-Json)

Remove-Variable plain
$call.execution_id
```

Answer the phone, talk to the agent for **at least 30 seconds** so the call
counts as connected, then hang up.

> Prefer to route the call *through* the CRM so it carries context? Use
> `POST $api/workspaces/$ws/voice/calls` with `{"phone": "+91..."}` instead —
> that is the CALL 2 path in step 11 and it needs `BOLNA_API_KEY` set.

## 7. Verify the webhook reached the CRM

Watch ngrok's inspector at **http://127.0.0.1:4040** — you want `POST
/api/v1/voice/bolna/…` returning **200**. Expect several: Bolna sends one per
status transition, and only the terminal one writes.

Or ask the CRM directly:

```powershell
$exec = Invoke-RestMethod "$api/workspaces/$ws/voice/context?phone=$target" `
  -Headers @{ Authorization = "Bearer $access" }
$exec | ConvertTo-Json -Depth 5
```

If nothing arrived: the uvicorn window logs every request; a 401 there means the
key in the URL is wrong or revoked, and a 404 means `BOLNA_WEBHOOK_ALLOWED_IPS`
is set and did not match.

## 8. Verify the correct lead

```powershell
$exec.lead_id
$exec.identity_value      # must be $target in E.164
$exec.name
```

## 9. Verify the call was stored

```powershell
$actions = Invoke-RestMethod "$api/workspaces/$ws/leads/$($exec.lead_id)/actions" `
  -Headers @{ Authorization = "Bearer $access" }

$actions.items | Where-Object kind -eq "CALL_LOGGED" |
  Select-Object performed_at, @{n="secs";e={$_.payload.duration_seconds}},
                              @{n="dir"; e={$_.payload.direction}}
```

Expect one row, a non-zero duration, and `OUTGOING`.

## 10. Verify the summary and context

```powershell
$exec.last_call_summary
$exec.call_count          # 1
```

Empty summary? Your agent has no summary disposition. Find its real name:

```powershell
docker compose exec -T postgres psql -U crm -d crm -c `
  "select jsonb_pretty(raw_payload->'extracted_data') from voice_call_executions order by created_at desc limit 1;"
```

then set `BOLNA_SUMMARY_DISPOSITION=<that name>` in both `.env` files and restart.

**The same query answers any field question.** The `telephony_data` key names
are read defensively precisely because they are not documented in the vendored
skills — this shows you what Bolna really sends:

```powershell
docker compose exec -T postgres psql -U crm -d crm -c `
  "select jsonb_pretty(raw_payload) from voice_call_executions order by created_at desc limit 1;"
```

If `to_number` is spelled differently, tell me and I will add it to `PHONE_PATHS`.

## 11. Second call, same number

```powershell
Invoke-RestMethod -Method Post -Uri "$api/workspaces/$ws/voice/calls" `
  -Headers @{ Authorization = "Bearer $access" } `
  -ContentType "application/json" `
  -Body (@{ phone = $target } | ConvertTo-Json) |
  Select-Object lead_id, execution_id, previous_call_summary
```

`previous_call_summary` must already show call 1's summary — that is the context
going *out* to the agent. Answer and hold this call too.

## 12. Verify no duplicate lead

```powershell
$leads = Invoke-RestMethod "$api/workspaces/$ws/leads?limit=100" `
  -Headers @{ Authorization = "Bearer $access" }
($leads.items | Where-Object identity_value -eq $target).Count     # must be 1
```

## 13. Verify the second call joined the same lead

```powershell
$after = Invoke-RestMethod "$api/workspaces/$ws/voice/context?phone=$target" `
  -Headers @{ Authorization = "Bearer $access" }

$after.lead_id -eq $exec.lead_id      # True
$after.call_count                     # 2
$after.last_call_summary              # the SECOND call's summary
```

And in the database — two call logs, one customer:

```powershell
docker compose exec -T postgres psql -U crm -d crm -c `
  "select external_id, status, bolna_status, completed_at is not null as written from voice_call_executions order by created_at;"
```

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `ERROR: unknown version '3'` from ngrok | Old ngrok config. Not an app problem: `& $ngrok config upgrade`, or delete `%LOCALAPPDATA%\ngrok\ngrok.yml` and re-add your authtoken. |
| Webhook 401 | Wrong or revoked key in the URL, or the URL was built before `$key` was set. |
| Webhook 404 | `BOLNA_WEBHOOK_ALLOWED_IPS` is set and the source did not match. Behind ngrok the peer is the tunnel, so leave it empty locally. |
| Webhook 422 `unknown_execution` | No `crm_lead_id` and no usable phone number. Check `raw_payload` for where the number really is. |
| Lead created with a blank name | Expected. Bolna sent no name and the CRM will not invent one. |
| Duplicate leads | Should be impossible — `leads_identity_uq`. If it happens, the two numbers normalised differently; check the workspace's `default_country_code`. |
| Everything works, tunnel dies overnight | ngrok URLs are ephemeral. Re-run steps 3–5. Nothing in the codebase remembers the URL, by design. |

## Security notes

- The webhook key lives in a URL because Bolna supports no header or signature.
  A URL secret can leak through proxy logs and browser history. Use a dedicated
  key, HTTPS only, and revoke it the moment the tunnel is retired.
- `BOLNA_WEBHOOK_ALLOWED_IPS` is defence in depth, not the boundary. The
  documented Bolna source IP is `13.203.39.153`; verify it against current Bolna
  docs before relying on it, and leave it empty behind ngrok.
- Never commit `.env` or `api\.env`. Never echo `$access`, `$key` or the Bolna
  key to a terminal that is being recorded or shared.
