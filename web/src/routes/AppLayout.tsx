import { useEffect, useState } from 'react'
import { Link, Navigate, Outlet, useLocation } from 'react-router-dom'
import { Menu, Settings2, X } from 'lucide-react'

import { MarbiMark, MarbiWordmark } from '@/components/brand/MarbiLogo'
import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/context'
import { ThemeToggle } from '@/features/theme/ThemeToggle'
import { WalletBadge } from '@/features/wallet/WalletBadge'
import {
  AUTOMATE_NAV,
  PRIMARY_NAV,
  WORKSPACE_NAV,
  isActive,
  type NavLink as NavLinkSpec,
} from '@/routes/nav'
import { cn } from '@/lib/utils'

/**
 * Collapsed width is a square around the icon; expanded is wide enough for the
 * longest label ("Voice extraction") without wrapping.
 */
const RAIL_COLLAPSED = 'w-[68px]'
const RAIL_EXPANDED = 'hover:w-[232px] focus-within:w-[232px]'

function NavItem({
  link,
  active,
  onNavigate,
}: {
  readonly link: NavLinkSpec
  readonly active: boolean
  readonly onNavigate: () => void
}) {
  const Icon = link.icon
  return (
    <Link
      to={link.to}
      onClick={onNavigate}
      // `page` rather than `true`: this link *is* the current page, which is
      // what a screen reader should say, and it is also the styling hook.
      aria-current={active ? 'page' : undefined}
      title={link.label}
      className={cn(
        'state-layer relative flex h-10 items-center gap-3 rounded-xl px-[22px] text-sm font-medium',
        'focus-visible:ring-ring/45 transition-colors duration-150 outline-none focus-visible:ring-[3px]',
        active ? 'bg-primary/10 text-foreground' : 'text-muted-foreground hover:text-foreground',
      )}
    >
      <Icon aria-hidden="true" className="size-[18px] shrink-0" />
      {/*
       * Faded rather than unmounted. The label is this link's accessible name,
       * so removing it when the rail is collapsed would leave twelve anonymous
       * icons — and `opacity` keeps it in the accessibility tree.
       */}
      <span className="truncate opacity-0 transition-opacity duration-200 group-focus-within/rail:opacity-100 group-hover/rail:opacity-100">
        {link.label}
      </span>
    </Link>
  )
}

