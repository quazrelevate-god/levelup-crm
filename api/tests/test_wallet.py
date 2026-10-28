"""Prepaid AI-call wallets (docs/14).

The rules under test, in the order a client meets them:

1. A workspace without a wallet is not metered — calling works exactly as it
   did before wallets existed.
2. A metered workspace cannot dial without at least one minute's credit, and a
   refused call leaves nothing behind: no execution row, nothing sent to Bolna.
3. Only connected time is billed, per started minute, once per call — however
   many times Bolna delivers the result, and even when the duration arrives
   late.
4. A call in progress is never cut off: the last call may take the balance
   negative, and the *next* one is refused.
5. Money only enters through the operator; the ledger's signs are enforced by
   the database itself.
6. Members see their wallet if their template allows billing, and never
   another workspace's.
"""

from __future__ import annotations

import argparse
import datetime as dt
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI, HTTPException
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from tests.factories import WorkspaceFixture, add_member, build_workspace, login

from app.auth.passwords import PasswordHasherService
from app.config import Settings
from app.integrations.bolna import BolnaSettings, RecordingBolnaClient
from app.models.billing import Wallet, WalletTransaction
from app.models.enums import WalletTransactionKind
from app.models.voice import VoiceCallExecution
from app.services.wallet import WalletService, billable_minutes, price_of
from app.tenancy.session import ScopedSession
from app.wallet.__main__ import _run as run_wallet_cli

FAKE_BOLNA_KEY = "bn-test-key-do-not-use-0000000000"
FAKE_AGENT_ID = "11111111-2222-3333-4444-555555555555"
PHONE = "+919087822357"
RATE = Decimal("9.00")


# --- 0. the arithmetic ----------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "minutes"),
    [(0, 0), (-5, 0), (1, 1), (59, 1), (60, 1), (61, 2), (154, 3), (600, 10)],
)
def test_every_started_minute_is_a_whole_minute(seconds: int, minutes: int) -> None:
    assert billable_minutes(seconds) == minutes


def test_prices_are_exact_to_the_cent() -> None:
    assert price_of(3, Decimal("9.00")) == Decimal("27.00")
    assert price_of(1, Decimal("8.50")) == Decimal("8.50")
    assert price_of(0, Decimal("9.00")) == Decimal("0.00")
    assert price_of(7, Decimal("7.63")) == Decimal("53.41")


# --- fixtures and helpers -------------------------------------------------------


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
        db_session, hasher, name="Wallet Co", owner_email="owner@wallet.example"
    )
    fixture.workspace.identity_field_id = fixture.fields["name"].id
    await db_session.commit()
    return fixture


def _service(session: AsyncSession, ws: WorkspaceFixture) -> WalletService:
    return WalletService(ScopedSession(session, ws.id), workspace=ws.workspace)


async def _meter(
    session: AsyncSession, ws: WorkspaceFixture, *, balance: str = "0", rate: Decimal = RATE
) -> None:
    """What the operator does at onboarding: create, then (maybe) top up."""
    service = _service(session, ws)
    await service.create(rate_per_minute=rate)
    if Decimal(balance) > 0:
        await service.record(
            WalletTransactionKind.TOPUP, Decimal(balance), note="opening", recorded_by="test"
        )
    await session.commit()


async def _webhook_key(api: AsyncClient, ws: WorkspaceFixture) -> str:
    await login(api, ws.owner)
    response = await api.post(
        ws.path("/settings/api-keys"),
        headers=ws.owner.auth,
        json={"name": "Bolna webhook", "permission_template_id": str(ws.templates["Root"].id)},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["key"])


