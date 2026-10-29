/**
 * The wallet balance in the header of every page (docs/14).
 *
 * Shows the balance at a glance. Hover or keyboard focus opens a small card
 * with the rate and the minutes used; a click opens the Wallet page. A
 * workspace that is not metered, or a member without billing permission,
 * gets nothing here — the wallet query answers 404/403 and the badge stays
 * out of the way.
 */

import { Link } from 'react-router-dom'
import { AlertTriangle, ArrowRight, Wallet as WalletIcon } from 'lucide-react'

import { cn } from '@/lib/utils'
import { formatMoney, isNegative, useWallet, type WalletUsage } from '@/features/wallet/api'

/** Calls charge the wallet as they finish; a minute is fresh enough for a header. */
const REFRESH_MS = 60_000

function usageLine(label: string, usage: WalletUsage, currency: string) {
  return (
    <div className="flex items-baseline justify-between gap-4">
      <span className="text-muted-foreground">{label}</span>
      <span className="tabular-nums">
        {usage.minutes} min · {formatMoney(usage.spent, currency)}
      </span>
    </div>
  )
}

export function WalletBadge({ workspaceId }: { readonly workspaceId: string }) {
  const wallet = useWallet(workspaceId, { refetchInterval: REFRESH_MS })
  if (!wallet.data) return null
  const w = wallet.data
  const blocked = !w.can_call

  return (
    <div className="group relative" data-testid="wallet-badge">
      <Link
        to="/wallet"
        aria-label={`Wallet balance ${formatMoney(w.balance, w.currency)}. Open the wallet.`}
        className={cn(
          'border-border/70 hover:bg-accent focus-visible:ring-ring/40 flex h-8 items-center gap-1.5 rounded-full border px-3 text-sm tabular-nums transition-colors outline-none focus-visible:ring-[3px]',
          blocked && 'border-destructive/40 text-destructive',
        )}
      >
        {blocked ? (
          <AlertTriangle aria-hidden="true" className="size-4" />
        ) : (
          <WalletIcon aria-hidden="true" className="size-4" />
        )}
        <span className={cn(isNegative(w.balance) && 'text-destructive')}>
          {formatMoney(w.balance, w.currency)}
        </span>
      </Link>

      {/* Padding, not margin, between badge and card: the pointer crosses it
          without leaving the group, so the card stays open on the way down. */}
      <div
        role="tooltip"
        className={cn(
          'invisible absolute top-full right-0 z-50 pt-2 opacity-0 transition-opacity',
          'group-focus-within:visible group-focus-within:opacity-100 group-hover:visible group-hover:opacity-100',
        )}
      >
        <div className="bg-popover text-popover-foreground border-border/70 w-72 rounded-2xl border p-4 text-sm shadow-lg">
          <p className="text-muted-foreground text-xs">AI-call wallet</p>
          <p
            className={cn(
              'mt-0.5 text-2xl font-semibold tabular-nums',
              isNegative(w.balance) && 'text-destructive',
            )}
          >
            {formatMoney(w.balance, w.currency)}
          </p>
          {blocked ? (
            <p className="text-destructive mt-1 text-xs">
              AI calls are paused until the wallet is recharged.
            </p>
          ) : null}
          <div className="mt-3 space-y-1.5">
            <div className="flex items-baseline justify-between gap-4">
              <span className="text-muted-foreground">Rate</span>
              <span className="tabular-nums">
                {formatMoney(w.rate_per_minute, w.currency)}/min + GST
              </span>
            </div>
            {usageLine('Today', w.today, w.currency)}
            {usageLine('This month', w.this_month, w.currency)}
          </div>
          <Link
            to="/wallet"
            className="text-primary mt-3 inline-flex items-center gap-1 text-xs font-medium hover:underline"
          >
            Statement and details <ArrowRight aria-hidden="true" className="size-3.5" />
          </Link>
        </div>
      </div>
    </div>
  )
}
