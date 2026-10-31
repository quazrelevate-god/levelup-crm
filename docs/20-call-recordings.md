# 20 — Call recordings and how a call ended

Every AI call Bolna places is recorded. The CRM plays it on the **call page**
(Recording card) and in the lead panel's **AI Call Summary**, and says how the
call ended: who hung up, the carrier's reason in plain words, how long it rang.

No migration: the link and the ending are read from the Bolna result the CRM
already stores (`voice_call_executions.raw_payload.telephony_data`), so calls
made before this change have recordings too.

## Why the CRM streams the audio

Bolna's `recording_url` (`https://api.bolna.ai/recordings/call/<id>`) opens for
anyone who has it, with no key, and redirects to its S3 storage. So:

- The link is **never sent to a browser.** It is redacted from the call's raw
  payload (`voice_history._PRIVATE_KEYS`), and the call only says
  `has_recording: true`.
- `GET /workspaces/{ws}/voice/calls/{call_id}/recording` streams the audio
  behind the same sign-in, `view_call_history` permission and cross-workspace
  404 as the call page. The browser fetches it with the session token and plays
  it from memory (`fetchBlob`); `Cache-Control: private`.
- **Only Bolna is fetched.** The link must be HTTPS on a host in
  `VOICE_RECORDING_HOSTS` (default `api.bolna.ai`); redirects are followed by
  hand, at most 3, and only to that list or `*.amazonaws.com`. Anything else is
  refused before a request is made — the URL arrives in a webhook body, and the
  server must never be talked into fetching an internal address.

## How it ended

`call_ending()` reads `hangup_by`, `hangup_reason` and `ring_duration`.
Bolna reports the side as the carrier does (`Caller` / `Callee`); the CRM turns
that into `lead` or `agent` using the call's direction (outbound: the callee is
the lead; inbound: the caller is). The screen turns common reasons into plain
words — e.g. `inactivity_timeout` → "Nobody spoke for a while (silence
timeout)", `subscriber absent` → "Phone switched off or not reachable".
