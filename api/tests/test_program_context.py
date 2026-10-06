"""Pre-call programme context: the KB's facts, passed through, never stored.

`ProgramContextService` reads the knowledge base's own search index and hands
the voice agent a compact summary of the programme a lead enquired about, as
`crm_program_context`. These tests pin the two properties the design rests on:

- **nothing is hardcoded** — the courses, their titles and their slugs come out
  of the index at run time, and `test_the_source_names_no_course_and_no_fact`
  reads this module's own source to prove no programme name or figure is
  written into the product;
- **nothing is invented** — an unknown course, an empty course or an
  unreachable knowledge base each send no key at all, and the call proceeds
  exactly as it did before the feature existed.

The fixture index below is deliberately *not* LevelUp's: fictional programmes
prove the matching is structural. Nothing here reaches the network.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.services import program_context as pc
from app.services.program_context import (
    MAX_CONTEXT_CHARS,
    ProgramContextService,
    clear_index_cache,
)

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"
KB = "https://kb.invalid"


def _page(location: str, title: str, text: str) -> dict[str, str]:
    return {"location": location, "title": title, "text": text}


def _index() -> list[dict[str, str]]:
    """Two fictional programmes in the KB's real shape."""
    docs: list[dict[str, str]] = []
    for slug, title in (
        ("01-tide-pool-diving", "Tide Pool Diving"),
        ("02-kite-repair", "Kite Repair"),
    ):
        docs.append(_page(f"03-courses/{slug}/", title, f"<p>The {title} knowledge base.</p>"))
        docs.append(
            _page(
                f"03-courses/{slug}/01-program-identity/#x",
                "Program Identity",
                f"<p>{title} runs for 9 weeks, live online, on weekends.</p>",
            )
        )
        docs.append(
            _page(
                f"03-courses/{slug}/07-pricing-and-cohorts/#x",
                "Pricing",
                f"<p>Standard Fee for {title}: 11,000. Application Fee: 100.</p>",
            )
        )
        docs.append(
            _page(
                f"03-courses/{slug}/02-questions-and-answers/#x",
                "Q&A",
                f"<p>Is {title} live? Yes, with recordings.</p>",
            )
        )
        docs.append(
            _page(
                f"03-courses/{slug}/03-persona-positioning/#x",
                "Persona",
                "<p>Note: internal authoring note, never for a caller.</p>",
            )
        )
    return docs


@pytest.fixture(autouse=True)
def _clean_cache() -> Any:
    clear_index_cache()
    yield
    clear_index_cache()


@pytest.fixture
def service(monkeypatch: pytest.MonkeyPatch) -> ProgramContextService:
    """A service whose knowledge base is the fixture index, not the network."""

    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        _docs.calls += 1  # type: ignore[attr-defined]
        return _index()

    _docs.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    built = ProgramContextService(KB)
    built.fetch_calls = _docs  # type: ignore[attr-defined]
    return built


# --- the lead's values identify the programme ----------------------------------


async def test_a_course_label_in_the_course_field(service: ProgramContextService) -> None:
    out = await service.for_values({"name": "Ada", "course": "Tide Pool Diving"})
    assert out["crm_program_name"] == "Tide Pool Diving"
    assert "9 weeks" in out["crm_program_context"]
    assert "Kite Repair" not in out["crm_program_context"]


async def test_a_course_slug_in_the_course_field(service: ProgramContextService) -> None:
    by_slug = await service.for_values({"course": "tide_pool_diving"})
    by_label = await service.for_values({"course": "Tide Pool Diving"})
    assert by_slug == by_label != {}


async def test_a_course_label_in_an_arbitrary_field(service: ProgramContextService) -> None:
    """The field may be called anything; only the value matters."""
    out = await service.for_values({"what_are_you_looking_for": "Kite Repair"})
    assert out["crm_program_name"] == "Kite Repair"


async def test_a_course_slug_in_an_arbitrary_field(service: ProgramContextService) -> None:
    out = await service.for_values({"programme_of_interest": "02-kite-repair"})
    assert out["crm_program_name"] == "Kite Repair"


async def test_a_different_lead_gets_a_different_context(service: ProgramContextService) -> None:
    first = await service.for_values({"course": "Tide Pool Diving"})
    second = await service.for_values({"course": "Kite Repair"})
    assert first["crm_program_context"] != second["crm_program_context"]
    assert "Kite Repair" in second["crm_program_context"]
    assert "Kite Repair" not in first["crm_program_context"]


