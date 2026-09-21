"""Post-call automation: normalise a Bolna delivery, then summarise it.

Two small, pure pieces that `VoiceCallService.handle_execution` composes:

    Bolna webhook body ──▶ normalise_execution() ──▶ NormalizedCallResult
                                                          │
                              summarize_call(summarizer) ◀┘
                                     │
                                     ▼
                     CallSummary(text, source=AI|FALLBACK, error)

**Why a normaliser.** Bolna's execution object (`GET /executions/{id}`, and the
webhook body, which Bolna documents as the same shape) spells things its own
way — `conversation_duration`, `user_number`, `context_details.recipient_data`
— and earlier fixtures in this repo guessed other spellings. Every read goes
through here, with the documented spelling first and the historical guesses
after it, so the rest of the service handles one internal shape and a vendor
rename is a one-line change.

**Why the summariser is Bolna's.** The CRM has no LLM integration of its own,
and the Bolna agent already runs one: with summarisation enabled on the agent,
the execution carries an LLM-written `summary` of the transcript. Reusing it
avoids a second AI vendor, a second credential and a second place a transcript
is sent. `CallSummarizer` is the seam — the same `Protocol` shape as
`BolnaClient` — so the test suite can drive success and failure, and a
CRM-side model can replace it later without touching the webhook.

**Nothing is ever invented.** No summary means the fallback text, which says
exactly what was and was not received. A transcript is never truncated into a
"summary": that would put a wall of dialogue on the timeline and, worse, into
the next call's prompt.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "CALLER_PHONE_PATHS",
    "DIRECTION_PATHS",
    "DISCONNECT_STATUSES",
    "DURATION_PATHS",
    "EXTRACTION_SOURCES",
    "FALLBACK_FAILED",
    "FALLBACK_NO_SUMMARY",
    "FALLBACK_NO_TRANSCRIPT",
    "MAX_SUMMARY_LENGTH",
    "PHONE_PATHS",
    "SUMMARY_SOURCE_AI",
    "SUMMARY_SOURCE_FALLBACK",
    "TERMINAL_FAILURE_STATUSES",
    "TERMINAL_SUCCESS_STATUSES",
    "CallSummarizer",
    "CallSummary",
    "Extraction",
    "NormalizedCallResult",
    "VendorCallSummarizer",
    "dig",
    "extractions_from_payload",
    "first_present",
    "flatten_extractions",
    "normalise_execution",
    "summarize_call",
]

#: Bolna's own vocabulary, kept as *their* strings rather than mapped into a
#: CRM enum — a vendor is entitled to add statuses, and a lossy mapping loses
#: the diagnosis. Bolna documents exactly these as terminal.
TERMINAL_SUCCESS_STATUSES = frozenset({"completed"})
TERMINAL_FAILURE_STATUSES = frozenset(
    {
        "failed",
        "error",
        "busy",
        "no-answer",
        "no_answer",
        "canceled",
        "cancelled",
        "stopped",
        "balance-low",
        "call-disconnected",
    }
)

#: Terminal statuses that describe *how a call ended*, not *whether it
#: connected*. `call-disconnected` is what Bolna reports when the far end hangs
#: up — which is how most answered calls end. It stays in
#: `TERMINAL_FAILURE_STATUSES` (a disconnect with no conversation really is a
#: failed call), but a disconnect with conversation evidence is a completed one.
#: See `NormalizedCallResult.had_conversation`.
DISCONNECT_STATUSES = frozenset({"call-disconnected"})

#: Where the customer's number is. Documented spellings first
#: (`telephony_data.to_number`, `user_number`); the rest are the defensive
#: guesses earlier revisions read, kept so nothing that matched still misses.
PHONE_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "to_number"),
    ("user_number",),
    ("telephony_data", "recipient_phone_number"),
    ("telephony_data", "to"),
    ("telephony_data", "recipient"),
    ("recipient_phone_number",),
    ("to_number",),
    ("context_details", "recipient_phone_number"),
)

#: The caller's number on an inbound call.
CALLER_PHONE_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "from_number"),
    ("telephony_data", "from"),
    ("from_number",),
)

#: How long the parties spoke. Bolna documents `conversation_duration` (seconds)
#: and `telephony_data.duration` (a numeric *string*); `conversation_time` is
#: the spelling an earlier vendored skill used.
DURATION_PATHS: tuple[tuple[str, ...], ...] = (
    ("conversation_duration",),
    ("conversation_time",),
    ("telephony_data", "duration"),
    ("telephony_data", "call_duration"),
    ("duration_seconds",),
    ("duration",),
)

DIRECTION_PATHS: tuple[tuple[str, ...], ...] = (
    ("telephony_data", "call_type"),
    ("telephony_data", "direction"),
    ("direction",),
    ("call_type",),
)

STARTED_AT_PATHS: tuple[tuple[str, ...], ...] = (
    ("initiated_at",),
    ("created_at",),
)
ENDED_AT_PATHS: tuple[tuple[str, ...], ...] = (("updated_at",),)

#: `CallLogCreate` bounds duration at 0..86_400; a call log written by the
#: webhook must satisfy the same bounds a human's would.
MAX_CALL_SECONDS = 86_400

#: Long enough for a real paragraph, short enough to stay a timeline entry.
MAX_SUMMARY_LENGTH = 1_500

SUMMARY_SOURCE_AI = "AI"
SUMMARY_SOURCE_FALLBACK = "FALLBACK"

FALLBACK_NO_TRANSCRIPT = (
    "AI call completed. Call data was received, but a transcript was not available."
)
FALLBACK_NO_SUMMARY = (
    "AI call completed. Call data and transcript were received, "
    "but an automatic summary was not available."
)
FALLBACK_FAILED = "AI call ended without a conversation (status: {status})."


def dig(payload: dict[str, Any], path: tuple[str, ...]) -> Any:
    """Follow a path through nested dicts, or return None."""
    current: Any = payload
    for key in path:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
        if current is None:
            return None
    return current


def first_present(payload: dict[str, Any], paths: tuple[tuple[str, ...], ...]) -> Any:
    for path in paths:
        found = dig(payload, path)
        if found not in (None, ""):
            return found
    return None


def _clean_text(value: Any) -> str | None:
    """Whitespace-collapsed text, or None for anything empty."""
    if value in (None, ""):
        return None
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = " ".join(text.split())
    return text or None


def _transcript(value: Any) -> str | None:
    """A transcript, keeping its line breaks — it is read as a dialogue."""
    if value in (None, ""):
        return None
    if isinstance(value, list):
        # Some providers send turns; render them as the lines Bolna would.
        lines = []
        for turn in value:
            if isinstance(turn, dict):
                role = turn.get("role") or turn.get("speaker") or ""
                content = turn.get("content") or turn.get("text") or ""
                lines.append(f"{role}: {content}".strip(": ").strip())
            else:
                lines.append(str(turn))
        text = "\n".join(line for line in lines if line)
    else:
        text = str(value)
    return text.strip() or None


def _seconds(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        seconds = round(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(seconds, MAX_CALL_SECONDS))


def _timestamp(value: Any) -> dt.datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def _vendor_summary(body: dict[str, Any], summary_disposition: str | None) -> str | None:
    """The summary Bolna's own LLM wrote, wherever this agent put it.

    1. `summary` — Bolna's documented key, present when summarisation is on.
    2. The disposition named by `BOLNA_SUMMARY_DISPOSITION` — customer
       vocabulary, so configuration and unset by default (CLAUDE.md).
    3. `context_details.summary`, which some agent configurations populate.
    """
    direct = _clean_text(body.get("summary")) if isinstance(body.get("summary"), str) else None
    if direct:
        return direct

    if summary_disposition:
        extracted = body.get("extracted_data")
        entry = extracted.get(summary_disposition) if isinstance(extracted, dict) else None
        if isinstance(entry, dict):
            value = entry.get("value")
            if value not in (None, ""):
                return _clean_text(value)
        elif entry not in (None, ""):
            return _clean_text(entry)

    detail = dig(body, ("context_details", "summary"))
    if isinstance(detail, str):
        return _clean_text(detail)
    return None


@dataclasses.dataclass(frozen=True, slots=True)
class NormalizedCallResult:
    """One Bolna delivery, in the CRM's terms. Every field may be absent."""

    execution_id: str | None
    agent_id: str | None
    #: Bolna's status, lower-cased; empty when absent.
    status: str
    recipient_phone: str | None
    caller_phone: str | None
    #: `OUTGOING` unless the payload says the customer called in.
    direction: str
    started_at: dt.datetime | None
    ended_at: dt.datetime | None
    duration_seconds: int | None
    transcript: str | None
    #: The vendor's own AI summary, if the agent produced one.
    vendor_summary: str | None
    extracted_data: dict[str, Any]
    #: `user_data` and Bolna's `context_details.recipient_data` merged — the
    #: second is where Bolna actually echoes what the trigger sent.
    user_data: dict[str, Any]
    error_message: str | None

    @property
    def had_conversation(self) -> bool:
        """Whether the parties actually spoke, judged from evidence.

        A non-empty transcript or a positive talk time. This is what separates
        a disconnect after a real conversation from one that never connected —
        the status string alone cannot, because Bolna reports both as
        `call-disconnected`.
        """
        return bool(self.transcript) or (self.duration_seconds or 0) > 0

    @property
    def succeeded(self) -> bool:
        if self.status in TERMINAL_SUCCESS_STATUSES:
            return True
        # Production, 2026-09-20: an 83-second conversation that the customer
        # ended arrived as `call-disconnected` and was logged as "ended without
        # a conversation". The transcript was right there.
        return self.status in DISCONNECT_STATUSES and self.had_conversation

    @property
    def failed(self) -> bool:
        return self.status in TERMINAL_FAILURE_STATUSES and not self.succeeded

    @property
    def terminal(self) -> bool:
        # A payload carrying a summary is a finished call whatever the status
        # says — a vendor may add a status this release has never seen, and
        # silently dropping the summary would be the worse failure.
        return self.succeeded or self.failed or self.vendor_summary is not None

    @property
    def crm_lead_id(self) -> str | None:
        value = self.user_data.get("crm_lead_id")
        return str(value) if value not in (None, "") else None