async def _lead(api: AsyncClient, ws: WorkspaceFixture) -> str:
    response = await api.post(
        ws.path("/leads"),
        headers=ws.owner.auth,
        json={"values": {"name": "Perumal", "phone": PHONE}},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


async def _dial(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> Any:
    return await api.post(ws.path("/voice/calls"), headers=ws.owner.auth, json={"lead_id": lead_id})


async def _dialled(api: AsyncClient, ws: WorkspaceFixture, lead_id: str) -> str:
    response = await _dial(api, ws, lead_id)
    assert response.status_code == 200, response.text
    return str(response.json()["execution_id"])


def _result(
    execution_id: str,
    *,
    status: str = "completed",
    duration: float | None = 154,
    summary: str | None = "Interested; wants the fee link.",
) -> dict[str, Any]:
    """Bolna's documented execution object, as `test_voice_post_call` uses it."""
    body: dict[str, Any] = {
        "id": execution_id,
        "agent_id": FAKE_AGENT_ID,
        "status": status,
        "conversation_duration": duration,
        "transcript": "assistant: Hello?\nuser: Yes." if duration else None,
        "user_number": PHONE,
        "extracted_data": None,
        "telephony_data": {
            "duration": str(duration) if duration is not None else None,
            "to_number": PHONE,
            "call_type": "outbound",
        },
    }
    if summary is not None:
        body["summary"] = summary
    return body


async def _deliver(api: AsyncClient, key: str, body: dict[str, Any]) -> Any:
    response = await api.post(f"/api/v1/voice/bolna/{key}", json=body)
    assert response.status_code == 200, response.text
    return response.json()


async def _wallet(session: AsyncSession, ws: WorkspaceFixture) -> Wallet:
    rows = await session.execute(
        select(Wallet).where(Wallet.workspace_id == ws.id).execution_options(populate_existing=True)
    )
    return rows.scalar_one()


async def _charges(session: AsyncSession, ws: WorkspaceFixture) -> list[WalletTransaction]:
    rows = await session.execute(
        select(WalletTransaction)
        .where(
            WalletTransaction.workspace_id == ws.id,
            WalletTransaction.kind == WalletTransactionKind.CALL_CHARGE,
        )
        .order_by(WalletTransaction.created_at)
    )
    return list(rows.scalars().all())


# --- 1. unmetered ---------------------------------------------------------------


@pytest.mark.integration
async def test_a_workspace_without_a_wallet_calls_and_is_never_billed(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))
    await _deliver(api, key, _result(execution_id))

    assert len(bolna.calls) == 1
    count = await db_session.execute(select(func.count()).select_from(WalletTransaction))
    assert count.scalar() == 0


# --- 2. the gate ----------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.parametrize("balance", ["0", "8.99"])
async def test_a_wallet_that_cannot_cover_a_minute_refuses_to_dial(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    balance: str,
) -> None:
    await _meter(db_session, ws, balance=balance)
    await login(api, ws.owner)
    lead_id = await _lead(api, ws)

    response = await _dial(api, ws, lead_id)

    assert response.status_code == 402, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "wallet_insufficient_balance"
    assert detail["required"] == "9.00"
    assert Decimal(detail["balance"]) == Decimal(balance)
    # Refused before anything happened: nothing sent, nothing to retry.
    assert bolna.calls == []
    rows = await db_session.execute(select(func.count()).select_from(VoiceCallExecution))
    assert rows.scalar() == 0


@pytest.mark.integration
async def test_exactly_one_minutes_credit_is_enough_to_dial(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="9")
    await login(api, ws.owner)
    response = await _dial(api, ws, await _lead(api, ws))
    assert response.status_code == 200, response.text
    assert len(bolna.calls) == 1


# --- 3. billing -----------------------------------------------------------------


@pytest.mark.integration
async def test_a_connected_call_is_billed_per_started_minute(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))

    await _deliver(api, key, _result(execution_id, duration=154))

    [charge] = await _charges(db_session, ws)
    assert charge.amount == Decimal("-27.00")  # 154s = 3 started minutes x 9
    assert charge.billed_seconds == 154
    assert charge.billed_minutes == 3
    assert charge.rate_per_minute == RATE
    assert charge.balance_after == Decimal("73.00")
    assert charge.currency == ws.workspace.currency
    call = await db_session.execute(
        select(VoiceCallExecution).where(VoiceCallExecution.external_id == execution_id)
    )
    assert charge.voice_call_id == call.scalar_one().id
    assert (await _wallet(db_session, ws)).balance == Decimal("73.00")


@pytest.mark.integration
async def test_bolna_repeating_the_result_never_bills_twice(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))

    first = await _deliver(api, key, _result(execution_id))
    again = await _deliver(api, key, _result(execution_id))

    assert first["status"] == "accepted"
    assert again["status"] == "duplicate"
    assert len(await _charges(db_session, ws)) == 1
    assert (await _wallet(db_session, ws)).balance == Decimal("73.00")