async def test_phone_email_and_ids_never_match(service: ProgramContextService) -> None:
    """A lead with no programme value must not match on its contact details."""
    out = await service.for_values(
        {
            "name": "Ada Lovelace",
            "phone": "+919087822357",
            "email": "ada@example.com",
            "lead_ref": "09ea3364-6cc7-4e43-8417-b41d823235b7",
            "notes": "Called twice, no answer.",
        }
    )
    assert out == {}


async def test_unknown_values_produce_no_context(service: ProgramContextService) -> None:
    assert await service.for_values({"course": "Underwater Basket Weaving"}) == {}


async def test_empty_values_produce_no_context(service: ProgramContextService) -> None:
    for values in ({}, {"course": ""}, {"course": "   "}, {"course": None}, {"course": 42}, None):
        assert await service.for_values(values) == {}


async def test_a_near_miss_is_not_a_match(service: ProgramContextService) -> None:
    """Exact normalised match only: a related phrase must not select a programme."""
    for nearly in ("Tide Pool", "Advanced Tide Pool Diving", "diving", "Kite"):
        assert await service.for_values({"course": nearly}) == {}


async def test_two_different_programmes_are_ambiguous(service: ProgramContextService) -> None:
    """No honest way to choose, so nothing is sent."""
    out = await service.for_values({"course": "Tide Pool Diving", "second_choice": "Kite Repair"})
    assert out == {}


async def test_the_same_programme_twice_is_not_ambiguous(service: ProgramContextService) -> None:
    """One programme named in two fields is still one programme."""
    out = await service.for_values(
        {"course": "Tide Pool Diving", "enquiry_about": "tide_pool_diving"}
    )
    assert out["crm_program_name"] == "Tide Pool Diving"


async def test_an_unreachable_knowledge_base_sends_no_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The KB being down must never stop the CRM placing a call."""

    async def _boom(self: ProgramContextService) -> list[dict[str, str]]:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(ProgramContextService, "_load_docs", _boom)
    assert await ProgramContextService(KB).for_values({"course": "Tide Pool Diving"}) == {}
    # And a deployment with no KB configured at all is simply inert.
    assert await ProgramContextService(None).for_values({"course": "Tide Pool Diving"}) == {}


# --- the shape of what is sent --------------------------------------------------


async def test_the_final_value_never_exceeds_the_hard_ceiling(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling is checked on the finished string, labels and newlines included."""
    huge = "word " * 4000

    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        return [
            _page("03-courses/01-big/", "Big", "<p>x</p>"),
            _page("03-courses/01-big/01-program-identity/#x", "Identity", f"<p>{huge}</p>"),
            _page("03-courses/01-big/07-pricing-and-cohorts/#x", "Pricing", f"<p>{huge}</p>"),
            _page("03-courses/01-big/02-questions-and-answers/#x", "Q&A", f"<p>{huge}</p>"),
        ]

    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    out = await ProgramContextService(KB).for_values({"course": "Big"})
    assert 0 < len(out["crm_program_context"]) <= MAX_CONTEXT_CHARS


async def test_pricing_survives_the_budget(service: ProgramContextService) -> None:
    """Pricing is what callers ask about; it must not be squeezed out."""
    context = (await service.for_values({"course": "Tide Pool Diving"}))["crm_program_context"]
    assert "Pricing and payment:" in context
    assert "11,000" in context
    assert "Application Fee" in context


async def test_markup_is_stripped_and_internal_notes_are_left_behind(
    service: ProgramContextService,
) -> None:
    context = (await service.for_values({"course": "Tide Pool Diving"}))["crm_program_context"]
    assert "<p>" not in context and "</p>" not in context
    assert "  " not in context
    # The persona section is not a source for the pre-call context, and its
    # internal authoring note must never reach a caller.
    assert "internal authoring note" not in context
    assert "Note:" not in context


# --- 10-11: cost and hardcoding -------------------------------------------------


async def test_the_index_is_fetched_once_and_cached(service: ProgramContextService) -> None:
    await service.for_values({"course": "Tide Pool Diving"})
    await service.for_values({"course": "Kite Repair"})
    await ProgramContextService(KB).for_values({"course": "Tide Pool Diving"})
    assert service.fetch_calls.calls == 1  # type: ignore[attr-defined]