def normalise_execution(
    body: dict[str, Any], *, summary_disposition: str | None = None
) -> NormalizedCallResult:
    """Reduce a Bolna delivery (already a dict) to `NormalizedCallResult`."""
    execution_id = body.get("execution_id") or body.get("id")
    direction_raw = str(first_present(body, DIRECTION_PATHS) or "").strip().lower()

    user_data: dict[str, Any] = {}
    recipient_data = dig(body, ("context_details", "recipient_data"))
    if isinstance(recipient_data, dict):
        user_data.update(recipient_data)
    if isinstance(body.get("user_data"), dict):
        user_data.update(body["user_data"])

    extracted = body.get("extracted_data")
    recipient = first_present(body, PHONE_PATHS)
    caller = first_present(body, CALLER_PHONE_PATHS)

    return NormalizedCallResult(
        execution_id=str(execution_id) if execution_id else None,
        agent_id=str(body["agent_id"]) if body.get("agent_id") else None,
        status=str(body.get("status") or "").strip().lower(),
        recipient_phone=str(recipient) if recipient is not None else None,
        caller_phone=str(caller) if caller is not None else None,
        direction="INCOMING" if direction_raw in ("inbound", "incoming", "in") else "OUTGOING",
        started_at=_timestamp(first_present(body, STARTED_AT_PATHS)),
        ended_at=_timestamp(first_present(body, ENDED_AT_PATHS)),
        duration_seconds=_seconds(first_present(body, DURATION_PATHS)),
        transcript=_transcript(body.get("transcript")),
        vendor_summary=_vendor_summary(body, summary_disposition),
        extracted_data=extracted if isinstance(extracted, dict) else {},
        user_data=user_data,
        error_message=_clean_text(body.get("error_message")),
    )


