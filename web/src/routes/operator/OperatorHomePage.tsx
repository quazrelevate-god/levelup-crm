/**
 * Operator console home (docs/19): the whole business at a glance, then every
 * company in one ledger. A company that is low on money or suspended stands
 * out; a click opens it.
 */

import { useMemo, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { AlertTriangle, Building2, PhoneCall, Plus, Search, Users, Wallet } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { useCompanies, useOverview } from '@/features/operator/api'
import { money } from '@/features/operator/roleInfo'
import { HierarchyStrip } from '@/features/operator/ui'
import { cn } from '@/lib/utils'

export function OperatorHomePage() {
  const overview = useOverview()
  const companies = useCompanies()
  const navigate = useNavigate()
  const [search, setSearch] = useState('')

  const rows = useMemo(() => {
    const needle = search.trim().toLowerCase()
    const list = companies.data ?? []
    if (!needle) return list
    return list.filter((c) =>
      [c.name, c.owner_name ?? '', c.owner_email ?? ''].some((v) =>
        v.toLowerCase().includes(needle),
      ),
    )
  }, [companies.data, search])

  const totals = overview.data
    ? Object.entries(overview.data.wallet_totals)
        .map(([currency, amount]) => money(amount, currency))
        .join(' + ') || '₹0'
    : '…'

  return (
    <>
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="space-y-2">
          <h1 className="text-2xl font-semibold tracking-tight">Companies</h1>
          <HierarchyStrip highlight="Company" />
        </div>
        <Button size="lg" onClick={() => void navigate('/operator/companies/new')}>
          <Plus aria-hidden="true" /> New company
        </Button>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
        <Stat
          icon={Building2}
          label="Companies"
          value={overview.data ? `${overview.data.active_companies} active` : '…'}
          sub={overview.data ? `${overview.data.companies} in total` : undefined}
        />
        <Stat
          icon={Users}
          label="People with a seat"
          value={overview.data ? String(overview.data.licensed_users) : '…'}
        />
        <Stat icon={Wallet} label="Money in wallets" value={totals} />
        <Stat
          icon={PhoneCall}
          label="AI calls, last 24 hours"
          value={overview.data ? String(overview.data.ai_calls_24h) : '…'}
        />
      </div>

      {overview.data && overview.data.low_balance.length > 0 ? (
        <Card className="border-amber-500/50">
          <CardContent className="space-y-2 pt-5">
            <p className="flex items-center gap-2 text-sm font-semibold text-amber-700 dark:text-amber-400">
              <AlertTriangle aria-hidden="true" className="size-4" />
              Low balance — below {money(overview.data.low_balance_at, 'INR')}. Their AI calls stop
              when it reaches zero.
            </p>
            <div className="flex flex-wrap gap-2">
              {overview.data.low_balance.map((c) => (
                <Link
                  key={c.id}
                  to={`/operator/companies/${c.id}?tab=wallet`}
                  className="hover:bg-muted rounded-lg border px-3 py-1.5 text-sm"
                >
                  {c.name} · <span className="font-semibold">{money(c.balance, c.currency)}</span>
                </Link>
              ))}
            </div>
          </CardContent>
        </Card>
      ) : null}

      {overview.data && overview.data.pending_number_requests > 0 ? (
        <Link
          to="/operator/numbers"
          className="bg-primary/5 hover:bg-primary/10 block rounded-xl border px-4 py-3 text-sm"
        >
          {overview.data.pending_number_requests} calling-number request
          {overview.data.pending_number_requests > 1 ? 's are' : ' is'} waiting for you →
        </Link>
      ) : null}

      <Card>
        <CardContent className="space-y-3 pt-5">
          <div className="flex flex-wrap items-center gap-2">
            <p className="font-semibold">Ledger</p>
            <div className="relative ml-auto w-full max-w-xs">
              <Search
                aria-hidden="true"
                className="text-muted-foreground absolute top-1/2 left-3 size-4 -translate-y-1/2"
              />
              <Input
                aria-label="Search companies"
                placeholder="Search company or owner"
                className="pl-9"
                value={search}
                onChange={(event) => setSearch(event.target.value)}
              />
            </div>
          </div>
          {companies.isPending ? (
            <p className="text-muted-foreground text-sm">Loading…</p>
          ) : companies.isError ? (
            <p className="text-muted-foreground text-sm">Companies could not be loaded.</p>
          ) : (
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-muted-foreground border-b text-left text-xs">
                  <tr>
                    <th className="py-2 pr-3 font-medium">Company</th>
                    <th className="py-2 pr-3 font-medium">Owner (Root)</th>
                    <th className="py-2 pr-3 font-medium">Status</th>
                    <th className="py-2 pr-3 font-medium">Seats</th>
                    <th className="py-2 pr-3 text-right font-medium">Wallet</th>
                    <th className="py-2 pr-3 text-right font-medium">AI minutes this month</th>
                    <th className="py-2 font-medium">Since</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((c) => (
                    <tr
                      key={c.id}
                      onClick={() => void navigate(`/operator/companies/${c.id}`)}
                      className="border-border/60 hover:bg-muted/40 cursor-pointer border-b last:border-b-0"
                    >
                      <td className="py-3 pr-3">
                        <span className="font-medium">{c.name}</span>
                        {c.support_active ? (
                          <Badge variant="outline" className="ml-2">
                            You are inside
                          </Badge>
                        ) : null}
                      </td>
                      <td className="py-3 pr-3">
                        <span className="block">{c.owner_name ?? '—'}</span>
                        <span className="text-muted-foreground text-xs">{c.owner_email}</span>
                      </td>
                      <td className="py-3 pr-3">
                        {c.is_active ? (
                          <Badge variant="success">Active</Badge>
                        ) : (
                          <Badge variant="destructive">Suspended</Badge>
                        )}
                      </td>
                      <td className="py-3 pr-3">
                        <SeatBar used={c.seats_used} limit={c.seat_limit} />
                      </td>
                      <td
                        className={cn(
                          'py-3 pr-3 text-right font-medium tabular-nums',
                          c.low_balance && 'text-amber-600 dark:text-amber-400',
                        )}
                      >
                        {money(c.balance, c.currency)}
                      </td>
                      <td className="py-3 pr-3 text-right tabular-nums">{c.minutes_this_month}</td>
                      <td className="text-muted-foreground py-3">
                        {new Date(c.created_at).toLocaleDateString()}
                      </td>
                    </tr>
                  ))}
                  {rows.length === 0 ? (
                    <tr>
                      <td colSpan={7} className="text-muted-foreground py-6 text-center">
                        {search ? 'No company matches.' : 'No companies yet.'}
                      </td>
                    </tr>
                  ) : null}
                </tbody>
              </table>
            </div>
          )}
        </CardContent>
      </Card>
    </>
  )
}