def test_the_source_names_no_course_and_no_fact() -> None:
    """The guard against the failure this design exists to avoid.

    A programme name, fee or duration written into product code would make the
    CRM a second, stale course database — and would break the first workspace
    that is not LevelUp Learning.
    """
    source = Path(pc.__file__).read_text(encoding="utf-8").lower()
    for forbidden in (
        "breakthrough",
        "filmmaking",
        "forge",
        "video editing",
        "residency",
        "bootcamp",
        "retreat",
        "40,000",
        "12 weeks",
        "levelup learning knowledge base is",
    ):
        assert forbidden not in source, f"{forbidden!r} is hardcoded in program_context.py"
    assert "if course ==" not in source


# --- 12-13: through the real trigger --------------------------------------------


@pytest.fixture
def hasher(wired_app: FastAPI) -> PasswordHasherService:
    hasher = wired_app.state.password_hasher
    assert isinstance(hasher, PasswordHasherService)
    return hasher


@pytest.fixture
def bolna(wired_app: FastAPI) -> RecordingBolnaClient:
    client = RecordingBolnaClient()
    wired_app.state.bolna_client = client
    wired_app.state.bolna_settings = BolnaSettings(
        api_key=FAKE_BOLNA_KEY, base_url="https://api.bolna.invalid", agent_id=FAKE_AGENT_ID
    )
    return client


@pytest.fixture
async def ws(db_session: AsyncSession, hasher: PasswordHasherService) -> WorkspaceFixture:
    fixture = await build_workspace(
        db_session, hasher, name="Program Context Co", owner_email="owner@progctx.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    return fixture


@pytest.mark.integration
async def test_existing_user_data_is_unchanged_and_two_keys_are_added(
    api: AsyncClient,
    db_session: AsyncSession,
    wired_app: FastAPI,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The trigger sends everything it sent before, plus exactly two keys.

    The programme lives in a field with an arbitrary name and the workspace's
    H2 is left at its provisioned default — proof that neither is consulted.
    """
    await login(api, ws.owner)
    h2_before = ws.workspace.primary_field_2_id

    created = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "What Are You Looking For", "field_type": "TEXT"},
    )
    assert created.status_code == 201, created.text
    field_key = created.json()["key"]

    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={
            "values": {
                "name": "Ada",
                "phone": "+919000000123",
                "email": "ada@example.com",
                field_key: "Tide Pool Diving",
            }
        },
    )
    assert lead.status_code == 201, lead.text

    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        return _index()

    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    wired_app.state.settings.kb_base_url = KB
    try:
        triggered = await api.post(
            ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
        )
    finally:
        wired_app.state.settings.kb_base_url = None
    assert triggered.status_code == 200, triggered.text
    user_data = triggered.json()["user_data"]

    # Everything that was there before is still there, unchanged.
    assert user_data["name"] == "Ada"
    assert user_data["email"] == "ada@example.com"
    assert user_data["crm_lead_id"] == lead.json()["id"]
    assert user_data["crm_workspace_id"] == str(ws.id)
    assert user_data["crm_is_repeat_caller"] == "no"
    assert user_data["crm_call_count"] == "0"
    # And exactly two keys are new.
    added = set(user_data) - {
        "name",
        "phone",
        "email",
        field_key,
        "crm_lead_id",
        "crm_workspace_id",
        "crm_idempotency",
        "crm_call_count",
        "crm_is_repeat_caller",
        "crm_stage",
        "crm_owner",
        # The caller context (`app.services.lead_context`) rides alongside and
        # is tested in `test_lead_context.py`; this test is about the programme.
        "crm_lead_context",
    }
    assert added == {"crm_program_name", "crm_program_context"}
    assert user_data["crm_program_name"] == "Tide Pool Diving"

    # The workspace's headline configuration was neither read nor written.
    await db_session.refresh(ws.workspace)
    assert ws.workspace.primary_field_2_id == h2_before


@pytest.mark.integration
async def test_the_context_reaches_the_bolna_request(
    api: AsyncClient,
    wired_app: FastAPI,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """What the vendor would actually receive."""
    await login(api, ws.owner)
    created = await api.post(
        ws.path("/settings/lead-fields"),
        headers=ws.owner.auth,
        json={"label": "Enquiry About", "field_type": "TEXT"},
    )
    key = created.json()["key"]

    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Grace", "phone": "+919000000124", key: "02-kite-repair"}},
    )
    assert lead.status_code == 201, lead.text

    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        return _index()

    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    wired_app.state.settings.kb_base_url = KB
    try:
        await api.post(
            ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
        )
    finally:
        wired_app.state.settings.kb_base_url = None

    sent = bolna.last.user_data
    assert sent["crm_program_name"] == "Kite Repair"
    assert "9 weeks" in sent["crm_program_context"]
    assert len(sent["crm_program_context"]) <= MAX_CONTEXT_CHARS


@pytest.mark.integration
async def test_a_lead_with_no_programme_value_still_calls_cleanly(
    api: AsyncClient,
    wired_app: FastAPI,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No match must not break the call; it simply sends no context."""
    await login(api, ws.owner)
    lead = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Alan", "phone": "+919000000125"}},
    )

    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        return _index()

    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    wired_app.state.settings.kb_base_url = KB
    try:
        triggered = await api.post(
            ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead.json()["id"]}
        )
    finally:
        wired_app.state.settings.kb_base_url = None

    assert triggered.status_code == 200, triggered.text
    user_data = triggered.json()["user_data"]
    assert "crm_program_context" not in user_data
    assert "crm_program_name" not in user_data
    assert user_data["name"] == "Alan"
    assert bolna.last.user_data == user_data


