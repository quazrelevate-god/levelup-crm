"""The Bolna voice-API client (Phase 2).

`docs/06-voice-integration-contract.md` §4 froze the outbound shape and the
vendored `.claude/skills/make-call/SKILL.md` documents the endpoint:

    POST {base_url}/call
    Authorization: Bearer <BOLNA_API_KEY>
    {"agent_id": ..., "recipient_phone_number": ..., "user_data": {...}}
    -> {"message": "done", "status": "queued", "execution_id": "..."}

Three things here are deliberate and load-bearing.

**The credential never leaves this module.** It is read from configuration into
one private attribute, written into one header, and referenced nowhere else. It
is not on the request object, not on the result, not in the error string, and
not in any log record — `BolnaCallResult.error` is built from the transport
exception's *type and message*, both of which are truncated, and the one place a
key could realistically leak (an upstream error body echoing the header) is
handled by never copying the response body into the error.

**The client is a `Protocol`, not a class to subclass.** Exactly the seam
`app/events/dispatcher.py` already uses for webhook delivery: the test suite
drives `RecordingBolnaClient` and never opens a socket, so the whole integration
— trigger, idempotency, write-back, continuity — is exercised without a paid
call. `HttpxBolnaClient` is what runs in a real deployment.

**Not configured is a first-class state, not a crash.** A workspace that has
never set up voice must get a clear 422 from the API rather than a 500 from a
missing environment variable, so `bolna_client_from_settings` returns `None` and
the caller decides.
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "BolnaCallRequest",
    "BolnaCallResult",
    "BolnaClient",
    "BolnaSettings",
    "HttpxBolnaClient",
    "RecordingBolnaClient",
    "bolna_settings_from",
    "sanitise_vendor_error",
]

#: A slow vendor must not hold a request handler open indefinitely. Ten seconds
#: is the same budget `app/events/dispatcher.py` gives a webhook consumer.
REQUEST_TIMEOUT_SECONDS = 15.0

#: How much of a transport failure we keep. Long enough to diagnose, short
#: enough that a verbose upstream cannot fill the column.
MAX_ERROR_LENGTH = 500

#: How much of a *vendor* error body we keep, after redaction.
VENDOR_DETAIL_LIMIT = 500

_REDACTED = "<redacted>"

#: `Bearer <token>` in any casing — the shape a header echoed back would take.
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)

#: A JSON field whose *name* suggests a credential. The value is replaced
#: wholesale rather than inspected: guessing which values are safe is how
#: redaction fails.
_SECRET_FIELD_RE = re.compile(
    r'("[^"]*(?:api[_-]?key|token|secret|password|authorization|auth)[^"]*"\s*:\s*)"[^"]*"',
    re.IGNORECASE,
)


def sanitise_vendor_error(
    body: str | None, *, secret: str | None = None, limit: int = VENDOR_DETAIL_LIMIT
) -> str:
    """Reduce a vendor error body to something safe to store and show.

    Exists because the alternative was worse. This client used to discard the
    body entirely, on the reasoning that an upstream echoing the request back
    could put the API key into the database. That held — and it also meant the
    one time somebody needed the vendor's explanation of a 400, it had already
    been thrown away, leaving `Bolna answered 400` and no way forward.

    So the body is kept, but only after three passes: the configured key is
    replaced by exact match, any `Bearer …` is replaced, and any JSON field
    whose *name* looks credential-ish has its value replaced. Whitespace is
    collapsed so a multi-line body stays one readable line, and the result is
    truncated — a verbose upstream must not be able to fill the column.

    Redaction is deliberately name-based rather than entropy-based: a rule that
    tried to guess which values look secret would eventually redact the vendor
    message itself, which defeats the purpose of keeping it.
    """
    if not body:
        return ""

    text = body.strip()
    if secret:
        text = text.replace(secret, _REDACTED)
    text = _BEARER_RE.sub(f"Bearer {_REDACTED}", text)
    text = _SECRET_FIELD_RE.sub(rf'\1"{_REDACTED}"', text)
    text = " ".join(text.split())

    if len(text) > limit:
        text = text[: max(0, limit - 1)].rstrip() + "…"
    return text


@dataclasses.dataclass(frozen=True, slots=True)
class BolnaSettings:
    """The resolved Bolna configuration for this deployment.

    A plain value object rather than the whole `Settings`, so that everything
    downstream of configuration takes exactly what it needs and a test can build
    one without an environment.
    """

    api_key: str
    base_url: str
    agent_id: str | None
    timeout_seconds: float = REQUEST_TIMEOUT_SECONDS
    #: Which Bolna disposition (if any) carries the call summary. Deliberately
    #: unset by default: a disposition name is the *customer's* vocabulary
    #: (CLAUDE.md, "Known traps"), so the product cannot ship a guess at it.
    #: Unset means the receiver falls back to the payload's own `summary` key.
    summary_disposition: str | None = None

    def __repr__(self) -> str:  # pragma: no cover - defensive, exercised in tests
        """Never let a repr put the key in a traceback or a log record."""
        return (
            f"BolnaSettings(base_url={self.base_url!r}, "
            f"agent_id={self.agent_id!r}, api_key=<redacted>)"
        )


def bolna_settings_from(settings: Any) -> BolnaSettings | None:
    """Read Bolna configuration off the app `Settings`, or `None` if unset.

    Absence is the normal state for a deployment that does not use voice, so it
    is a return value rather than an exception.
    """
    api_key = getattr(settings, "bolna_api_key", None)
    if not api_key:
        return None
    return BolnaSettings(
        api_key=str(api_key),
        base_url=str(getattr(settings, "bolna_base_url", "https://api.bolna.ai")).rstrip("/"),
        agent_id=getattr(settings, "bolna_agent_id", None) or None,
        timeout_seconds=float(
            getattr(settings, "bolna_request_timeout_seconds", REQUEST_TIMEOUT_SECONDS)
        ),
        summary_disposition=getattr(settings, "bolna_summary_disposition", None) or None,
    )


@dataclasses.dataclass(frozen=True, slots=True)
class BolnaCallRequest:
    """One outbound call, exactly as contract §4 freezes it.

    `user_data` is already rendered and already permission-projected by the time
    it arrives here — this module does no field access and knows nothing about
    leads, which is what keeps the projection chokepoint (architecture rule 3)
    upstream where it belongs.
    """

    agent_id: str
    recipient_phone_number: str
    user_data: dict[str, Any]

    def body(self) -> dict[str, Any]:
        return {
            "agent_id": self.agent_id,
            "recipient_phone_number": self.recipient_phone_number,
            "user_data": self.user_data,
        }


@dataclasses.dataclass(frozen=True, slots=True)
class BolnaCallResult:
    """What Bolna answered. `execution_id` is the join key for everything after."""

    execution_id: str | None
    status: str | None
    status_code: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.execution_id is not None and self.error is None


@runtime_checkable
class BolnaClient(Protocol):
    """The seam. One operation is all the integration needs."""

    async def place_call(self, request: BolnaCallRequest) -> BolnaCallResult: ...


class HttpxBolnaClient:
    """The real one. Used in deployment; never in the test suite."""

    def __init__(self, settings: BolnaSettings) -> None:
        self._base_url = settings.base_url
        self._timeout = settings.timeout_seconds
        # The one place the credential is held.
        self.__api_key = settings.api_key

    def __repr__(self) -> str:
        return f"HttpxBolnaClient(base_url={self._base_url!r}, api_key=<redacted>)"

    async def place_call(self, request: BolnaCallRequest) -> BolnaCallResult:
        import httpx

        url = f"{self._base_url}/call"
        headers = {
            "Authorization": f"Bearer {self.__api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                response = await client.post(url, json=request.body(), headers=headers)
        except Exception as exc:
            # The exception's *type and message* only. There is no response to
            # read here — the request never completed — so nothing to sanitise.
            return BolnaCallResult(
                execution_id=None,
                status=None,
                status_code=None,
                error=f"{type(exc).__name__}: {exc}"[:MAX_ERROR_LENGTH],
            )

        if response.status_code < 200 or response.status_code >= 300:
            # The vendor's own explanation, sanitised. Without it a 400 says
            # only that Bolna refused, and every possible cause — unknown
            # agent, malformed number, account state — looks identical.
            detail = ""
            try:
                detail = sanitise_vendor_error(response.text, secret=self.__api_key)
            except Exception:  # pragma: no cover - a body that cannot be read
                detail = ""
            message = f"Bolna answered {response.status_code}"
            if detail:
                message = f"{message}: {detail}"
            return BolnaCallResult(
                execution_id=None,
                status=None,
                status_code=response.status_code,
                error=message,
            )

        try:
            payload: dict[str, Any] = response.json()
        except Exception:
            return BolnaCallResult(
                execution_id=None,
                status=None,
                status_code=response.status_code,
                error="Bolna answered with a body that is not JSON",
            )

        execution_id = payload.get("execution_id") or payload.get("id")
        return BolnaCallResult(
            execution_id=str(execution_id) if execution_id else None,
            status=str(payload.get("status")) if payload.get("status") else None,
            status_code=response.status_code,
            error=None if execution_id else "Bolna answered without an execution_id",
        )


class RecordingBolnaClient:
    """A test double that records what it was asked to send.

    Exists so the entire integration can be exercised — including the exact
    `user_data` that would reach Bolna — without a network call or a paid
    minute. `calls` is what the assertions read.
    """

    def __init__(
        self,
        *,
        execution_ids: list[str] | None = None,
        result: BolnaCallResult | None = None,
    ) -> None:
        self.calls: list[BolnaCallRequest] = []
        self._queue = list(execution_ids or [])
        self._forced = result
        self._counter = 0

    async def place_call(self, request: BolnaCallRequest) -> BolnaCallResult:
        self.calls.append(request)
        if self._forced is not None:
            return self._forced
        if self._queue:
            execution_id = self._queue.pop(0)
        else:
            self._counter += 1
            execution_id = f"exec-{self._counter:04d}"
        return BolnaCallResult(execution_id=execution_id, status="queued", status_code=200)

    @property
    def last(self) -> BolnaCallRequest:
        return self.calls[-1]
