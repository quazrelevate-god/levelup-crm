# 14 — Prepaid wallet for AI-call minutes

**Status: additive.** Two new tables (`wallets`, `wallet_transactions`), one
enum, revision `0017_wallets`. Nothing existing is altered. A workspace without
a wallet behaves exactly as before.

Clients pay for AI calling in advance. They recharge a wallet; every connected
call draws it down by the minute; a wallet that has run out stops new calls
until it is recharged. No invoices, no chasing payment.

---

## 1. The rules

| Rule | Detail |
|---|---|
| **What is billed** | Connected time only: a call Bolna reports `completed` with a positive duration. Unanswered, busy, failed and zero-second calls are free — even when they report ringing seconds. |
| **Unit** | Per started minute. 61 seconds is two minutes. |
| **Price** | `rate_per_minute` on the wallet, **before GST**. GST is charged on the recharge invoice: a client who pays ₹5,900 gets ₹5,000 of credit, and each minute deducts the plain rate. Set per workspace, so one client can be on a different price from another. |
| **Running out** | A call in progress is never cut off, so the last call may take the balance below zero. The *next* call is refused until the wallet covers at least one minute. |
| **Who adds money** | Only the operator, from the command line (§3). No workspace endpoint can move money — a client's own Root cannot credit their wallet. |
| **Who can see it** | Members whose template grants `billings.view_billing`: Root by default; Admin only if a workspace opts in. |
| **Metering** | Only workspaces with a wallet. Internal, demo and test workspaces have none and call freely. Create the wallet when a client is onboarded. |

One call is billed at most once. The webhook is idempotent already (docs/13
§4); on top of that, `WalletService.settle_call` charges a call only what it
still owes, so a retried or out-of-order delivery adds nothing, and a duration
that arrives in a *later* delivery is billed when it arrives.

## 2. The ledger

`wallet_transactions` is append-only. A row is never edited or deleted; a
mistake is corrected with a further `ADJUSTMENT` row. Each row carries
`balance_after`, so a statement reads without re-adding history.

| Kind | Sign (database-enforced) | Recorded by |
|---|---|---|
| `TOPUP` | > 0 | operator: a payment from the client |
| `DISCOUNT` | > 0 | operator: credit with no payment, kept apart from top-ups so revenue and concessions never blur |
| `ADJUSTMENT` | ≠ 0, note required | operator: a correction either way |
| `CALL_CHARGE` | < 0 | the system, when a call completes |

A call charge records the seconds and minutes it billed and the rate it was
priced at, so a later rate change never rewrites an old statement. Notes are
shown on the client's statement; `recorded_by` (who on the operator's side made
the entry) is not.

`wallets.balance` is a cached total, updated in the same transaction as the
ledger row that moves it, under a row lock.

## 3. Operating it

```bash
cd api
uv run python -m app.wallet list                                   # every metered workspace
uv run python -m app.wallet create   --workspace <slug> --rate 9   # start metering, balance 0
uv run python -m app.wallet topup    --workspace <slug> --amount 5000 --note "UPI ref …"
uv run python -m app.wallet discount --workspace <slug> --amount 500  --note "Launch offer"
uv run python -m app.wallet adjust   --workspace <slug> --amount -18  --note "Why"
uv run python -m app.wallet set-rate --workspace <slug> --rate 8
uv run python -m app.wallet show     --workspace <slug>            # balance, usage, recent lines
```

`--workspace` takes a slug, an id or the exact name. Every money movement shows
the balance before and after and asks for confirmation; `--yes` skips it for
scripts. On a hosted deployment, run the same commands in the API service's
shell.

## 4. API

Read-only, gated on `billings.view_billing`:

- `GET /workspaces/{id}/wallet` — balance, rate, whether a call may be placed
  now, the minimum needed, and minutes / connected calls / spend for today and
  this month in the **workspace's** timezone (rule 10). `404
  wallet_not_configured` when the workspace is not metered.
- `GET /workspaces/{id}/wallet/transactions?kind=&limit=&offset=` — the
  statement, newest first.

Amounts are JSON strings (`"27.00"`), never numbers: rule 11.

Placing a call (`POST /voice/calls`) on a metered workspace that cannot cover a
minute returns `402 wallet_insufficient_balance` with `balance`, `required` and
`currency`. Nothing is written and nothing is sent to Bolna.

## 5. Where it lives

| Piece | File |
|---|---|
| Models | `api/app/models/billing.py` |
| Rules (gate, settlement, operator entries, usage) | `api/app/services/wallet.py` |
| Hooks into the call flow | `api/app/services/voice_calls.py` — `trigger`, `handle_execution`, `_upgrade` |
| Read API | `api/app/routers/wallet.py`, `api/app/schemas/wallet.py` |
| Operator CLI | `api/app/wallet/__main__.py` |
| Screen | `web/src/routes/WalletPage.tsx` (Settings → Platform → Wallet) |
| Tests | `api/tests/test_wallet.py` |

## 6. Not built

- Online payment (e.g. Razorpay). Recharges are recorded by the operator after
  the client pays.
- Per-language rates. Every call is priced at the wallet's single rate; the
  current agent is English-only.
- Reserving balance at dial time. With calls placed one at a time this does not
  matter; concurrent batch calling would let several calls pass the gate on the
  same minute of credit, each then finishing slightly negative.
- An operator screen. The command line is the operator's interface for now.