@pytest.mark.integration
@pytest.mark.parametrize(
    ("status", "duration"),
    [
        ("no-answer", 0),
        ("busy", None),
        ("failed", 0),
        ("completed", 0),
        ("completed", None),
        # The case that matters: an unanswered call that still reports
        # seconds — ringing time, or a vendor-side hangup. Not connected, so
        # not billed, whatever the duration says.
        ("no-answer", 25),
        ("busy", 8),
        ("failed", 40),
    ],
)
async def test_calls_that_never_connected_are_free(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    bolna: RecordingBolnaClient,
    status: str,
    duration: float | None,
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))

    await _deliver(api, key, _result(execution_id, status=status, duration=duration))

    assert await _charges(db_session, ws) == []
    assert (await _wallet(db_session, ws)).balance == Decimal("100.00")


@pytest.mark.integration
async def test_a_duration_that_arrives_late_is_billed_once_when_it_arrives(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """Bolna's first terminal delivery can lack the duration; a later one has it."""
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))

    await _deliver(api, key, _result(execution_id, duration=None, summary=None))
    assert await _charges(db_session, ws) == []

    upgraded = await _deliver(api, key, _result(execution_id, duration=154))
    assert upgraded["status"] == "upgraded"
    [charge] = await _charges(db_session, ws)
    assert charge.billed_minutes == 3
    assert charge.amount == Decimal("-27.00")

    # And the upgrade itself is not billable twice.
    await _deliver(api, key, _result(execution_id, duration=154))
    assert len(await _charges(db_session, ws)) == 1


@pytest.mark.integration
async def test_settling_a_call_a_second_time_owes_nothing(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    """`settle_call` itself is idempotent, independent of the webhook gate."""
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))
    await _deliver(api, key, _result(execution_id))

    call = (
        await db_session.execute(
            select(VoiceCallExecution).where(VoiceCallExecution.external_id == execution_id)
        )
    ).scalar_one()
    assert await _service(db_session, ws).settle_call(call) is None
    assert len(await _charges(db_session, ws)) == 1


# --- 4. running out -------------------------------------------------------------


@pytest.mark.integration
async def test_the_last_call_may_go_negative_and_the_next_is_refused(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="10")  # covers one minute
    key = await _webhook_key(api, ws)
    lead_id = await _lead(api, ws)
    execution_id = await _dialled(api, ws, lead_id)

    await _deliver(api, key, _result(execution_id, duration=154))  # 3 minutes: 27

    assert (await _wallet(db_session, ws)).balance == Decimal("-17.00")
    refused = await _dial(api, ws, lead_id)
    assert refused.status_code == 402
    assert len(bolna.calls) == 1

    # A top-up that clears the debt and covers a minute lets calling resume.
    await _service(db_session, ws).record(
        WalletTransactionKind.TOPUP, Decimal("26"), note="UPI", recorded_by="test"
    )
    await db_session.commit()
    assert (await _dial(api, ws, lead_id)).status_code == 200


