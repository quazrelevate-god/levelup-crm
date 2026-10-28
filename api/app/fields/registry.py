"""The two type registries — the only place field *kinds* are declared.

`docs/03-configuration-model.md` §1.3 lists 13 lead field types; §4.3 lists 8
action field types. Those two lists, and nothing else about a customer's data
model, are compiled into this product.

Each entry declares five things, and every consumer reads them from here:

1. **validation**   — is this value acceptable for this type and config?
2. **normalisation** — the canonical form actually persisted
3. **storage shape**  — what lands in the JSONB blob (`values -> key`)
4. **operators**      — which filters the type supports (M6's compiler reads this)
5. **renderer**       — the contract the frontend builds an input from

The renderer contract is why `GET /settings/field-types` exists: the frontend
asks the backend what types exist and how to draw them, so adding a type is a
backend change plus a renderer, never a hardcoded list in TypeScript.

Normalisation is deliberately separate from validation. `PHONE` validates that a
number is plausible and normalises it to E.164 using *the workspace's* default
country code — never a hardcoded 91 (architecture rule 12).
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import ipaddress
import re
import uuid
from collections.abc import Callable, Mapping, Sequence
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import Any, Literal

from app.fields import operators as ops
from app.fields.operators import Operator
from app.models.enums import ActionFieldType, LeadFieldType

__all__ = [
    "ACTION_FIELD_TYPES",
    "LEAD_FIELD_TYPES",
    "FieldTypeSpec",
    "FieldValueError",
    "ValidationContext",
    "action_type_spec",
    "lead_type_spec",
    "normalise_phone",
]

# The storage shapes a JSONB value can take. Declared so the registry can tell
# the frontend (and M6's compiler) how to reach into `values -> key`.
StorageShape = Literal["scalar", "list", "object"]


class FieldValueError(Exception):
    """A field value failed validation for its type.

    Carries the field key when the caller knows it, so a 422 can name the field
    rather than reporting "something in values was wrong".
    """

    def __init__(self, message: str, *, field_key: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.field_key = field_key


@dataclasses.dataclass(frozen=True, slots=True)
class ValidationContext:
    """Everything a validator needs that is not the value itself.

    Carries the *workspace's* locale settings, which is what keeps phone and
    money normalisation from hardcoding a country or currency.
    """

    default_country_code: str
    currency: str
    timezone: str
    # Option codes available for this field, when the type is choice-based.
    # `None` for types that do not use options.
    option_codes: frozenset[str] | None = None
    # For DEPENDENT_DROPDOWN: child option code -> parent option code.
    option_parents: Mapping[str, str | None] | None = None
    # Type-specific configuration from `lead_fields.config`.
    config: Mapping[str, Any] = dataclasses.field(default_factory=dict)
    # Workspace member ids, for the action-only USER type.
    membership_ids: frozenset[uuid.UUID] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class FieldTypeSpec:
    """One entry in a type registry."""

    key: str
    label: str
    description: str
    storage: StorageShape
    operators: tuple[Operator, ...]
    #: What the frontend needs to render an input. `widget` selects the
    #: component; the rest are hints that component reads.
    renderer: Mapping[str, Any]
    #: Validates and returns the canonical stored form. Raises `FieldValueError`.
    normalise: Callable[[Any, ValidationContext], Any]
    #: Whether the type draws its values from `field_options`.
    uses_options: bool = False
    #: Whether this type's values belong in `leads.search_vector`.
    #:
    #: A property of the *type*, not a per-field toggle: §1.4 of the
    #: configuration model defines exactly four field toggles and searchable is
    #: not among them, so inventing a fifth would be adding configuration
    #: surface the product does not have. Free text, addresses and option codes
    #: are what someone types into a search box; amounts, dates and checkboxes
    #: are what they filter on, and folding them into the vector would make
    #: "2024" match a date, a phone fragment and a price alike.
    searchable: bool = False
    #: JSON-schema-ish description of the type's `config` object, so the
    #: settings UI can render type-specific configuration generically.
    config_schema: Mapping[str, Any] = dataclasses.field(default_factory=dict)

    def as_payload(self) -> dict[str, Any]:
        """Serialisable form for `GET /settings/field-types`."""
        return {
            "key": self.key,
            "label": self.label,
            "description": self.description,
            "storage": self.storage,
            "uses_options": self.uses_options,
            "searchable": self.searchable,
            "operators": [op.value for op in self.operators],
            "renderer": dict(self.renderer),
            "config_schema": dict(self.config_schema),
        }


# --- shared helpers ----------------------------------------------------------


def _require_str(value: Any, *, what: str) -> str:
    if not isinstance(value, str):
        raise FieldValueError(f"{what} must be text")
    return value.strip()


def _blank_to_none(value: Any) -> Any:
    """Empty string and empty collections all mean "no value".

    Normalising them to `None` at the boundary means the rest of the system has
    exactly one representation of absence, and `is_empty` filters work whether
    the user cleared a text box or never filled it.
    """
    if value is None:
        return None
    if isinstance(value, str) and not value.strip():
        return None
    if isinstance(value, list | dict) and len(value) == 0:
        return None
    return value


# --- scalar normalisers ------------------------------------------------------

_MAX_TEXT = 10_000


def _norm_text(value: Any, ctx: ValidationContext) -> str | None:
    if (value := _blank_to_none(value)) is None:
        return None
    text = _require_str(value, what="Value")
    if len(text) > _MAX_TEXT:
        raise FieldValueError(f"Text is longer than {_MAX_TEXT} characters")
    return text


# Deliberately permissive: this rejects what is definitely not an address, not
# what is not deliverable. RFC 5322 in a regex is a well-known dead end, and a
# CRM that refuses a real customer's address is worse than one that stores a
# typo.
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


def _norm_email(value: Any, ctx: ValidationContext) -> str | None:
    if (value := _blank_to_none(value)) is None:
        return None
    text = _require_str(value, what="Email").lower()
    if not _EMAIL_RE.match(text):
        raise FieldValueError(f"{text!r} is not a valid email address")
    return text


_PHONE_STRIP_RE = re.compile(r"[\s\-().]")
_PHONE_DIGITS_RE = re.compile(r"^\+?\d{4,20}$")


def _norm_phone(value: Any, ctx: ValidationContext) -> str | None:
    """Normalise to `+<country><subscriber>` using the *workspace's* code.

    Architecture rule 12: never hardcode 91. A workspace configured for `1`
    turns `555 0123` into `+15550123`; the same input in an Indian workspace
    becomes `+915550123`.
    """
    if (value := _blank_to_none(value)) is None:
        return None
    raw = _PHONE_STRIP_RE.sub("", _require_str(value, what="Phone number"))
    if not raw:
        return None
    if not _PHONE_DIGITS_RE.match(raw):
        raise FieldValueError(f"{value!r} is not a valid phone number")

    if raw.startswith("+"):
        return raw
    # A leading 0 is a national trunk prefix in most numbering plans; it is
    # dropped when the country code is prepended.
    national = raw.lstrip("0") or raw
    return f"+{ctx.default_country_code}{national}"


def normalise_phone(value: Any, ctx: ValidationContext) -> str | None:
    """The PHONE field rule, for callers outside field validation.

    A test call's number is typed by a person, not stored in a field, but it
    must be normalised the same way (rule 12: the workspace's country code).
    """
    return _norm_phone(value, ctx)


def _norm_checkbox(value: Any, ctx: ValidationContext) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"true", "yes", "1", "y"}:
            return True
        if lowered in {"false", "no", "0", "n"}:
            return False
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    raise FieldValueError(f"{value!r} is not a yes/no value")


def _norm_number(value: Any, ctx: ValidationContext) -> float | int | None:
    if (value := _blank_to_none(value)) is None:
        return None
    if isinstance(value, bool):
        raise FieldValueError("A yes/no value is not a number")
    if isinstance(value, int | float):
        number: float | int = value
    else:
        try:
            text = _require_str(value, what="Number")
            number = float(text) if ("." in text or "e" in text.lower()) else int(text)
        except (TypeError, ValueError) as exc:
            raise FieldValueError(f"{value!r} is not a number") from exc

    config = ctx.config
    minimum, maximum = config.get("min"), config.get("max")
    if minimum is not None and number < minimum:
        raise FieldValueError(f"Must be at least {minimum}")
    if maximum is not None and number > maximum:
        raise FieldValueError(f"Must be at most {maximum}")
    return number


def _norm_money(value: Any, ctx: ValidationContext) -> dict[str, Any] | None:
    """Money is `{amount, currency}` — never a bare float.

    Architecture rule 11. The currency defaults to the workspace's, so a US
    workspace stores USD without a code change, and a value that already names
    a currency keeps it (an import may carry mixed currencies).
    """
    if (value := _blank_to_none(value)) is None:
        return None

    if isinstance(value, dict):
        raw_amount = value.get("amount")
        currency = value.get("currency") or ctx.currency
    else:
        raw_amount, currency = value, ctx.currency

    if raw_amount is None or raw_amount == "":
        return None
    try:
        amount = Decimal(str(raw_amount).replace(",", ""))
    except (InvalidOperation, TypeError) as exc:
        raise FieldValueError(f"{raw_amount!r} is not a monetary amount") from exc

    if not isinstance(currency, str) or len(currency) != 3:
        raise FieldValueError(f"{currency!r} is not a 3-letter ISO 4217 currency code")

    # numeric(12,2) in the data model. ROUND_HALF_UP explicitly, because
    # Decimal defaults to banker's rounding and would turn 1.005 into 1.00 —
    # Postgres rounds a numeric(12,2) cast half-up, and the stored value should
    # equal what the column itself would have held.
    quantised = amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if abs(quantised) >= Decimal("10000000000"):
        raise FieldValueError("Amount is too large for numeric(12,2)")
    # Stored as a string: JSON floats cannot represent decimal money exactly.
    return {"amount": str(quantised), "currency": currency.upper()}


def _norm_date(value: Any, ctx: ValidationContext) -> str | None:
    """ISO `YYYY-MM-DD`. A calendar date has no timezone — a birthday is the
    same date everywhere, so storing it as an instant would be wrong."""
    if (value := _blank_to_none(value)) is None:
        return None
    if isinstance(value, dt.datetime):
        return value.date().isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    text = _require_str(value, what="Date")
    try:
        return dt.date.fromisoformat(text[:10]).isoformat()
    except ValueError as exc:
        raise FieldValueError(f"{text!r} is not a date in YYYY-MM-DD form") from exc


_URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)


def _norm_website(value: Any, ctx: ValidationContext) -> str | None:
    if (value := _blank_to_none(value)) is None:
        return None
    text = _require_str(value, what="Website")
    # Customers type "acme.com"; storing it unusable as a link helps no one.
    if not text.lower().startswith(("http://", "https://")):
        text = f"https://{text}"
    if not _URL_RE.match(text):
        raise FieldValueError(f"{value!r} is not a valid website address")
    return text


def _norm_dropdown(value: Any, ctx: ValidationContext) -> str | None:
    if (value := _blank_to_none(value)) is None:
        return None
    code = _require_str(value, what="Option")
    if ctx.option_codes is not None and code not in ctx.option_codes:
        raise FieldValueError(f"{code!r} is not an option on this field")
    return code


def _norm_tags(value: Any, ctx: ValidationContext) -> list[str] | None:
    if (value := _blank_to_none(value)) is None:
        return None
    raw = value if isinstance(value, list) else [value]
    codes: list[str] = []
    for item in raw:
        code = _require_str(item, what="Option")
        if not code:
            continue
        if ctx.option_codes is not None and code not in ctx.option_codes:
            raise FieldValueError(f"{code!r} is not an option on this field")
        if code not in codes:  # order-preserving dedupe
            codes.append(code)
    return codes or None


# --- composite normalisers ---------------------------------------------------


def _norm_dependent_dropdown(value: Any, ctx: ValidationContext) -> dict[str, Any] | None:
    """`{"parent": <code|null>, "value": <code>}`.

    Both levels are stored even though the parent is derivable from the option
    tree. Two reasons: filtering on "any lead in Tamil Nadu" must not require a
    recursive join at query time, and if an option is later re-parented the
    historical value still records what the user actually chose.

    The parent is *verified* against the tree rather than trusted from the
    client — a payload naming a mismatched pair is rejected, not silently
    corrected.
    """
    if (value := _blank_to_none(value)) is None:
        return None

    if isinstance(value, dict):
        selected = value.get("value")
        claimed_parent = value.get("parent")
    else:
        selected, claimed_parent = value, None

    if (selected := _blank_to_none(selected)) is None:
        return None
    code = _require_str(selected, what="Option")

    if ctx.option_codes is not None and code not in ctx.option_codes:
        raise FieldValueError(f"{code!r} is not an option on this field")

    actual_parent = (ctx.option_parents or {}).get(code)
    if (
        claimed_parent is not None
        and actual_parent is not None
        and _require_str(claimed_parent, what="Parent option") != actual_parent
    ):
        raise FieldValueError(f"{code!r} does not belong to parent {claimed_parent!r}")
    return {"parent": actual_parent, "value": code}


_RECURRENCE_FREQUENCIES = ("YEARLY", "MONTHLY", "WEEKLY", "DAILY")


def _next_occurrence(start: dt.date, frequency: str, interval: int, today: dt.date) -> dt.date:
    """First occurrence on or after `today`.

    Computed at write time and stored, so "whose renewal falls in the next 30
    days" is an indexable range scan on a date rather than a recurrence
    expansion across every lead in the workspace.
    """
    if start >= today:
        return start
    if frequency == "DAILY":
        elapsed = (today - start).days
        steps = -(-elapsed // interval)  # ceil
        return start + dt.timedelta(days=steps * interval)
    if frequency == "WEEKLY":
        elapsed_weeks = (today - start).days / 7
        steps = -(-int(elapsed_weeks * 1000) // (interval * 1000))
        candidate = start + dt.timedelta(weeks=steps * interval)
        while candidate < today:
            candidate += dt.timedelta(weeks=interval)
        return candidate
    if frequency == "MONTHLY":
        months = (today.year - start.year) * 12 + (today.month - start.month)
        steps = months // interval
        candidate = _add_months(start, steps * interval)
        while candidate < today:
            steps += 1
            candidate = _add_months(start, steps * interval)
        return candidate
    # YEARLY
    steps = (today.year - start.year) // interval
    candidate = _add_months(start, steps * interval * 12)
    while candidate < today:
        steps += 1
        candidate = _add_months(start, steps * interval * 12)
    return candidate


def _add_months(date: dt.date, months: int) -> dt.date:
    """Add months, clamping the day to the target month's length.

    31 Jan + 1 month is 28/29 Feb. Without the clamp this raises, which would
    make a monthly recurrence starting on the 31st unusable.
    """
    total = date.month - 1 + months
    year = date.year + total // 12
    month = total % 12 + 1
    next_month_start = dt.date(year + 1, 1, 1) if month == 12 else dt.date(year, month + 1, 1)
    last_day = (next_month_start - dt.timedelta(days=1)).day
    return dt.date(year, month, min(date.day, last_day))


def _norm_recurring_date(value: Any, ctx: ValidationContext) -> dict[str, Any] | None:
    """`{"start": ISO, "frequency": ..., "interval": n, "next": ISO}`.

    `next` is derived, not user-supplied. It exists so the occurrences view and
    M8's greeting scheduler can range-scan an indexable column instead of
    expanding a rule per lead.

    Because `next` is relative to "today", it goes stale. The occurrences
    endpoint recomputes on read rather than trusting it blindly, and a nightly
    job (M8) refreshes the stored value.
    """
    if (value := _blank_to_none(value)) is None:
        return None

    if not isinstance(value, dict):
        # A bare date is a one-off: valid, and the common case on import.
        start_iso = _norm_date(value, ctx)
        if start_iso is None:
            return None
        return {"start": start_iso, "frequency": "YEARLY", "interval": 1, "next": start_iso}

    start_iso = _norm_date(value.get("start"), ctx)
    if start_iso is None:
        raise FieldValueError("A recurring date needs a start date")

    frequency = str(value.get("frequency") or "YEARLY").upper()
    if frequency not in _RECURRENCE_FREQUENCIES:
        raise FieldValueError(
            f"{frequency!r} is not a recurrence frequency ({', '.join(_RECURRENCE_FREQUENCIES)})"
        )

    try:
        interval = int(value.get("interval") or 1)
    except (TypeError, ValueError) as exc:
        raise FieldValueError("Recurrence interval must be a whole number") from exc
    if not 1 <= interval <= 365:
        raise FieldValueError("Recurrence interval must be between 1 and 365")

    start = dt.date.fromisoformat(start_iso)
    following = _next_occurrence(start, frequency, interval, dt.date.today())
    return {
        "start": start_iso,
        "frequency": frequency,
        "interval": interval,
        "next": following.isoformat(),
    }


_LOCATION_TEXT_KEYS = ("line1", "line2", "city", "state", "postal_code", "country")


def _norm_location(value: Any, ctx: ValidationContext) -> dict[str, Any] | None:
    """Structured address plus optional coordinates — never a scalar string.

    §1.3 calls this out explicitly. Storing "Chennai, TN" as text makes "all
    leads in Tamil Nadu" a substring search that also matches a street called
    Tamil Nadu Road.
    """
    if (value := _blank_to_none(value)) is None:
        return None

    if isinstance(value, str):
        # Imports and the intake API send a single line; keep it rather than
        # rejecting, in the field that means "unstructured street address".
        return {"line1": value.strip()} if value.strip() else None

    if not isinstance(value, dict):
        raise FieldValueError("A location must be an address object")

    address: dict[str, Any] = {}
    for key in _LOCATION_TEXT_KEYS:
        raw = value.get(key)
        if raw is None:
            continue
        text = _require_str(raw, what=key.replace("_", " ").title())
        if text:
            address[key] = text

    lat, lng = value.get("lat"), value.get("lng")
    if (lat is None) != (lng is None):
        raise FieldValueError("Latitude and longitude must be given together")
    if lat is not None and lng is not None:
        try:
            lat_f, lng_f = float(lat), float(lng)
        except (TypeError, ValueError) as exc:
            raise FieldValueError("Coordinates must be numbers") from exc
        if not -90 <= lat_f <= 90:
            raise FieldValueError("Latitude must be between -90 and 90")
        if not -180 <= lng_f <= 180:
            raise FieldValueError("Longitude must be between -180 and 180")
        address["lat"], address["lng"] = lat_f, lng_f

    return address or None


# --- action-only normalisers -------------------------------------------------


def _norm_user(value: Any, ctx: ValidationContext) -> str | None:
    """A membership id, verified to be in *this* workspace.

    The scoped session would refuse a foreign membership on write, but catching
    it here produces a 422 naming the field rather than an opaque failure.
    """
    if (value := _blank_to_none(value)) is None:
        return None
    try:
        membership_id = uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise FieldValueError(f"{value!r} is not a member reference") from exc
    if ctx.membership_ids is not None and membership_id not in ctx.membership_ids:
        raise FieldValueError("That member is not part of this workspace")
    return str(membership_id)


def _norm_file(value: Any, ctx: ValidationContext) -> dict[str, Any] | None:
    """`{storage_key, filename, content_type, size_bytes}`.

    The upload itself is a separate multipart call (M5); what lands in the
    action payload is the reference the upload returned.
    """
    if (value := _blank_to_none(value)) is None:
        return None
    if not isinstance(value, dict):
        raise FieldValueError("A file value must be an uploaded-file reference")
    storage_key = value.get("storage_key")
    if not storage_key:
        raise FieldValueError("File reference is missing its storage key")
    return {
        "storage_key": _require_str(storage_key, what="Storage key"),
        "filename": _require_str(value.get("filename") or "", what="Filename") or None,
        "content_type": value.get("content_type"),
        "size_bytes": value.get("size_bytes"),
    }


def _norm_media_link(value: Any, ctx: ValidationContext) -> str | None:
    """A URL to an audio/video asset.

    Same shape as WEBSITE but semantically distinct: the frontend renders a
    player rather than a link, and a private-network address is refused because
    this URL gets fetched by a player in the customer's browser.
    """
    url = _norm_website(value, ctx)
    if url is None:
        return None
    host = re.sub(r"^https?://", "", url).split("/")[0].split(":")[0]
    try:
        if ipaddress.ip_address(host).is_private:
            raise FieldValueError("A media link cannot point at a private address")
    except ValueError:
        pass  # a hostname, not a literal IP — nothing to check here
    return url


# --- the registries ----------------------------------------------------------


def _spec(
    key: str,
    label: str,
    description: str,
    storage: StorageShape,
    operators: tuple[Operator, ...],
    renderer: Mapping[str, Any],
    normalise: Callable[[Any, ValidationContext], Any],
    *,
    uses_options: bool = False,
    searchable: bool = False,
    config_schema: Mapping[str, Any] | None = None,
) -> FieldTypeSpec:
    return FieldTypeSpec(
        key=key,
        label=label,
        description=description,
        storage=storage,
        operators=operators,
        renderer=renderer,
        normalise=normalise,
        uses_options=uses_options,
        searchable=searchable,
        config_schema=config_schema or {},
    )


LEAD_FIELD_TYPES: dict[LeadFieldType, FieldTypeSpec] = {
    LeadFieldType.TEXT: _spec(
        "TEXT",
        "Text",
        "Names, addresses, free text",
        "scalar",
        ops.TEXTUAL,
        {"widget": "text", "multiline": False},
        _norm_text,
        searchable=True,
        config_schema={"multiline": {"type": "boolean", "default": False}},
    ),
    LeadFieldType.DROPDOWN: _spec(
        "DROPDOWN",
        "Dropdown",
        "One of a predefined list",
        "scalar",
        ops.CHOICE,
        {"widget": "select", "multiple": False},
        _norm_dropdown,
        uses_options=True,
        searchable=True,
    ),
    LeadFieldType.TAGS: _spec(
        "TAGS",
        "Tags",
        "Many of a predefined list",
        "list",
        ops.SET,
        {"widget": "select", "multiple": True},
        _norm_tags,
        uses_options=True,
        searchable=True,
    ),
    LeadFieldType.EMAIL: _spec(
        "EMAIL",
        "Email",
        "Email addresses",
        "scalar",
        ops.TEXTUAL,
        {"widget": "email", "inputMode": "email"},
        _norm_email,
        searchable=True,
    ),
    LeadFieldType.PHONE: _spec(
        "PHONE",
        "Phone",
        "Contact numbers",
        "scalar",
        ops.TEXTUAL,
        {"widget": "phone", "inputMode": "tel", "normalisedToE164": True},
        _norm_phone,
        searchable=True,
    ),
    LeadFieldType.CHECKBOX: _spec(
        "CHECKBOX",
        "Checkbox",
        "Yes/no, true/false",
        "scalar",
        ops.BOOLEAN,
        {"widget": "checkbox"},
        _norm_checkbox,
    ),
    LeadFieldType.DATE: _spec(
        "DATE",
        "Date",
        "Calendar dates",
        "scalar",
        ops.TEMPORAL,
        {"widget": "date"},
        _norm_date,
    ),
    LeadFieldType.MONEY: _spec(
        "MONEY",
        "Money",
        "Currency amounts",
        "object",
        ops.NUMERIC,
        {"widget": "money", "amountKey": "amount", "currencyKey": "currency"},
        _norm_money,
    ),
    LeadFieldType.NUMBER: _spec(
        "NUMBER",
        "Number",
        "Numeric values",
        "scalar",
        ops.NUMERIC,
        {"widget": "number"},
        _norm_number,
        config_schema={
            "min": {"type": "number", "required": False},
            "max": {"type": "number", "required": False},
        },
    ),
    LeadFieldType.WEBSITE: _spec(
        "WEBSITE",
        "Website",
        "URLs",
        "scalar",
        ops.TEXTUAL,
        {"widget": "url", "inputMode": "url"},
        _norm_website,
        searchable=True,
    ),
    LeadFieldType.DEPENDENT_DROPDOWN: _spec(
        "DEPENDENT_DROPDOWN",
        "Dependent dropdown",
        "Cascading — Country to State, Category to Subcategory",
        "object",
        ops.CHOICE,
        {
            "widget": "cascader",
            "valueKey": "value",
            "parentKey": "parent",
            # The frontend fetches the option tree from the field's options
            # endpoint; `parent_option_id` gives it the edges.
            "optionsAreTree": True,
        },
        _norm_dependent_dropdown,
        uses_options=True,
        searchable=True,
        config_schema={
            "parent_field_id": {
                "type": "uuid",
                "required": False,
                "description": (
                    "Optional sibling field whose selection filters this one. "
                    "When absent the cascade is internal to this field's own "
                    "option tree."
                ),
            }
        },
    ),
    LeadFieldType.RECURRING_DATE: _spec(
        "RECURRING_DATE",
        "Recurring date",
        "Repeating events — birthdays, renewals",
        "object",
        ops.TEMPORAL,
        {
            "widget": "recurring-date",
            "startKey": "start",
            "frequencyKey": "frequency",
            "intervalKey": "interval",
            "derivedKey": "next",
            "frequencies": list(_RECURRENCE_FREQUENCIES),
        },
        _norm_recurring_date,
    ),
    LeadFieldType.LOCATION: _spec(
        "LOCATION",
        "Location",
        "City/state/landmark or GPS coordinates",
        "object",
        ops.TEXTUAL,
        {
            "widget": "location",
            "textKeys": list(_LOCATION_TEXT_KEYS),
            "latKey": "lat",
            "lngKey": "lng",
        },
        _norm_location,
        searchable=True,
    ),
}

ACTION_FIELD_TYPES: dict[ActionFieldType, FieldTypeSpec] = {
    ActionFieldType.TEXT: LEAD_FIELD_TYPES[LeadFieldType.TEXT],
    ActionFieldType.NUMBER: LEAD_FIELD_TYPES[LeadFieldType.NUMBER],
    ActionFieldType.DATE: LEAD_FIELD_TYPES[LeadFieldType.DATE],
    ActionFieldType.DROPDOWN: LEAD_FIELD_TYPES[LeadFieldType.DROPDOWN],
    ActionFieldType.TAGS: LEAD_FIELD_TYPES[LeadFieldType.TAGS],
    ActionFieldType.USER: _spec(
        "USER",
        "User",
        "A picker over workspace members",
        "scalar",
        ops.CHOICE,
        {"widget": "member-select"},
        _norm_user,
    ),
    ActionFieldType.FILE: _spec(
        "FILE",
        "File",
        "An upload stored in object storage",
        "object",
        ops.PRESENCE,
        {"widget": "file", "uploadEndpoint": "/actions/{action_id}/attachments"},
        _norm_file,
    ),
    ActionFieldType.MEDIA_LINK: _spec(
        "MEDIA_LINK",
        "Media link",
        "A link to an audio or video asset",
        "scalar",
        ops.TEXTUAL,
        {"widget": "media-link"},
        _norm_media_link,
    ),
}


def lead_type_spec(field_type: LeadFieldType) -> FieldTypeSpec:
    return LEAD_FIELD_TYPES[field_type]


def action_type_spec(field_type: ActionFieldType) -> FieldTypeSpec:
    return ACTION_FIELD_TYPES[field_type]


def lead_type_payloads() -> list[dict[str, Any]]:
    """`GET /settings/field-types` body, in declaration order."""
    return [LEAD_FIELD_TYPES[t].as_payload() for t in LeadFieldType]


def action_type_payloads() -> list[dict[str, Any]]:
    """`GET /settings/action-field-types` body, in declaration order."""
    return [ACTION_FIELD_TYPES[t].as_payload() for t in ActionFieldType]


def supported_operators(field_type: LeadFieldType) -> Sequence[Operator]:
    return LEAD_FIELD_TYPES[field_type].operators
