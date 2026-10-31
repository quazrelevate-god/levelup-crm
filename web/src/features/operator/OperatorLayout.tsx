/**
 * The operator console's frame (docs/19). Deliberately unlike a company's CRM —
 * a dark band across the top saying "Operator console" — so the operator always
 * knows they are above every company, not inside one.
 */

import { Link, Navigate, NavLink, Outlet } from 'react-router-dom'
import { Building2, Crown, History, KeyRound, LogOut, PhoneCall } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/context'
import { ThemeToggle } from '@/features/theme/ThemeToggle'
import { cn } from '@/lib/utils'

const TABS = [
  { to: '/operator', label: 'Companies', icon: Building2, end: true },
  { to: '/operator/numbers', label: 'Number requests', icon: PhoneCall, end: false },
  { to: '/operator/history', label: 'History', icon: History, end: false },
  { to: '/operator/logins', label: 'Operator logins', icon: KeyRound, end: false },
] as const

export function OperatorLayout() {
  const { status, isOperator, user, memberships, logout } = useAuth()

  if (status === 'loading') {
    return (
      <div className="text-muted-foreground flex min-h-screen items-center justify-center text-sm">
        Loading…
      </div>
    )
  }
  if (status === 'anonymous') return <Navigate to="/login" replace />
  if (!isOperator) return <Navigate to="/" replace />

  const ownCompanies = memberships.filter((m) => m.is_active && m.has_license && !m.is_support)

  return (
    <div className="min-h-screen">
      <header className="bg-slate-900 text-white">
        <div className="mx-auto flex max-w-7xl flex-wrap items-center gap-3 px-4 py-3 sm:px-6">
          <span className="flex items-center gap-2">
            <span className="rounded-lg bg-amber-400 p-1.5 text-slate-900">
              <Crown aria-hidden="true" className="size-4" />
            </span>
            <span className="leading-tight">
              <span className="block text-sm font-semibold">Operator console</span>
              <span className="block text-[11px] text-slate-300">
                Above every company · {user?.email}
              </span>
            </span>
          </span>
          <div className="ml-auto flex items-center gap-2">
            {ownCompanies.length > 0 ? (
              <Button asChild size="sm" variant="secondary">
                <Link to="/workspaces">Open a company CRM</Link>
              </Button>
            ) : null}
            <ThemeToggle />
            <Button
              size="sm"
              variant="ghost"
              className="text-white hover:text-white"
              onClick={() => void logout()}
            >
              <LogOut aria-hidden="true" /> Sign out
            </Button>
          </div>
        </div>
        <nav
          className="mx-auto flex max-w-7xl gap-1 overflow-x-auto px-4 sm:px-6"
          aria-label="Console"
        >
          {TABS.map((tab) => {
            const Icon = tab.icon
            return (
              <NavLink
                key={tab.to}
                to={tab.to}
                end={tab.end}
                className={({ isActive }) =>
                  cn(
                    'flex items-center gap-1.5 rounded-t-lg px-3 py-2 text-sm whitespace-nowrap',
                    isActive
                      ? 'bg-background text-foreground font-medium'
                      : 'text-slate-300 hover:text-white',
                  )
                }
              >
                <Icon aria-hidden="true" className="size-4" />
                {tab.label}
              </NavLink>
            )
          })}
        </nav>
      </header>
      <main className="mx-auto max-w-7xl space-y-5 px-4 py-6 sm:px-6">
        <Outlet />
      </main>
    </div>
  )
}
