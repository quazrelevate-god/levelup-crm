# 19 — Operator console

One place for the platform operator (whoever sells this CRM) to run every
client company: create them, decide seats and features, put money in wallets,
add people at any role, suspend, and step inside to help. Requires migration
**`0024_operator_console`**.

Who is an operator is decided only by `PLATFORM_OPERATOR_EMAILS` on the server
(docs/16). Nothing inside the app can grant it.

## Getting in

- An operator who signs in lands on **`/operator`**, not inside a company
  (`TokenResponse.is_platform_operator`).
- From a company's CRM, the header has an **Operator console** button.
- **Operator logins** (console tab) lists the operator addresses and gives a
  one-time "set your password" link for each — how a new operator address gets
  its first password, since nobody types passwords for anyone.

## Screens

| Tab / page | What it does |
|---|---|
| **Companies** | Overview cards (companies, people with a seat, money in wallets, AI calls 24h), the **low-balance** list (below ₹500, `OPERATOR_LOW_BALANCE`), pending number requests, and the **ledger**: company, owner (Root), status, seats used/limit, wallet, AI minutes this month. |
| **New company** | 5 steps: company (name, country code, timezone, currency) → seats & features → wallet (opening balance, price per AI minute) → owner (Root) → check & create. Ends with the owner's set-password link (copy or WhatsApp). The operator is **not** made a member. |
| **Company → People** | The org tree by reporting line, role colours, the hierarchy strip (Operator → Company → Root → Admin → Manager → Caller with counts). **Add person**: choose role (cards say what each can do) → reports to (only people ranked above) → name & email → link. **Edit**: role, reports to, switch off (asks who takes their open leads). **Sign-in link** for anyone locked out. |
| **Company → Plan & features** | Name, seats (never below those in use), feature switches, **Suspend / Reactivate** (with confirmation). |
| **Company → Wallet** | Balance, price per minute, **Add money / Free credit / Correct** — each typed, then confirmed against the new balance. Full money history. Nothing is ever deleted; corrections are entries. |
| **Company → History** | Everything done to this company from the console. |
| **Open their CRM to help** | The operator enters the company as **Root**, marked `is_support`: holds no seat, is never routed a lead, and a yellow banner says so on every page. **End my access** removes it. |
| **Number requests** | Calling-number requests from every company: give the number, or cancel (refunds the month). |
| **History** | Every console action across all companies: who, what, when. |

## Rules the server enforces

- Seats: adding a person needs a free seat; seats cannot drop below those in use.
  Support access never counts.
- Money: top-up and free credit must be positive; a correction needs a note;
  all three are `WalletTransaction` rows recorded with the operator's email.
- Every change writes an `operator_actions` row (global table; `workspace_id`
  cascades with the workspace, which is never hard-deleted).
- A suspended company: its people cannot sign in, the Razorpay webhook stops,
  nothing is deleted.

## API

All under `/api/v1/operator`, operator only:
`GET overview`, `GET features`, `GET|POST companies`, `GET|PATCH companies/{id}`,
`GET companies/{id}/roles`, `GET|POST companies/{id}/people`,
`PATCH companies/{id}/people/{mid}`, `POST companies/{id}/people/{mid}/sign-in-link`,
`GET companies/{id}/wallet`, `POST companies/{id}/wallet/entries`,
`PUT companies/{id}/wallet/rate`, `POST|DELETE companies/{id}/support`,
`GET number-requests`, `POST companies/{id}/number-requests/{rid}`,
`GET history`, `GET operators`, `POST operators/sign-in-link`.
