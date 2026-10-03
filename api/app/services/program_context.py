"""Pre-call programme context — the KB's facts, passed through, never stored.

The CRM is not the programme-context store. It holds the lead's own submitted
values; the authoritative description of a programme lives in the LevelUp
Learning knowledge base. This module fetches a compact, customer-facing summary
of the programme a lead enquired about and hands it to the voice agent as one
`user_data` variable, so Aisha can answer "how long is it?" without a retrieval
round-trip. Nothing is written to the database and no programme fact is stored
in this codebase.

Two rules shape all of it:

**No lead field is named in code either.** Which field holds the programme is
not knowable in advance — a workspace may call it Course, Programme, or
Interested In, and the product's H2 headline slot defaults to Phone, so it
cannot be relied on. Instead every submitted value is offered to the knowledge
base, and the one that *is* a programme identifies itself by matching a
published title or slug exactly. One match wins; none or several send nothing.

**No course is named in code.** The course list, their titles and their slugs
are read out of the knowledge base's own search index at run time
(`/search/search_index.json`, which MkDocs publishes). A sixth programme starts
working when it is published, not when this file is edited. No branch in this
module compares a course to a literal, and a test reads this file to prove no
course name or figure appears in it.

**Nothing is invented.** Text is lifted verbatim from the KB and trimmed. If
the lead's course matches no programme, or the KB cannot be reached, no key is
sent at all — the call then behaves exactly as it did before this existed. A
missing variable is honest; a guessed one is not.
"""

from __future__ import annotations

import html
import logging
import re
import time
from typing import Any

__all__ = [
    "MAX_CONTEXT_CHARS",
    "SECTION_PRIORITY",
    "ProgramContextService",
    "clear_index_cache",
]

logger = logging.getLogger(__name__)

#: Which knowledge-base sections feed the pre-call context, in priority order,
#: with a character allowance each.
#:
#: These are *structural* section names — the same eight files exist for every
#: programme — not facts about any programme.
#:
#: Per-section allowances rather than one shared budget: with a single budget
#: the identity section consumed all of it and pricing, which callers ask about
#: most, never reached the agent.
#:
#: `03-persona-positioning` is deliberately absent. It is written for whoever
#: configures the agent rather than for a caller, and in the live KB it carries
#: internal authoring notes. The pre-call context carries customer-facing
#: facts only. The `answer_persona_positioning` RAG node still serves that
#: material when a caller actually asks.
SECTION_PRIORITY: tuple[tuple[str, str, int], ...] = (
    ("01-program-identity", "Identity, duration, delivery, schedule, audience", 620),
    ("07-pricing-and-cohorts", "Pricing and payment", 420),
    ("02-questions-and-answers", "Common questions", 380),
)

#: Hard ceiling on the final value, applied after labels and newlines are added.
#: Bolna documents no limit for a `user_data` value; this is deliberately modest
#: so a long programme page cannot bloat every LLM call on the conversation.
MAX_CONTEXT_CHARS = 1500

#: Where course pages live in the knowledge base.
COURSES_ROOT = "03-courses/"

#: How long a fetched index is reused. The KB changes rarely; a call should not
#: pay for a fetch it does not need.
INDEX_TTL_SECONDS = 900

REQUEST_TIMEOUT_SECONDS = 8.0

#: Lines that are guidance for whoever maintains the KB rather than something a
#: caller should ever hear.
_INTERNAL_PREFIXES = ("note:", "todo", "tbd", "internal:", "draft:")

#: Values that cannot be a programme name, skipped before matching. Exact
#: matching would reject them anyway; skipping is cheaper and says why.
_EMAILISH = re.compile(r"\S+@\S+")
_UUIDISH = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)
_HAS_LETTER = re.compile(r"[a-z]", re.I)

#: A programme title is short. Anything longer is free text, not a name.
MAX_CANDIDATE_CHARS = 120

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")
_NUMBERED = re.compile(r"^\d+-")

#: `{base_url: (fetched_at, docs)}`. Module level so the cache survives the
#: per-request services that read it.
_INDEX_CACHE: dict[str, tuple[float, list[dict[str, Any]]]] = {}


def clear_index_cache() -> None:
    """Drop the cached index. For tests and for an operator forcing a refresh."""
    _INDEX_CACHE.clear()


def _plain(text: str) -> str:
    """KB HTML to speakable plain text."""
    return _WS.sub(" ", html.unescape(_TAG.sub(" ", text or ""))).strip()


def _is_internal(text: str) -> bool:
    lowered = text.strip().lower()
    return any(lowered.startswith(prefix) for prefix in _INTERNAL_PREFIXES)


def _match_key(value: str) -> str:
    """Normalise for matching: casefold, drop everything but letters and digits.

    Makes `Seaside Photography`, `seaside_photography` and
    `01-seaside-photography` the same key, so a workspace's option code and its
    label both resolve without either being written down here.
    """
    return re.sub(r"[^a-z0-9]+", "", (value or "").casefold())


