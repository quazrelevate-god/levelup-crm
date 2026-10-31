"""Call recordings and how a call ended, read from Bolna's own result (docs/20).

Bolna records every call and puts a link in `telephony_data.recording_url`.
That link opens for anyone who has it — no key — so it is never handed to a
browser. The CRM streams the audio instead, behind the same sign-in and
`view_call_history` permission as the call itself, and the link is redacted
from the call's raw payload on the way out.

**Only Bolna's servers are fetched.** The URL arrives in a webhook body, so it
is checked before the CRM requests it: HTTPS, a host on the allowed list, and
redirects followed by hand to that list or to S3 (where Bolna keeps the files).
Anything else is refused — the CRM must never be talked into fetching an
address on its own network.

How the call ended (who hung up, the carrier's reason, how long it rang) sits
in the same block; it is what tells "switched off" from "did not pick up".
"""

from __future__ import annotations

import dataclasses
from collections.abc import AsyncIterator, Iterable, Mapping
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.errors import api_error
from app.models.voice import VoiceCallExecution

__all__ = [
    "CallEnding",
    "RecordingStream",
    "call_ending",
    "open_recording",
    "recording_url",
]

#: Where Bolna's files actually live, after its redirect.
_STORAGE_SUFFIX = ".amazonaws.com"
_MAX_REDIRECTS = 3
_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _telephony(call: VoiceCallExecution) -> Mapping[str, Any]:
    payload = call.raw_payload or {}
    block = payload.get("telephony_data") if isinstance(payload, Mapping) else None
    return block if isinstance(block, Mapping) else {}


def recording_url(call: VoiceCallExecution) -> str | None:
    raw = _telephony(call).get("recording_url") or (call.raw_payload or {}).get("recording_url")
    return str(raw).strip() if isinstance(raw, str) and raw.strip() else None


@dataclasses.dataclass(frozen=True, slots=True)
class CallEnding:
    #: `user` (the lead), `agent`, or the carrier's own word.
    hangup_by: str | None
    #: The carrier's reason, e.g. "Normal Hangup", "User busy", "Unallocated number".
    hangup_reason: str | None
    #: Seconds it rang before anyone answered (or it gave up).
    ring_seconds: int | None


def _int(value: Any) -> int | None:
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def call_ending(call: VoiceCallExecution) -> CallEnding:
    block = _telephony(call)

    def text(key: str) -> str | None:
        value = block.get(key)
        return str(value).strip() if value not in (None, "") else None

    return CallEnding(
        hangup_by=_who(text("hangup_by"), text("call_type")),
        hangup_reason=text("hangup_reason"),
        ring_seconds=_int(block.get("ring_duration")),
    )


def _who(raw: str | None, call_type: str | None) -> str | None:
    """`lead` or `agent` from the carrier's caller/callee.

    On an outbound call the lead is the one called; on an inbound call the
    lead is the one calling. Any other word is passed on as the carrier said it.
    """
    if raw is None:
        return None
    side = raw.strip().lower()
    inbound = (call_type or "").strip().lower() in {"inbound", "incoming"}
    if side in {"callee", "called"}:
        return "agent" if inbound else "lead"
    if side in {"caller", "calling"}:
        return "lead" if inbound else "agent"
    if side in {"user", "customer"}:
        return "lead"
    if side in {"agent", "assistant", "bot"}:
        return "agent"
    return raw


def _allowed(url: str, hosts: Iterable[str], *, storage_ok: bool) -> bool:
    parsed = urlparse(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return False
    host = parsed.hostname.lower()
    if host in {h.lower() for h in hosts}:
        return True
    return storage_ok and host.endswith(_STORAGE_SUFFIX)


@dataclasses.dataclass(slots=True)
class RecordingStream:
    response: httpx.Response
    client: httpx.AsyncClient

    @property
    def media_type(self) -> str:
        kind = self.response.headers.get("content-type", "audio/mpeg").split(";")[0]
        return kind if kind.startswith("audio/") else "audio/mpeg"

    @property
    def length(self) -> str | None:
        length: str | None = self.response.headers.get("content-length")
        return length

    async def chunks(self) -> AsyncIterator[bytes]:
        try:
            async for chunk in self.response.aiter_bytes():
                yield chunk
        finally:
            await self.response.aclose()
            await self.client.aclose()


async def open_recording(
    url: str, *, hosts: Iterable[str], transport: httpx.AsyncBaseTransport | None = None
) -> RecordingStream:
    """Start streaming a recording, refusing anything but Bolna and its storage."""
    allowed = tuple(hosts)
    if not _allowed(url, allowed, storage_ok=False):
        raise api_error(404, "no_recording", "This call has no recording the CRM can play")

    client = httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False, transport=transport)
    current = url
    try:
        for _ in range(_MAX_REDIRECTS + 1):
            request = client.build_request("GET", current)
            response = await client.send(request, stream=True)
            if response.is_redirect:
                await response.aclose()
                target = urljoin(current, response.headers.get("location", ""))
                if not _allowed(target, allowed, storage_ok=True):
                    raise api_error(
                        502, "recording_unavailable", "The recording moved somewhere unexpected"
                    )
                current = target
                continue
            if response.status_code != 200:
                await response.aclose()
                raise api_error(
                    404 if response.status_code == 404 else 502,
                    "recording_unavailable",
                    "Bolna does not have this recording (yet)",
                )
            return RecordingStream(response=response, client=client)
        raise api_error(502, "recording_unavailable", "Too many redirects fetching the recording")
    except BaseException:
        await client.aclose()
        raise