# --- summarising --------------------------------------------------------------


@runtime_checkable
class CallSummarizer(Protocol):
    """Produce a short, factual CRM summary of one call, or None."""

    async def summarize(self, call: NormalizedCallResult) -> str | None: ...


class VendorCallSummarizer:
    """The default: the summary Bolna's own LLM wrote from the transcript.

    Only presentation is applied — whitespace, a leading "Summary:" label, and
    a sentence-boundary length cap. The content is the vendor's, unedited:
    rewording a model's output here would be inventing text of our own.
    """

    async def summarize(self, call: NormalizedCallResult) -> str | None:
        text = call.vendor_summary
        if not text:
            return None
        for label in ("summary:", "call summary:"):
            if text.lower().startswith(label):
                text = text[len(label) :].strip()
        return _cap(text) or None


def _cap(text: str) -> str:
    if len(text) <= MAX_SUMMARY_LENGTH:
        return text
    head = text[:MAX_SUMMARY_LENGTH]
    stop = head.rfind(". ")
    if stop > MAX_SUMMARY_LENGTH // 2:
        return head[: stop + 1]
    return head.rstrip() + "…"


@dataclasses.dataclass(frozen=True, slots=True)
class CallSummary:
    text: str
    #: `SUMMARY_SOURCE_AI` or `SUMMARY_SOURCE_FALLBACK`.
    source: str
    #: Why the summariser produced nothing, for diagnostics. Never the payload.
    error: str | None = None


