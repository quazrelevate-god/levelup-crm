"""The operator's command line for AI-call wallets (docs/14).

Money enters a wallet only from here — never from inside a workspace — so a
client's own admin cannot credit their own account.

    uv run python -m app.wallet list
    uv run python -m app.wallet show     --workspace levelup-learning
    uv run python -m app.wallet create   --workspace levelup-learning --rate 9
    uv run python -m app.wallet topup    --workspace levelup-learning --amount 5000 \\
                                         --note "UPI ref 4821"
    uv run python -m app.wallet discount --workspace levelup-learning --amount 500 \\
                                         --note "Launch offer"
    uv run python -m app.wallet adjust   --workspace levelup-learning --amount -18 \\
                                         --note "Test call billed twice"
    uv run python -m app.wallet set-rate --workspace levelup-learning --rate 8

`--workspace` takes a slug, an id, or the exact name. Anything that moves money
shows what it is about to do and asks first; `--yes` skips the question for
scripts. Notes appear on the client's statement.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import getpass
import sys
import uuid
from decimal import Decimal, InvalidOperation

from fastapi import HTTPException
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.db import create_engine, create_session_factory
from app.models.billing import Wallet
from app.models.enums import WalletTransactionKind
from app.models.workspace import Workspace
from app.schemas.common import PageParams
from app.services.wallet import WalletService
from app.tenancy.session import ScopedSession

_MOVES = {
    "topup": WalletTransactionKind.TOPUP,
    "discount": WalletTransactionKind.DISCOUNT,
    "adjust": WalletTransactionKind.ADJUSTMENT,
}


def _decimal(value: str) -> Decimal:
    try:
        return Decimal(value)
    except InvalidOperation as exc:
        raise argparse.ArgumentTypeError(f"not an amount: {value!r}") from exc


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="python -m app.wallet", description="Manage AI-call wallets (operator only)."
    )
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("list", help="Every metered workspace and its balance")

    def workspace_command(name: str, help_text: str) -> argparse.ArgumentParser:
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--workspace", required=True, help="Slug, id or exact name")
        return sub

    workspace_command("show", "Balance, rate, usage and recent statement lines")

    create = workspace_command("create", "Start metering a workspace (opening balance 0)")
    create.add_argument("--rate", type=_decimal, required=True, help="Per minute, before GST")

    rate = workspace_command("set-rate", "Change the per-minute price of future calls")
    rate.add_argument("--rate", type=_decimal, required=True, help="Per minute, before GST")

    for name, help_text in (
        ("topup", "Record a payment from the client"),
        ("discount", "Grant credit without a payment"),
        ("adjust", "Correct the balance (+ or -); needs --note"),
    ):
        sub = workspace_command(name, help_text)
        sub.add_argument("--amount", type=_decimal, required=True)
        sub.add_argument("--note", help="Shown on the client's statement")
        sub.add_argument("--by", default=getpass.getuser(), help="Who is recording this")
        sub.add_argument("--yes", action="store_true", help="Do not ask for confirmation")
    return parser.parse_args(argv)


async def _find_workspace(session: AsyncSession, ref: str) -> Workspace:
    conditions = [Workspace.slug == ref, Workspace.name == ref]
    with contextlib.suppress(ValueError):  # not every reference is an id
        conditions.append(Workspace.id == uuid.UUID(ref))
    rows = await session.execute(select(Workspace).where(or_(*conditions)))
    found = list(rows.scalars().all())
    if not found:
        raise SystemExit(f"error: no workspace matches {ref!r}")
    if len(found) > 1:
        raise SystemExit(f"error: {ref!r} matches several workspaces; use the slug or id")
    return found[0]


def _confirm(prompt: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    return input(f"{prompt} [y/N] ").strip().lower() in {"y", "yes"}


async def _show(service: WalletService, workspace: Workspace) -> None:
    wallet = await service.require()
    today, month = service.period_starts()
    used_today = await service.usage(today)
    used_month = await service.usage(month)
    print(f"{workspace.name} ({workspace.slug})")
    print(f"  balance   {wallet.currency} {wallet.balance}")
    print(f"  rate      {wallet.currency} {wallet.rate_per_minute} per minute, before GST")
    print(
        f"  today     {used_today.minutes} min, {used_today.connected_calls} calls, "
        f"{wallet.currency} {used_today.spent}"
    )
    print(
        f"  month     {used_month.minutes} min, {used_month.connected_calls} calls, "
        f"{wallet.currency} {used_month.spent}"
    )
    lines = await service.statement(PageParams(limit=10, offset=0))
    if lines.items:
        print("  recent:")
    for row in lines.items:
        when = row.created_at.astimezone().strftime("%d %b %H:%M")
        detail = f"{row.billed_minutes} min" if row.billed_minutes else (row.note or "")
        print(
            f"    {when}  {row.kind.value:<11} {row.amount:>10}  "
            f"-> {row.balance_after:>10}  {detail}"
        )


async def _run(args: argparse.Namespace, settings: Settings) -> int:
    engine = create_engine(settings)
    session_factory = create_session_factory(engine)
    try:
        async with session_factory() as session:
            if args.command == "list":
                rows = await session.execute(
                    select(Workspace, Wallet)
                    .join(Wallet, Wallet.workspace_id == Workspace.id)
                    .order_by(Workspace.name)
                )
                found = rows.all()
                if not found:
                    print("No metered workspaces yet. Start one with `create`.")
                for workspace, wallet in found:
                    print(
                        f"{workspace.slug:<30} {wallet.currency} {wallet.balance:>12}  "
                        f"@ {wallet.rate_per_minute}/min"
                    )
                return 0

            workspace = await _find_workspace(session, args.workspace)
            service = WalletService(ScopedSession(session, workspace.id), workspace=workspace)

            if args.command == "show":
                await _show(service, workspace)
                return 0

            if args.command == "create":
                wallet = await service.create(rate_per_minute=args.rate)
                await session.commit()
                print(
                    f"metering {workspace.name}: {wallet.currency} {wallet.rate_per_minute}"
                    " per minute, balance 0. Calls are blocked until the first top-up."
                )
                return 0

            if args.command == "set-rate":
                wallet = await service.require()
                old = wallet.rate_per_minute
                await service.set_rate(args.rate)
                await session.commit()
                print(f"{workspace.name}: rate {old} -> {wallet.rate_per_minute} per minute")
                return 0

            kind = _MOVES[args.command]
            wallet = await service.require()
            prompt = (
                f"Record {kind.value} of {wallet.currency} {args.amount} for {workspace.name}"
                f" (balance {wallet.balance} -> {wallet.balance + args.amount})?"
            )
            if not _confirm(prompt, args.yes):
                print("nothing recorded")
                return 1
            row = await service.record(kind, args.amount, note=args.note, recorded_by=args.by)
            await session.commit()
            print(
                f"recorded {kind.value} {row.currency} {row.amount}; "
                f"balance is now {row.currency} {row.balance_after}"
            )
            return 0
    except HTTPException as exc:
        # `api_error` puts `{code, message}` in `detail`; anything else is a string.
        raw: object = exc.detail
        message = raw.get("message", raw) if isinstance(raw, dict) else raw
        print(f"error: {message}", file=sys.stderr)
        return 1
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    return asyncio.run(_run(args, get_settings()))


if __name__ == "__main__":
    raise SystemExit(main())