# --- the budget, after the pricing allowance was raised -------------------------
#
# These pin the two numbers that were deliberately changed, and the four
# properties that must survive the change. They use their own fixture index,
# shaped like the real knowledge base but carrying no real programme or figure,
# so nothing here reaches the network and no customer fact is committed.

#: A pricing section that opens with a maintainer-facing disclaimer and only
#: then states the fee — the shape that made the old 420-char window spend
#: itself on the preamble. The fee sits past character 420 on purpose.
_DISCLAIMER = (
    "Note Commercial information can change. Always confirm current pricing and "
    "availability with the admissions team before sharing it with a lead. Prices "
    "below are indicative and are reviewed every intake, so treat them as a guide "
    "rather than a quotation, and never commit a caller to an amount that has not "
    "been confirmed by admissions for the intake they are actually applying to. "
    "Confirm the figure for the intake being applied to. "
)
_FEE_BLOCK = (
    "Harbour Fee 85,000 Includes: Residential stay Meals Full experience "
    "Not Included: Travel Application Fee 800-900 Refundable if not selected "
    "Booking Amount Paid within 24 hours after selection Confirms place "
    "Remaining Balance Paid according to the admissions timeline Venue Harbour"
)
_LONG_PRICING = _DISCLAIMER + _FEE_BLOCK

#: Where the old window stopped and the new one reaches.
_OLD_PRICING_ALLOWANCE = 420


def _budget_index() -> list[dict[str, str]]:
    """One fictional programme whose every section overflows its allowance."""
    slug, title = "01-harbour-navigation", "Harbour Navigation"
    filler = "identity " * 200
    qa = "Answer: a question and its answer. " * 200
    return [
        _page(f"03-courses/{slug}/", title, "<p>The Harbour Navigation knowledge base.</p>"),
        _page(
            f"03-courses/{slug}/01-program-identity/#x",
            "Identity",
            f"<p>{title} runs for 9 weeks, live online, on weekends. {filler}</p>",
        ),
        _page(f"03-courses/{slug}/07-pricing-and-cohorts/#x", "Pricing", f"<p>{_LONG_PRICING}</p>"),
        _page(f"03-courses/{slug}/02-questions-and-answers/#x", "Q&A", f"<p>{qa}</p>"),
        _page(
            f"03-courses/{slug}/03-persona-positioning/#x",
            "Persona",
            "<p>This guide helps admissions agents position the programme. "
            "Emphasize that the learner does not need everything figured out.</p>",
        ),
    ]


#: A second base URL, because the index cache is keyed by it: a test that uses
#: both fixture indexes at once would otherwise serve one from the other's
#: cache entry.
KB_BUDGET = "https://kb-budget.invalid"


@pytest.fixture
def budget_service(monkeypatch: pytest.MonkeyPatch) -> ProgramContextService:
    async def _docs(self: ProgramContextService) -> list[dict[str, str]]:
        return _budget_index()

    monkeypatch.setattr(ProgramContextService, "_load_docs", _docs)
    return ProgramContextService(KB_BUDGET)


