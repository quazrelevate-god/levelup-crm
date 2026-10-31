/**
 * One company in the operator console (docs/19): who they are, what they
 * bought, their people as a tree, their money, and everything done to them.
 * "Open their CRM" takes the operator inside as Root to help, holding no seat.
 */

import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { ArrowLeft, LogIn, LogOut } from 'lucide-react'

import { ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useAuth } from '@/features/auth/context'
import { useCompany, useSupport } from '@/features/operator/api'
import { HistoryList } from '@/features/operator/HistoryList'
import { PeoplePanel } from '@/features/operator/PeoplePanel'
import { PlanPanel } from '@/features/operator/PlanPanel'
import { money } from '@/features/operator/roleInfo'
import { WalletPanel } from '@/features/operator/WalletPanel'
import { cn } from '@/lib/utils'
import { SeatBar } from '@/routes/operator/OperatorHomePage'

const TABS = [
  { key: 'people', label: 'People' },
  { key: 'plan', label: 'Plan & features' },
  { key: 'wallet', label: 'Wallet' },
  { key: 'history', label: 'History' },
] as const

type Tab = (typeof TABS)[number]['key']

export function CompanyPage() {
  const { companyId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const tab: Tab = TABS.find((t) => t.key === params.get('tab'))?.key ?? 'people'
  const company = useCompany(companyId)
  const support = useSupport(companyId)
  const { reload, selectWorkspace } = useAuth()
  const navigate = useNavigate()

  if (company.isPending) return <p className="text-muted-foreground text-sm">Loading…</p>
  if (company.isError) {
    return (
      <p className="text-muted-foreground text-sm">
        This company could not be loaded. <Link to="/operator">Back</Link>
      </p>
    )
  }
  const c = company.data

  const goInside = () =>
    support.mutate(true, {
      onSuccess: () => {
        void reload().then(() => {
          selectWorkspace(c.id)
          void navigate('/dashboard')
        })
      },
    })

  return (
    <>
      <Link to="/operator" className="text-muted-foreground flex items-center gap-1 text-sm">
        <ArrowLeft aria-hidden="true" className="size-4" /> Companies
      </Link>

      <Card>
        <CardContent className="flex flex-wrap items-center gap-x-8 gap-y-4 pt-5">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <h1 className="text-2xl font-semibold tracking-tight">{c.name}</h1>
              {c.is_active ? (
                <Badge variant="success">Active</Badge>
              ) : (
                <Badge variant="destructive">Suspended</Badge>
              )}
            </div>
            <p className="text-muted-foreground text-sm">
              Owner {c.owner_name ?? '—'} · {c.owner_email}
            </p>
          </div>
          <Fact label="Seats">
            <SeatBar used={c.seats_used} limit={c.seat_limit} />
          </Fact>
          <Fact label="Wallet">
            <span
              className={cn('font-semibold', c.low_balance && 'text-amber-600 dark:text-amber-400')}
            >
              {money(c.balance, c.currency)}
            </span>
          </Fact>
          <Fact label="AI minutes this month">
            <span className="font-semibold">{c.minutes_this_month}</span>
          </Fact>
          <div className="ml-auto flex flex-wrap gap-2">
            {c.support_active ? (
              <>
                <Button onClick={goInside} disabled={support.isPending}>
                  <LogIn aria-hidden="true" /> Go to their CRM
                </Button>
                <Button
                  variant="outline"
                  disabled={support.isPending}
                  onClick={() => support.mutate(false)}
                >
                  <LogOut aria-hidden="true" /> End my access
                </Button>
              </>
            ) : (
              <Button onClick={goInside} disabled={support.isPending || !c.is_active}>
                <LogIn aria-hidden="true" /> Open their CRM to help
              </Button>
            )}
          </div>
          {support.error ? (
            <p role="alert" className="text-destructive w-full text-sm">
              {support.error instanceof ApiError ? support.error.message : 'That did not work.'}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <div className="border-border flex gap-1 border-b" role="tablist">
        {TABS.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setParams({ tab: t.key })}
            className={cn(
              '-mb-px border-b-2 px-4 py-2 text-sm font-medium',
              tab === t.key
                ? 'border-primary text-foreground'
                : 'text-muted-foreground border-transparent',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === 'people' ? <PeoplePanel company={c} /> : null}
      {tab === 'plan' ? <PlanPanel key={`${c.name}-${c.seat_limit}`} company={c} /> : null}
      {tab === 'wallet' ? <WalletPanel company={c} /> : null}
      {tab === 'history' ? <HistoryList workspaceId={c.id} /> : null}
    </>
  )
}

function Fact({ label, children }: { readonly label: string; readonly children: React.ReactNode }) {
  return (
    <div>
      <p className="text-muted-foreground text-xs">{label}</p>
      {children}
    </div>
  )
}