def fallback_summary(call: NormalizedCallResult) -> str:
    # "Ended without a conversation" is a factual claim. It is only made when
    # there is no transcript and no talk time — otherwise it is false.
    if call.failed and not call.had_conversation:
        return FALLBACK_FAILED.format(status=call.status or "unknown")
    if not call.transcript:
        return FALLBACK_NO_TRANSCRIPT
    return FALLBACK_NO_SUMMARY


async def summarize_call(call: NormalizedCallResult, summarizer: CallSummarizer) -> CallSummary:
    """Summarise, and never fail the webhook doing it.

    A call that did not connect is not summarised at all — there is no
    conversation to summarise, and asking a model to would invite invention.
    Any exception from the summariser becomes the fallback plus a recorded
    error (its type name only: an exception message can quote the input).
    """
    if call.failed and not call.had_conversation:
        return CallSummary(text=fallback_summary(call), source=SUMMARY_SOURCE_FALLBACK)
    try:
        text = await summarizer.summarize(call)
    except Exception as exc:
        return CallSummary(
            text=fallback_summary(call),
            source=SUMMARY_SOURCE_FALLBACK,
            error=f"summarizer_failed: {type(exc).__name__}",
        )
    cleaned = _clean_text(text)
    if not cleaned:
        return CallSummary(
            text=fallback_summary(call),
            source=SUMMARY_SOURCE_FALLBACK,
            error="summary_unavailable",
        )
    return CallSummary(text=_cap(cleaned), source=SUMMARY_SOURCE_AI)


# --- extracted data -----------------------------------------------------------

#: Where a Bolna execution carries its extractions. `extracted_data` is the
#: documented one; the other two appear on the execution object and are read
#: only when the first is absent, so a vendor that moves them does not silently
#: empty the Call Details page.
EXTRACTION_SOURCES: tuple[str, ...] = ("extracted_data", "custom_extractions", "agent_extraction")

#: Keys a leaf node uses for the value itself. `subjective` is what Bolna's
#: own "Call Summary" extraction emits; the rest are the shapes this codebase
#: and its tests already see.
_VALUE_KEYS: tuple[str, ...] = ("value", "subjective", "answer", "text", "content", "result")

#: Keys that mark a node as a *leaf* rather than a group of extractions.
_LEAF_MARKERS: tuple[str, ...] = (*_VALUE_KEYS, "confidence", "validation")


