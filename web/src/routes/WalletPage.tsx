/**
 * Wallet — the workspace's prepaid AI-call balance (docs/14).
 *
 * What a client needs to know about their spend, in the order they ask it:
 * how much is left, can we still call, what does a minute cost, how much have
 * we used — and then the statement that explains every change.
 *
 * Read-only. There is no recharge button because there is no recharge
 * endpoint: money is added by the operator when the client pays, so this page
 * tells the client to ask for a recharge rather than pretending to take one.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowDownLeft, ArrowUpRight, Bot, Wallet as WalletIcon } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { cn } from '@/lib/utils'
import { useAuth } from '@/features/auth/context'
import { formatCallMoment } from '@/features/calls/format'
import {
  formatMoney,
  formatSignedMoney,
  isNegative,
  useWallet,
  useWalletTransactions,
  type Wallet,
  type WalletTransaction,
  type WalletTransactionKind,
  type WalletUsage,
} from '@/features/wallet/api'

const PAGE_SIZE = 20

const KIND_LABELS: Record<WalletTransactionKind, string> = {
  TOPUP: 'Recharge',
  DISCOUNT: 'Discount',
  ADJUSTMENT: 'Adjustment',
  CALL_CHARGE: 'AI call',
  NUMBER_RENTAL: 'Calling number',
}

function minutes(value: number): string {
  return `${value} ${value === 1 ? 'minute' : 'minutes'}`
}

function UsageCard({
  title,
  usage,
  currency,
}: {
  readonly title: string
  readonly usage: WalletUsage
  readonly currency: string
}) {
  return (
    <Card>
      <CardHeader>
        <CardDescription>{title}</CardDescription>
        <CardTitle className="text-2xl tabular-nums">{minutes(usage.minutes)}</CardTitle>
      </CardHeader>
      <CardContent className="text-muted-foreground text-sm">
        {usage.connected_calls} connected {usage.connected_calls === 1 ? 'call' : 'calls'} ·{' '}
        {formatMoney(usage.spent, currency)}
      </CardContent>
    </Card>
  )
}

function Summary({ wallet }: { readonly wallet: Wallet }) {
  const negative = isNegative(wallet.balance)
  return (
    <div className="space-y-4">
      {!wallet.can_call ? (
        <div
          role="alert"
          className="border-destructive/30 bg-destructive/8 text-destructive flex items-start gap-3 rounded-2xl border p-4 text-sm"
          data-testid="wallet-blocked"
        >
          <AlertTriangle aria-hidden="true" className="mt-0.5 size-4 shrink-0" />
          <p>
            AI calls are paused. A call needs at least{' '}
            {formatMoney(wallet.minimum_to_call, wallet.currency)} in the wallet. Ask your account
            manager to recharge it, and calling resumes straight away.
          </p>
        </div>
      ) : null}

      <div className="grid gap-4 md:grid-cols-4">
        <Card className="md:col-span-2">
          <CardHeader>
            <CardDescription className="flex items-center gap-2">
              <WalletIcon aria-hidden="true" className="size-4" /> Balance
            </CardDescription>
            <CardTitle
              className={cn('text-3xl tabular-nums', negative && 'text-destructive')}
              data-testid="wallet-balance"
            >
              {formatMoney(wallet.balance, wallet.currency)}
            </CardTitle>
          </CardHeader>
          <CardContent className="text-muted-foreground text-sm">
            {formatMoney(wallet.rate_per_minute, wallet.currency)} per minute of connected call,
            before GST (GST is on your recharge invoice). Every started minute counts as a full
            minute; unanswered calls are free.
          </CardContent>
        </Card>
        <UsageCard title="Today" usage={wallet.today} currency={wallet.currency} />
        <UsageCard title="This month" usage={wallet.this_month} currency={wallet.currency} />
      </div>
    </div>
  )
}

function StatementRow({ row }: { readonly row: WalletTransaction }) {
  const debit = isNegative(row.amount)
  const Icon = row.kind === 'CALL_CHARGE' ? Bot : debit ? ArrowUpRight : ArrowDownLeft
  return (
    <li
      className="border-border/60 flex flex-wrap items-center justify-between gap-3 border-b py-3 last:border-b-0"
      data-testid="wallet-line"
    >
      <div className="flex min-w-0 items-center gap-3">
        <span
          aria-hidden="true"
          className={cn(
            'flex size-9 shrink-0 items-center justify-center rounded-xl',
            debit ? 'bg-secondary text-secondary-foreground' : 'bg-success/14 text-success',
          )}
        >
          <Icon className="size-4" />
        </span>
        <div className="min-w-0">
          <p className="flex items-center gap-2 text-sm font-medium">
            {KIND_LABELS[row.kind]}
            {row.kind === 'CALL_CHARGE' && row.billed_minutes !== null ? (
              <Badge variant="outline">{minutes(row.billed_minutes)}</Badge>
            ) : null}
          </p>
          <p className="text-muted-foreground truncate text-xs">
            {formatCallMoment(row.created_at)}
            {row.kind === 'CALL_CHARGE' && row.voice_call_id ? (
              <>
                {' · '}
                <Link
                  className="underline-offset-2 hover:underline"
                  to={`/calls/${row.voice_call_id}`}
                >
                  View call
                </Link>
              </>
            ) : row.note ? (
              ` · ${row.note}`
            ) : null}
          </p>
        </div>
      </div>
      <div className="text-right tabular-nums">
        <p className={cn('text-sm font-medium', debit ? 'text-foreground' : 'text-success')}>
          {formatSignedMoney(row.amount, row.currency)}
        </p>
        <p className="text-muted-foreground text-xs">
          Balance {formatMoney(row.balance_after, row.currency)}
        </p>
      </div>
    </li>
  )
}

function Statement({ workspaceId }: { readonly workspaceId: string }) {
  const [offset, setOffset] = useState(0)
  const page = useWalletTransactions(workspaceId, { limit: PAGE_SIZE, offset })
  const total = page.data?.total ?? 0

  return (
    <Card>
      <CardHeader>
        <CardTitle>Statement</CardTitle>
        <CardDescription>Every recharge, discount and call charge, newest first.</CardDescription>
      </CardHeader>
      <CardContent>
        {page.isPending ? (
          <p className="text-muted-foreground text-sm">Loading the statement…</p>
        ) : page.isError ? (
          <p className="text-muted-foreground text-sm">The statement could not be loaded.</p>
        ) : page.data.items.length === 0 ? (
          <p className="text-muted-foreground text-sm">Nothing on the statement yet.</p>
        ) : (
          <>
            <ol data-testid="wallet-statement">
              {page.data.items.map((row) => (
                <StatementRow key={row.id} row={row} />
              ))}
            </ol>
            {total > PAGE_SIZE ? (
              <div className="mt-4 flex items-center justify-between text-sm">
                <span className="text-muted-foreground">
                  {offset + 1}–{Math.min(offset + PAGE_SIZE, total)} of {total}
                </span>
                <div className="flex gap-2">
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset === 0}
                    onClick={() => setOffset(Math.max(0, offset - PAGE_SIZE))}
                  >
                    Newer
                  </Button>
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={offset + PAGE_SIZE >= total}
                    onClick={() => setOffset(offset + PAGE_SIZE)}
                  >
                    Older
                  </Button>
                </div>
              </div>
            ) : null}
          </>
        )}
      </CardContent>
    </Card>
  )
}

export function WalletPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const wallet = useWallet(workspaceId)
  const code = wallet.error instanceof ApiError ? wallet.error.code : null

  return (
    <div className="space-y-5">
      <PageHeader
        title="Wallet"
        description="Prepaid AI-call minutes: what is left, what has been used, and every charge."
      />

      {wallet.isPending ? (
        <p className="text-muted-foreground text-sm">Loading the wallet…</p>
      ) : code === 'wallet_not_configured' ? (
        <Card>
          <CardContent className="text-muted-foreground py-6 text-sm">
            Billing is not set up for this workspace, so AI calls here are not metered.
          </CardContent>
        </Card>
      ) : code === 'insufficient_permissions' ? (
        <Card>
          <CardContent className="text-muted-foreground py-6 text-sm">
            Your permission template does not include billing. Ask a workspace owner for access.
          </CardContent>
        </Card>
      ) : wallet.isError ? (
        <p className="text-muted-foreground text-sm">The wallet could not be loaded.</p>
      ) : (
        <>
          <Summary wallet={wallet.data} />
          <Statement workspaceId={workspaceId} />
        </>
      )}
    </div>
  )
}