@pytest.mark.integration
async def test_a_rate_change_never_reprices_a_call_already_billed(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    first = await _dialled(api, ws, await _lead(api, ws))
    await _deliver(api, key, _result(first, duration=60))

    await _service(db_session, ws).set_rate(Decimal("8"))
    await db_session.commit()

    [old] = await _charges(db_session, ws)
    assert old.rate_per_minute == Decimal("9.00")
    assert old.amount == Decimal("-9.00")
    assert (await _wallet(db_session, ws)).rate_per_minute == Decimal("8.00")


# --- 5. the operator and the ledger ---------------------------------------------


@pytest.mark.integration
async def test_operator_entries_follow_their_rules(
    db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await _meter(db_session, ws)
    service = _service(db_session, ws)

    def code(exc: pytest.ExceptionInfo[HTTPException]) -> str:
        detail = exc.value.detail
        assert isinstance(detail, dict)
        return str(detail["code"])

    with pytest.raises(HTTPException) as negative_topup:
        await service.record(WalletTransactionKind.TOPUP, Decimal("-5"), note=None, recorded_by="t")
    assert code(negative_topup) == "invalid_amount"

    with pytest.raises(HTTPException) as silent_adjustment:
        await service.record(
            WalletTransactionKind.ADJUSTMENT, Decimal("-5"), note="  ", recorded_by="t"
        )
    assert code(silent_adjustment) == "note_required"

    with pytest.raises(HTTPException) as forged_charge:
        await service.record(
            WalletTransactionKind.CALL_CHARGE, Decimal("-5"), note="x", recorded_by="t"
        )
    assert code(forged_charge) == "invalid_kind"

    await service.record(WalletTransactionKind.TOPUP, Decimal("500"), note="UPI", recorded_by="t")
    await service.record(
        WalletTransactionKind.DISCOUNT, Decimal("50"), note="Launch offer", recorded_by="t"
    )
    await service.record(
        WalletTransactionKind.ADJUSTMENT, Decimal("-20"), note="Duplicate test", recorded_by="t"
    )
    await db_session.commit()
    assert (await _wallet(db_session, ws)).balance == Decimal("530.00")


@pytest.mark.integration
async def test_a_second_wallet_for_one_workspace_is_refused(
    db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    await _meter(db_session, ws)
    with pytest.raises(HTTPException) as exc:
        await _service(db_session, ws).create(rate_per_minute=RATE)
    assert exc.value.status_code == 409


@pytest.mark.integration
@pytest.mark.parametrize(
    ("kind", "amount"),
    [
        (WalletTransactionKind.TOPUP, "-1"),
        (WalletTransactionKind.DISCOUNT, "0"),
        (WalletTransactionKind.CALL_CHARGE, "5"),
        (WalletTransactionKind.ADJUSTMENT, "0"),
    ],
)
async def test_the_database_rejects_a_ledger_row_with_the_wrong_sign(
    db_session: AsyncSession, ws: WorkspaceFixture, kind: WalletTransactionKind, amount: str
) -> None:
    """Defence in depth: even a writer that skips the service cannot do this."""
    await _meter(db_session, ws)
    wallet = await _wallet(db_session, ws)
    db_session.add(
        WalletTransaction(
            workspace_id=ws.id,
            wallet_id=wallet.id,
            kind=kind,
            amount=Decimal(amount),
            currency="INR",
            balance_after=Decimal("0"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


def test_today_and_this_month_are_the_workspaces_own() -> None:
    """Rule 10: 19:00 UTC on the 21st is already the 22nd in Kolkata."""

    class _Workspace:
        timezone = "Asia/Kolkata"

    service = WalletService.__new__(WalletService)
    service._workspace = _Workspace()  # type: ignore[assignment]
    day, month = service.period_starts(dt.datetime(2026, 10, 21, 19, 0, tzinfo=dt.UTC))
    assert (day.day, day.hour, day.minute) == (22, 0, 0)
    assert day.utcoffset() == dt.timedelta(hours=5, minutes=30)
    assert (month.month, month.day) == (10, 1)


# --- 6. what members can see ----------------------------------------------------


@pytest.mark.integration
async def test_the_wallet_endpoint_shows_balance_rate_and_minutes_used(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))
    await _deliver(api, key, _result(execution_id, duration=154))

    response = await api.get(ws.path("/wallet"), headers=ws.owner.auth)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["balance"] == "73.00"  # money is a string, never a float
    assert body["rate_per_minute"] == "9.00"
    assert body["minimum_to_call"] == "9.00"
    assert body["can_call"] is True
    assert body["today"] == {"minutes": 3, "connected_calls": 1, "spent": "27.00"}
    assert body["this_month"]["minutes"] == 3


@pytest.mark.integration
async def test_the_statement_is_newest_first_filterable_and_hides_the_operator(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture, bolna: RecordingBolnaClient
) -> None:
    await _meter(db_session, ws, balance="100")
    key = await _webhook_key(api, ws)
    execution_id = await _dialled(api, ws, await _lead(api, ws))
    await _deliver(api, key, _result(execution_id, duration=61))

    response = await api.get(ws.path("/wallet/transactions"), headers=ws.owner.auth)
    assert response.status_code == 200, response.text
    page = response.json()
    assert page["total"] == 2
    assert [item["kind"] for item in page["items"]] == ["CALL_CHARGE", "TOPUP"]
    assert page["items"][0]["amount"] == "-18.00"  # 61s = 2 minutes
    assert page["items"][1]["note"] == "opening"
    assert "recorded_by" not in page["items"][1]

    only_topups = await api.get(
        ws.path("/wallet/transactions"), headers=ws.owner.auth, params={"kind": "TOPUP"}
    )
    assert [item["kind"] for item in only_topups.json()["items"]] == ["TOPUP"]


@pytest.mark.integration
async def test_an_unmetered_workspace_has_no_wallet_to_show(
    api: AsyncClient, ws: WorkspaceFixture
) -> None:
    await login(api, ws.owner)
    response = await api.get(ws.path("/wallet"), headers=ws.owner.auth)
    assert response.status_code == 404
    assert response.json()["detail"]["code"] == "wallet_not_configured"


@pytest.mark.integration
async def test_a_template_without_billing_cannot_see_the_wallet(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    hasher: PasswordHasherService,
) -> None:
    """Billing is Root's by default; a Caller has no business seeing it."""
    await _meter(db_session, ws, balance="100")
    caller = await add_member(
        db_session, hasher, ws, key="rep", email="rep@wallet.example", template_name="Caller"
    )
    await login(api, caller)
    for path in ("/wallet", "/wallet/transactions"):
        response = await api.get(ws.path(path), headers=caller.auth)
        assert response.status_code == 403, path


@pytest.mark.integration
async def test_no_member_endpoint_can_move_money(
    api: AsyncClient, db_session: AsyncSession, ws: WorkspaceFixture
) -> None:
    """Read-only by construction: there is no route to POST, PUT or PATCH."""
    await _meter(db_session, ws, balance="100")
    await login(api, ws.owner)
    for method in ("POST", "PUT", "PATCH", "DELETE"):
        for path in ("/wallet", "/wallet/transactions"):
            response = await api.request(
                method, ws.path(path), headers=ws.owner.auth, json={"amount": "1000"}
            )
            assert response.status_code == 405, f"{method} {path}"
    assert (await _wallet(db_session, ws)).balance == Decimal("100.00")


@pytest.mark.integration
async def test_another_workspaces_wallet_is_a_404_and_never_leaks(
    api: AsyncClient,
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    hasher: PasswordHasherService,
) -> None:
    other = await build_workspace(
        db_session, hasher, name="Other Co", owner_email="owner@other.example"
    )
    await _meter(db_session, ws, balance="100")
    # A figure that cannot occur by chance inside a timestamp or an id: an ISO
    # time never has four digits before its decimal point, a UUID has no dot.
    await _meter(db_session, other, balance="4321.98")
    await login(api, ws.owner)

    for path in ("/wallet", "/wallet/transactions"):
        response = await api.get(other.path(path), headers=ws.owner.auth)
        assert response.status_code == 404, path

    own = await api.get(ws.path("/wallet/transactions"), headers=ws.owner.auth)
    assert own.status_code == 200
    assert "4321.98" not in own.text
    assert str(other.id) not in own.text


# --- 7. the operator's command line ---------------------------------------------


@pytest.mark.integration
async def test_the_command_line_meters_tops_up_and_reports(
    db_session: AsyncSession,
    ws: WorkspaceFixture,
    settings: Settings,
    postgres_async_dsn: str,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cli_settings = settings.model_copy(update={"database_url": postgres_async_dsn})
    slug = ws.workspace.slug

    def args(command: str, **extra: Any) -> argparse.Namespace:
        return argparse.Namespace(command=command, workspace=slug, **extra)

    assert await run_wallet_cli(args("create", rate=Decimal("9")), cli_settings) == 0
    topup = args("topup", amount=Decimal("1000"), note="UPI ref 1", by="ops", yes=True)
    assert await run_wallet_cli(topup, cli_settings) == 0

    # Declining the confirmation records nothing.
    monkeypatch.setattr("builtins.input", lambda _prompt: "n")
    declined = args("discount", amount=Decimal("50"), note="maybe", by="ops", yes=False)
    assert await run_wallet_cli(declined, cli_settings) == 1

    assert await run_wallet_cli(args("show"), cli_settings) == 0
    assert await run_wallet_cli(argparse.Namespace(command="list"), cli_settings) == 0
    out = capsys.readouterr().out
    assert "balance   " in out and "1000.00" in out
    assert "nothing recorded" in out
    assert slug in out

    await db_session.rollback()  # see the CLI's committed rows
    wallet = await _wallet(db_session, ws)
    assert wallet.balance == Decimal("1000.00")
    rows = await db_session.execute(
        select(WalletTransaction).where(WalletTransaction.workspace_id == ws.id)
    )
    [entry] = rows.scalars().all()
    assert (entry.kind, entry.recorded_by, entry.note) == (
        WalletTransactionKind.TOPUP,
        "ops",
        "UPI ref 1",
    )


@pytest.mark.integration
async def test_the_command_line_refuses_an_unknown_workspace(
    db_session: AsyncSession, settings: Settings, postgres_async_dsn: str
) -> None:
    cli_settings = settings.model_copy(update={"database_url": postgres_async_dsn})
    with pytest.raises(SystemExit, match="no workspace matches"):
        await run_wallet_cli(
            argparse.Namespace(command="show", workspace=f"nope-{uuid.uuid4()}"), cli_settings
        )