function Stat({
  icon: Icon,
  label,
  value,
  sub,
}: {
  readonly icon: typeof Building2
  readonly label: string
  readonly value: string
  readonly sub?: string
}) {
  return (
    <Card>
      <CardContent className="flex items-start gap-3 pt-5">
        <span className="bg-primary/10 text-primary rounded-lg p-2">
          <Icon aria-hidden="true" className="size-4" />
        </span>
        <span>
          <span className="text-muted-foreground block text-xs">{label}</span>
          <span className="block text-xl font-semibold">{value}</span>
          {sub ? <span className="text-muted-foreground block text-xs">{sub}</span> : null}
        </span>
      </CardContent>
    </Card>
  )
}

export function SeatBar({ used, limit }: { readonly used: number; readonly limit: number }) {
  const share = limit > 0 ? Math.min(1, used / limit) : 0
  return (
    <span className="flex min-w-24 items-center gap-2">
      <span className="bg-muted h-1.5 w-16 overflow-hidden rounded-full">
        <span
          className={cn('block h-full rounded-full', share >= 1 ? 'bg-amber-500' : 'bg-primary')}
          style={{ width: `${share * 100}%` }}
        />
      </span>
      <span className="tabular-nums">
        {used}/{limit}
      </span>
    </span>
  )
}