@dataclasses.dataclass(frozen=True, slots=True)
class Extraction:
    """One extracted item, flattened out of whatever nesting Bolna used.

    Bolna groups extractions: `{"General": {"Call Summary": {"subjective": …}}}`
    is the group *General* containing the extraction *Call Summary*. An older
    reading treated the top level as the extraction name, which made "General"
    the name of something that is not an extraction at all — the whole payload
    then looked unmapped and nothing was displayable.
    """

    #: The group, when the payload had one (`General`), else `None`.
    group: str | None
    #: The extraction's own name (`Call Summary`).
    name: str
    #: `Group / Name`, or just the name when ungrouped. What an operator sees,
    #: and one of the spellings a mapping may be written against.
    path: str
    #: The meaningful value: the leaf's `value`/`subjective`/…, or the node.
    value: Any
    confidence: float | None
    #: The node exactly as it arrived, for anything the above flattened away.
    raw: Any


def _looks_like_leaf(node: Any) -> bool:
    """True when this dict is one extraction rather than a group of them."""
    if not isinstance(node, dict):
        return True
    if any(key in node for key in _LEAF_MARKERS):
        return True
    # A group's children are themselves dicts; a leaf of plain scalars is a
    # leaf even when none of the known value keys are present.
    return not any(isinstance(child, dict) for child in node.values())


def _leaf_value(node: Any) -> Any:
    """The value a leaf carries, or the node itself when it has no obvious one."""
    if not isinstance(node, dict):
        return node
    for key in _VALUE_KEYS:
        if key in node and node[key] not in (None, ""):
            return node[key]
    # A leaf that *names* its value but leaves it empty is empty — not the
    # node. Returning the whole `{"value": null, "confidence": 0.9}` here
    # would turn "nothing was extracted" into a dict, and the write-back
    # would try to store that dict in a text field.
    if any(key in node for key in _VALUE_KEYS):
        return None
    # A single-entry dict is its own value: {"anything_else": "x"} is more
    # useful read as "x".
    if len(node) == 1:
        return next(iter(node.values()))
    return node


def _confidence(node: Any) -> float | None:
    if not isinstance(node, dict):
        return None
    raw = node.get("confidence")
    if raw is None:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def flatten_extractions(extracted: Any) -> list[Extraction]:
    """Flatten Bolna's extractions to a list, one entry per extracted item.

    Handles both shapes the vendor produces, and does not care which:

    - flat — `{"Customer Name": {"value": "Asha", "confidence": 0.9}}`
    - grouped — `{"General": {"Call Summary": {"subjective": "…"}}}`

    Nothing is dropped: an entry no mapping matches still appears here, which
    is what lets the Call Details page show it instead of discarding it.
    Never raises; a shape nobody anticipated becomes one entry with the node
    as its value.
    """
    if not isinstance(extracted, dict):
        return []

    entries: list[Extraction] = []
    for key, node in extracted.items():
        name = str(key)
        if isinstance(node, dict) and not _looks_like_leaf(node):
            for child_key, child in node.items():
                child_name = str(child_key)
                entries.append(
                    Extraction(
                        group=name,
                        name=child_name,
                        path=f"{name} / {child_name}",
                        value=_leaf_value(child),
                        confidence=_confidence(child),
                        raw=child,
                    )
                )
            continue
        entries.append(
            Extraction(
                group=None,
                name=name,
                path=name,
                value=_leaf_value(node),
                confidence=_confidence(node),
                raw=node,
            )
        )
    return entries


def extractions_from_payload(body: Any) -> tuple[dict[str, Any], list[Extraction]]:
    """`(raw extractions dict, flattened entries)` from an execution body.

    Reads the first source that actually carries something, so the page shows
    what the call produced rather than only what one key name held.
    """
    if not isinstance(body, dict):
        return {}, []
    for source in EXTRACTION_SOURCES:
        candidate = body.get(source)
        if isinstance(candidate, dict) and candidate:
            return candidate, flatten_extractions(candidate)
    return {}, []