class ProgramContextService:
    """Builds `crm_program_name` / `crm_program_context` for one lead.

    Constructed per request like every other service. `base_url` is deployment
    configuration (`KB_BASE_URL`); when it is unset the service is inert and
    returns nothing, which is what a deployment without a knowledge base should
    do rather than fail a call.
    """

    def __init__(self, base_url: str | None, *, ttl_seconds: int = INDEX_TTL_SECONDS) -> None:
        self._base_url = (base_url or "").rstrip("/")
        self._ttl = ttl_seconds

    # --- the knowledge base -------------------------------------------------

    async def _load_docs(self) -> list[dict[str, Any]]:
        """One HTTP read of the KB search index. No caching, no error handling."""
        import httpx

        async with httpx.AsyncClient(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = await client.get(f"{self._base_url}/search/search_index.json")
            response.raise_for_status()
            payload = response.json()

        docs = payload.get("docs") if isinstance(payload, dict) else None
        return docs if isinstance(docs, list) else []

    async def _fetch_docs(self) -> list[dict[str, Any]]:
        """The index, cached across requests. Raises only for `for_course` to swallow."""
        cached = _INDEX_CACHE.get(self._base_url)
        if cached is not None and (time.monotonic() - cached[0]) < self._ttl:
            return cached[1]
        docs = await self._load_docs()
        _INDEX_CACHE[self._base_url] = (time.monotonic(), docs)
        return docs

    @staticmethod
    def _courses(docs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
        """`{slug: {"title": …, "pages": {section: [text, …]}}}`, from the index."""
        found: dict[str, dict[str, Any]] = {}
        for entry in docs:
            location = str((entry or {}).get("location") or "")
            if not location.startswith(COURSES_ROOT):
                continue
            remainder = location[len(COURSES_ROOT) :]
            parts = remainder.split("/")
            slug = parts[0]
            if not slug or slug.startswith("#"):
                continue

            course = found.setdefault(slug, {"title": None, "pages": {}})
            section = parts[1].split("#")[0] if len(parts) > 1 else ""
            if not section and "#" not in remainder:
                # The course landing page carries the programme's own title.
                course["title"] = course["title"] or entry.get("title")
            if section:
                text = _plain(str(entry.get("text") or ""))
                if text and not _is_internal(text):
                    course["pages"].setdefault(section, []).append(text)
        return found

    # --- the one public operation -------------------------------------------

    @staticmethod
    def _is_candidate(value: Any) -> bool:
        """Could this submitted value plausibly be a programme name?

        Phone numbers, email addresses, ids and free text are skipped. Exact
        matching would reject them regardless; this keeps the intent visible.
        """
        if not isinstance(value, str):
            return False
        text = value.strip()
        if not text or len(text) > MAX_CANDIDATE_CHARS:
            return False
        if _EMAILISH.search(text) or _UUIDISH.match(text):
            return False
        return bool(_HAS_LETTER.search(text))

    async def for_values(self, values: Any) -> dict[str, str]:
        """`{crm_program_name, crm_program_context}` for the lead, or `{}`.

        Every submitted value is compared — normalised, and only ever for an
        **exact** match against a published course title or slug. Nothing
        fuzzy: a near-miss must not put one programme's fees in front of a
        caller who asked about another.

        The programme must be unambiguous. Zero matches sends nothing; two
        *different* programmes among the values sends nothing either, because
        there is no honest way to choose. The same programme found in two
        fields is still one programme, and is used.
        """
        if not self._base_url or not isinstance(values, dict) or not values:
            return {}

        try:
            docs = await self._fetch_docs()
        except Exception as exc:
            # A knowledge base that is down must not stop the CRM placing calls.
            logger.warning("program_context: knowledge base unreachable (%s)", type(exc).__name__)
            return {}

        try:
            courses = self._courses(docs)
            lookup: dict[str, str] = {}
            for slug, course in courses.items():
                # Three exact spellings of the same programme: the published
                # slug, that slug without the ordering prefix (which is what a
                # workspace option code looks like), and the title.
                for key in (
                    _match_key(slug),
                    _match_key(_NUMBERED.sub("", slug)),
                    _match_key(str(course.get("title") or "")),
                ):
                    if key:
                        lookup[key] = slug

            matched = {
                lookup[_match_key(value)]
                for value in values.values()
                if self._is_candidate(value) and _match_key(value) in lookup
            }
            if len(matched) != 1:
                if matched:
                    logger.info(
                        "program_context: %d programmes matched this lead; sending none",
                        len(matched),
                    )
                return {}
            return self._compose(courses[matched.pop()])
        except Exception as exc:  # pragma: no cover - defensive
            logger.warning("program_context: could not build context (%s)", type(exc).__name__)
            return {}

    @staticmethod
    def _compose(course: dict[str, Any]) -> dict[str, str]:
        """Assemble the sections, then enforce the hard ceiling on the result."""
        lines: list[str] = []
        for section, label, allowance in SECTION_PRIORITY:
            body = " ".join(course["pages"].get(section, [])).strip()
            if not body:
                continue
            if len(body) > allowance:
                body = body[:allowance].rsplit(" ", 1)[0] + "…"
            lines.append(f"{label}: {body}")

        context = "\n".join(lines)
        # The ceiling is checked here, on the finished value — labels, newlines
        # and all — not on the sections in isolation. Trimming from the end
        # drops the lowest-priority material first.
        if len(context) > MAX_CONTEXT_CHARS:
            context = context[: MAX_CONTEXT_CHARS - 1].rsplit(" ", 1)[0] + "…"
        if not context:
            return {}

        return {
            "crm_program_name": str(course.get("title") or ""),
            "crm_program_context": context,
        }