export function AppLayout() {
  const { status, user, activeMembership, activeWorkspaceId, memberships, logout } = useAuth()
  const location = useLocation()
  const [drawerOpen, setDrawerOpen] = useState(false)

  useEffect(() => {
    setDrawerOpen(false)
  }, [location.pathname])

  if (status === 'loading') {
    return (
      <div className="text-muted-foreground flex min-h-screen items-center justify-center text-sm">
        Loading…
      </div>
    )
  }
  if (status === 'anonymous') {
    return <Navigate to="/login" replace state={{ from: location.pathname }} />
  }
  if (!activeWorkspaceId || !activeMembership) {
    return <Navigate to="/workspaces" replace />
  }

  const workspaceName = activeMembership.workspace.name
  const settingsActive = isActive(location.pathname, '/settings')
  const close = () => setDrawerOpen(false)

  const railContents = (
    <div className="flex h-full flex-col overflow-hidden py-3">
      <div className="mb-2 flex h-12 items-center gap-2.5 px-[18px]">
        <MarbiMark className="size-8" />
        {/* Fades in with the nav labels as the rail widens. */}
        <MarbiWordmark className="text-[1.375rem] opacity-0 transition-opacity duration-200 group-focus-within/rail:opacity-100 group-hover/rail:opacity-100" />
      </div>

      <div className="flex flex-col gap-0.5">
        {PRIMARY_NAV.map((link) => (
          <NavItem
            key={link.to}
            link={link}
            active={isActive(location.pathname, link.to)}
            onNavigate={close}
          />
        ))}
      </div>

      <p className="text-muted-foreground/70 truncate px-[22px] pt-5 pb-1.5 text-[0.6875rem] font-semibold tracking-wider uppercase opacity-0 transition-opacity duration-200 group-focus-within/rail:opacity-100 group-hover/rail:opacity-100">
        Workspace
      </p>
      <div className="flex flex-col gap-0.5">
        {WORKSPACE_NAV.map((link) => (
          <NavItem
            key={link.to}
            link={link}
            active={isActive(location.pathname, link.to)}
            onNavigate={close}
          />
        ))}
      </div>

      <p className="text-muted-foreground/70 truncate px-[22px] pt-5 pb-1.5 text-[0.6875rem] font-semibold tracking-wider uppercase opacity-0 transition-opacity duration-200 group-focus-within/rail:opacity-100 group-hover/rail:opacity-100">
        Automate
      </p>
      <div className="flex flex-col gap-0.5">
        {AUTOMATE_NAV.map((link) => (
          <NavItem
            key={link.to}
            link={link}
            active={isActive(location.pathname, link.to)}
            onNavigate={close}
          />
        ))}
      </div>

      <div className="mt-auto pt-3">
        <Link
          to="/settings"
          onClick={close}
          aria-current={settingsActive ? 'page' : undefined}
          title="Settings"
          className={cn(
            'state-layer flex h-10 items-center gap-3 rounded-xl px-[22px] text-sm font-medium',
            'focus-visible:ring-ring/45 transition-colors duration-150 outline-none focus-visible:ring-[3px]',
            settingsActive
              ? 'bg-primary/10 text-foreground'
              : 'text-muted-foreground hover:text-foreground',
          )}
        >
          <Settings2 aria-hidden="true" className="size-[18px] shrink-0" />
          <span className="truncate opacity-0 transition-opacity duration-200 group-focus-within/rail:opacity-100 group-hover/rail:opacity-100">
            Settings
          </span>
        </Link>
      </div>
    </div>
  )

  return (
    // `h-screen` + `overflow-hidden`: the page itself never scrolls. The only
    // scroll container is the card, which is what keeps it a fixed frame with
    // even margins rather than something that drifts as content grows.
    <div className="bg-background flex h-screen overflow-hidden">
      {/*
       * The rail widens on hover and the content column, being its flex
       * sibling, is pushed right by exactly that much — so the card slides
       * aside rather than the menu overlapping it.
       */}
      <aside
        className={cn(
          'group/rail hidden shrink-0 flex-col lg:flex',
          RAIL_COLLAPSED,
          RAIL_EXPANDED,
          'transition-[width] duration-300 ease-[cubic-bezier(0.2,0,0,1)]',
        )}
      >
        {railContents}
      </aside>

      {drawerOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={close}
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
          />
          {/* Always expanded on mobile: a hover rail has nothing to hover. */}
          <aside className="bg-card group/rail shadow-e3 absolute inset-y-0 left-0 w-[250px] border-r">
            <div className="flex justify-end px-3 pt-3">
              <Button variant="ghost" size="icon" onClick={close} aria-label="Close navigation">
                <X aria-hidden="true" />
              </Button>
            </div>
            <div className="h-[calc(100%-3rem)] [&_span]:!opacity-100">{railContents}</div>
          </aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="relative z-40 flex h-14 shrink-0 items-center justify-between gap-3 px-4">
          <div className="flex min-w-0 items-center gap-2">
            <Button
              variant="ghost"
              size="icon"
              className="lg:hidden"
              onClick={() => setDrawerOpen(true)}
              aria-label="Open navigation"
            >
              <Menu aria-hidden="true" />
            </Button>
            <MarbiMark className="size-7 lg:hidden" />
            <span className="min-w-0">
              <span className="block truncate text-sm font-semibold">{workspaceName}</span>
              <span className="text-muted-foreground hidden truncate text-xs sm:block">
                {activeMembership.template_name}
              </span>
            </span>
          </div>

          <div className="flex items-center gap-1.5">
            <WalletBadge workspaceId={activeWorkspaceId} />
            <span className="text-muted-foreground hidden max-w-[24ch] truncate text-sm sm:inline">
              {user?.email}
            </span>
            <ThemeToggle />
            {memberships.length > 1 ? (
              <Button variant="outline" size="sm" asChild>
                <Link to="/workspaces">Switch workspace</Link>
              </Button>
            ) : null}
            <Button variant="ghost" size="sm" onClick={() => void logout()}>
              Sign out
            </Button>
          </div>
        </header>

        {/* The even margin on all four sides. */}
        <main className="min-h-0 flex-1 p-4">
          <div className="bg-card border-border/60 shadow-e3 h-full overflow-y-auto rounded-2xl border px-6 py-6 sm:px-8">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
