"""Rendering stored field values for speech (Phase 2).

`docs/06-voice-integration-contract.md` §4, which is frozen:

> **Values are rendered, not raw.** Dates in the workspace timezone, money with
> the workspace currency, dropdowns as their label — the same rendering the UI
> shows. An agent reading `1755129600` aloud is a defect.

So this module turns the *stored* form of a lead's values — option codes, ISO
dates, bare numbers — into the strings a text-to-speech engine should say. It is
the last step before `user_data` leaves the building, and it runs **after**
`FieldProjectionService`, never instead of it: a field the caller cannot View
never reaches this function at all (architecture rule 3).

Two rules that fall out of the contract and matter more than they look:

**An absent value is dropped, not sent empty.** §4: "Absent means not granted or
not set." A prompt that says `{budget}` must degrade when Budget is unset, and it
can only do that if the key is genuinely missing rather than present-and-blank.

**Nothing here knows any business vocabulary.** It branches on `LeadFieldType`,
which is a product concept, and reads option *labels* out of the database. No
field key, option code or disposition name appears in this file — that is the
trap `CLAUDE.md` names first.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping, Sequence
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.models.enums import LeadFieldType
from app.models.field import FieldOption, LeadField

__all__ = ["render_for_voice", "render_one"]


def _zone(timezone_name: str | None) -> dt.tzinfo:
    """The workspace's zone, falling back to UTC rather than failing a call.

    Architecture rule 10 says render in the *workspace's* configured timezone. A
    workspace carrying a zone this host has no database entry for is a
    misconfiguration worth surviving: saying the time in UTC is wrong by hours,
    while refusing to place the call is wrong entirely.
    """
    if not timezone_name:
        return dt.UTC
    try:
        return ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - defensive
        return dt.UTC


def _label_for(code: str, options: Sequence[FieldOption]) -> str:
    """An option's label, or the code itself if the option is gone.

    Mirrors `FieldValueService.project_labels`: history is not ours to erase, so
    a deleted option renders as its code rather than vanishing.
    """
    for option in options:
        if option.code == code:
            return option.label
    return code


def _render_date(value: Any, zone: dt.tzinfo) -> str | None:
    """A stored date/datetime as a speakable string in the workspace's zone."""
    moment: dt.datetime | None = None
    if isinstance(value, dt.datetime):
        moment = value
    elif isinstance(value, dt.date):
        return value.strftime("%d %B %Y")
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            moment = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            # Not an ISO string. Say it as it was stored rather than dropping a
            # value somebody deliberately entered.
            return text
    elif isinstance(value, int | float):
        # A bare epoch is exactly the "reading 1755129600 aloud" defect §4 names.
        try:
            moment = dt.datetime.fromtimestamp(float(value), tz=dt.UTC)
        except (OverflowError, OSError, ValueError):  # pragma: no cover - defensive
            return None
    if moment is None:  # pragma: no cover - defensive
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=dt.UTC)
    local = moment.astimezone(zone)
    if local.hour or local.minute:
        return local.strftime("%d %B %Y at %H:%M")
    return local.strftime("%d %B %Y")


def _render_money(value: Any, currency: str | None) -> str | None:
    """Amount plus the workspace's currency code (architecture rule 11)."""
    if isinstance(value, Mapping):
        amount = value.get("amount")
        code = str(value.get("currency") or currency or "").strip()
    else:
        amount = value
        code = str(currency or "").strip()
    if amount in (None, ""):
        return None
    try:
        number = float(amount)
    except (TypeError, ValueError):
        return f"{amount} {code}".strip()
    rendered = f"{number:,.2f}".rstrip("0").rstrip(".")
    return f"{rendered} {code}".strip()


def render_one(
    value: Any,
    field: LeadField,
    *,
    options: Sequence[FieldOption] = (),
    zone: dt.tzinfo = dt.UTC,
    currency: str | None = None,
) -> str | None:
    """One stored value as a speakable string, or `None` to drop the key."""
    if value is None or value == "" or value == [] or value == {}:
        return None

    kind = field.field_type

    if kind in (LeadFieldType.DATE, LeadFieldType.RECURRING_DATE):
        if isinstance(value, Mapping):
            # RECURRING_DATE is composite; the date part is what is speakable.
            inner = value.get("date") or value.get("value") or value.get("next")
            return _render_date(inner, zone) if inner is not None else None
        return _render_date(value, zone)

    if kind is LeadFieldType.MONEY:
        return _render_money(value, currency)

    if kind is LeadFieldType.CHECKBOX:
        return "yes" if value else "no"

    if kind is LeadFieldType.DROPDOWN:
        return _label_for(str(value), options)

    if kind is LeadFieldType.DEPENDENT_DROPDOWN:
        if isinstance(value, Mapping):
            code = value.get("value")
            return _label_for(str(code), options) if code else None
        return _label_for(str(value), options)

    if kind is LeadFieldType.TAGS:
        if isinstance(value, list):
            labels = [_label_for(str(item), options) for item in value if item not in (None, "")]
            return ", ".join(labels) or None
        return str(value)

    if kind is LeadFieldType.LOCATION:
        if isinstance(value, Mapping):
            parts = [
                str(value[part]).strip()
                for part in ("line1", "line2", "city", "state", "country", "pincode")
                if value.get(part) not in (None, "")
            ]
            return ", ".join(parts) or None
        return str(value)

    if kind is LeadFieldType.NUMBER:
        if isinstance(value, float) and value.is_integer():
            return str(int(value))
        return str(value)

    text = str(value).strip()
    return text or None


def render_for_voice(
    values: Mapping[str, Any],
    fields: Iterable[LeadField],
    *,
    timezone_name: str | None = None,
    currency: str | None = None,
    options_by_field: Mapping[Any, Sequence[FieldOption]] | None = None,
) -> dict[str, str]:
    """Render an already-projected `values` map for speech.

    `values` must be the output of `FieldProjectionService` — this function does
    no permission work of its own and must never be handed a raw `lead.values`.

    A key whose field definition is unknown is still rendered, as text, rather
    than dropped: an admin renaming or archiving a field must not silently empty
    a live agent's prompt.
    """
    zone = _zone(timezone_name)
    by_key = {field.key: field for field in fields}
    options = options_by_field or {}

    rendered: dict[str, str] = {}
    for key, value in values.items():
        field = by_key.get(key)
        if field is None:
            if value in (None, "", [], {}):
                continue
            rendered[key] = str(value)
            continue
        text = render_one(
            value,
            field,
            options=options.get(field.id, ()),
            zone=zone,
            currency=currency,
        )
        if text is not None:
            rendered[key] = text
    return rendered