def test_the_ceiling_is_1800() -> None:
    assert MAX_CONTEXT_CHARS == 1800


def test_the_section_table_is_the_approved_one() -> None:
    """The two changed numbers, and the three that were not to change.

    Ordering is part of the contract: identity first, then pricing, then the
    FAQ slice, because the ceiling trims from the end.
    """
    assert pc.SECTION_PRIORITY == (
        ("01-program-identity", "Identity, duration, delivery, schedule, audience", 620),
        ("07-pricing-and-cohorts", "Pricing and payment", 520),
        ("02-questions-and-answers", "Common questions", 380),
    )


def test_the_persona_section_is_not_a_source() -> None:
    """It is written for whoever operates the agent, not for a caller."""
    assert not any(section.startswith("03-") for section, _, _ in pc.SECTION_PRIORITY)
    assert "03-persona-positioning" not in {s for s, _, _ in pc.SECTION_PRIORITY}


async def test_no_persona_content_reaches_the_context(
    budget_service: ProgramContextService,
) -> None:
    """Its maintainer-facing framing must never be in front of a caller."""
    context = (await budget_service.for_values({"course": "Harbour Navigation"}))[
        "crm_program_context"
    ]
    for forbidden in ("helps admissions agents", "Emphasize", "position the programme"):
        assert forbidden not in context, f"persona text {forbidden!r} reached the caller context"


async def test_a_programme_that_overflows_every_section_stays_within_the_ceiling(
    budget_service: ProgramContextService,
) -> None:
    """Every section longer than its allowance, so the ceiling is what binds."""
    context = (await budget_service.for_values({"course": "Harbour Navigation"}))[
        "crm_program_context"
    ]
    assert 0 < len(context) <= MAX_CONTEXT_CHARS, len(context)


async def test_every_programme_in_the_index_stays_within_the_ceiling(
    service: ProgramContextService,
) -> None:
    """Checked per programme, not just for the biggest one."""
    for course in ("Tide Pool Diving", "Kite Repair"):
        context = (await service.for_values({"course": course}))["crm_program_context"]
        assert 0 < len(context) <= MAX_CONTEXT_CHARS, (course, len(context))


async def test_identity_and_questions_still_reach_the_caller(
    budget_service: ProgramContextService,
) -> None:
    """Raising the pricing allowance must not evict what was already there."""
    context = (await budget_service.for_values({"course": "Harbour Navigation"}))[
        "crm_program_context"
    ]
    assert "Identity, duration, delivery, schedule, audience:" in context
    assert "9 weeks, live online, on weekends" in context
    assert "Common questions:" in context
    assert "a question and its answer" in context


async def test_pricing_gains_about_a_hundred_characters(
    budget_service: ProgramContextService,
) -> None:
    """The pricing line is now ~520 chars of body rather than ~420."""
    context = (await budget_service.for_values({"course": "Harbour Navigation"}))[
        "crm_program_context"
    ]
    line = next(ln for ln in context.splitlines() if ln.startswith("Pricing and payment:"))
    body = line.split("Pricing and payment: ", 1)[1].rstrip("…").rstrip()
    allowance = {s: a for s, _, a in pc.SECTION_PRIORITY}["07-pricing-and-cohorts"]
    assert allowance == 520
    # Trimming is on a word boundary, so the body lands just under the allowance.
    assert allowance - 20 <= len(body) <= allowance
    assert len(body) - _OLD_PRICING_ALLOWANCE >= 80


async def test_a_disclaimer_first_pricing_section_still_yields_the_fee(
    budget_service: ProgramContextService,
) -> None:
    """The reason the allowance was raised.

    A section whose first 420 characters are a "confirm with admissions"
    preamble used to reach the agent with no amount in it at all. The fee in
    this fixture sits past that point, so this fails on the old allowance and
    passes on the new one.
    """
    assert _LONG_PRICING.index("Harbour Fee") > _OLD_PRICING_ALLOWANCE
    context = (await budget_service.for_values({"course": "Harbour Navigation"}))[
        "crm_program_context"
    ]
    # The headline amount now arrives. The rest of the fee block still does not
    # fit, and is still answerable from the knowledge base — the point of the
    # change is that the agent is no longer handed a window with no figure in it.
    assert "85,000" in context
    assert "Harbour Fee" in context
