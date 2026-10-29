/**
 * The AI-call wallet (docs/14). Read-only: a member can see the balance, the
 * minutes used and the statement, but nothing on this side moves money. Top-ups
 * and discounts are recorded by the operator, outside the app.
 *
 * Amounts arrive as strings ("120.00") and are shown as strings. Money is never
 * turned into a float, not even for display.
 */

import { useQuery } from '@tanstack/react-query'

import { api } from '@/api/client'
import type { Page } from '@/api/types'

export interface WalletUsage {
  readonly minutes: number
  readonly connected_calls: number
  readonly spent: string
}

export interface Wallet {
  readonly currency: string
  readonly balance: string
  /** Per started minute of connected call time, before GST. */
  readonly rate_per_minute: string
  readonly can_call: boolean
  /** One minute's price: what the balance must cover before a call can dial. */
  readonly minimum_to_call: string
  readonly today: WalletUsage
  readonly this_month: WalletUsage
  readonly updated_at: string
}

export type WalletTransactionKind = 'TOPUP' | 'DISCOUNT' | 'ADJUSTMENT' | 'CALL_CHARGE'

export interface WalletTransaction {
  readonly id: string
  readonly kind: WalletTransactionKind
  /** Signed: credits positive, charges negative. */
  readonly amount: string
  readonly currency: string
  readonly balance_after: string
  readonly voice_call_id: string | null
  readonly billed_seconds: number | null
  readonly billed_minutes: number | null
  readonly rate_per_minute: string | null
  readonly note: string | null
  readonly created_at: string
}

const base = (workspaceId: string) => `/workspaces/${workspaceId}/wallet`

export const walletKey = (workspaceId: string) => ['wallet', workspaceId] as const

export function useWallet(
  workspaceId: string,
  options: { readonly refetchInterval?: number } = {},
) {
  return useQuery({
    queryKey: walletKey(workspaceId),
    refetchInterval: options.refetchInterval,
    queryFn: () => api.get<Wallet>(base(workspaceId)),
    // A 404 (not metered) or 403 (no billing permission) is an answer, not a
    // hiccup worth retrying.
    retry: false,
  })
}

export function useWalletTransactions(
  workspaceId: string,
  options: { readonly limit: number; readonly offset: number; readonly enabled?: boolean },
) {
  const { limit, offset, enabled = true } = options
  return useQuery({
    queryKey: [...walletKey(workspaceId), 'transactions', { limit, offset }] as const,
    enabled,
    queryFn: () =>
      api.get<Page<WalletTransaction>>(`${base(workspaceId)}/transactions`, {
        query: { limit, offset },
      }),
    retry: false,
  })
}

/** `INR 120.00`, currency first — the app's money convention (lib/format). */
export function formatMoney(amount: string, currency: string): string {
  return `${currency} ${amount}`
}

/** `+ INR 1000.00` / `− INR 27.00` for a statement line. */
export function formatSignedMoney(amount: string, currency: string): string {
  const negative = amount.startsWith('-')
  const magnitude = negative ? amount.slice(1) : amount
  return `${negative ? '−' : '+'} ${currency} ${magnitude}`
}

export function isNegative(amount: string): boolean {
  return amount.startsWith('-')
}
