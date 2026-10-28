"""When AI calls may be placed (the `voice_calling_hours` setting).

Kept apart from `voice_calls` so the settings validator can import it without
pulling in the whole call integration.

The window is read in the **workspace's** timezone, never the server's
(architecture rule 10): 21:00 in Kolkata is 15:30 UTC, and a server in another
region must not let a call through because its own clock says it is still
afternoon.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

__all__ = ["CallingHours", "parse_calling_hours", "within_calling_hours"]

CallingHours = tuple[dt.time, dt.time]


def parse_calling_hours(spec: str | None) -> CallingHours | None:
    """`"09:00-21:00"` -> `(09:00, 21:00)`; empty -> `None` (no restriction)."""
    if spec is None or not spec.strip():
        return None
    try:
        start_text, end_text = (part.strip() for part in spec.split("-"))
        start = dt.time.fromisoformat(start_text)
        end = dt.time.fromisoformat(end_text)
    except ValueError as exc:
        raise ValueError(f"calling hours must look like 09:00-21:00, got {spec!r}") from exc
    if start >= end:
        raise ValueError(f"calling hours must start before they end, got {spec!r}")
    return start, end


def within_calling_hours(window: CallingHours, *, timezone: str, now: dt.datetime) -> bool:
    """Whether `now` falls in `[start, end)` in `timezone`'s local time."""
    local = now.astimezone(ZoneInfo(timezone)).time()
    start, end = window
    return start <= local < end
