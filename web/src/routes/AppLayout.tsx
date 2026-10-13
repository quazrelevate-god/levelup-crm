import { useEffect, useState } from 'react'
import { Link, Navigate, Outlet, useLocation } from 'react-router-dom'
import { Menu, Settings2, X } from 'lucide-react'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/context'
import { ThemeToggle } from '@/features/theme/ThemeToggle'
import { PRIMARY_NAV, WORKSPACE_NAV, isActive, type NavLink as NavLinkSpec } from '@/routes/nav'
import { cn } from '@/lib/utils'

function initials(name: string): string {
  const words = name.trim().split(/\s+/).slice(0, 2)
  return words.map((word) => word[0]?.toUpperCase() ?? '').join('') || '·'
}

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
      className={cn(
        'state-layer group relative flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium',
        'transition-colors duration-150 outline-none',
        'focus-visible:ring-ring/45 focus-visible:ring-[3px]',
        active ? 'bg-primary/12 text-primary' : 'text-muted-foreground hover:text-foreground',
      )}
    >
      {/* The active marker is a bar as well as a colour — colour alone would be
          the only thing distinguishing the current page. */}
      <span
        aria-hidden="true"
        className={cn(
          'absolute left-0 h-5 w-[3px] rounded-r-full transition-all duration-200',
          active ? 'bg-primary opacity-100' : 'opacity-0',
        )}
      />
      <Icon aria-hidden="true" className="size-4.5 shrink-0" />
      <span className="truncate">{link.label}</span>
    </Link>
  )
}

function NavSection({
  heading,
  links,
  pathname,
  onNavigate,
}: {
  readonly heading?: string
  readonly links: readonly NavLinkSpec[]
  readonly pathname: string
  readonly onNavigate: () => void
}) {
  return (
    <div className="flex flex-col gap-0.5">
      {heading ? (
        <p className="text-muted-foreground/70 px-3 pt-4 pb-1.5 text-[0.6875rem] font-semibold tracking-wider uppercase">
          {heading}
        </p>
      ) : null}
      {links.map((link) => (
        <NavItem
          key={link.to}
          link={link}
          active={isActive(pathname, link.to)}
          onNavigate={onNavigate}
        />
      ))}
    </div>
  )
}

/**
 * The authenticated shell.
 *
 * Guards two things in one place: you must be signed in, and you must have
 * chosen a workspace. Every tenant route hangs off this, so no page has to
 * check either for itself.
 *
 * The sidebar replaces what used to be eighteen buttons wrapping across two
 * rows of the header. Settings is one entry rather than ten, because the ten
 * are configuration and deployment screens that a client demo should never
 * wander into by accident.
 */
export function AppLayout() {
  const { status, user, activeMembership, activeWorkspaceId, memberships, logout } = useAuth()
  const location = useLocation()
  const [drawerOpen, setDrawerOpen] = useState(false)

  // A route change with the drawer still open leaves it covering the page the
  // user just asked for.
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

  const sidebar = (
    <div className="flex h-full flex-col gap-1 overflow-y-auto px-3 pb-4">
      <div className="flex items-center gap-3 px-2 py-5">
        <span
          aria-hidden="true"
          className="bg-primary text-primary-foreground shadow-e1 flex size-9 shrink-0 items-center justify-center rounded-xl text-sm font-semibold"
        >
          {initials(workspaceName)}
        </span>
        <span className="min-w-0 flex-1">
          <span className="block truncate text-sm font-semibold">{workspaceName}</span>
          <span className="text-muted-foreground block truncate text-xs">
            {activeMembership.template_name}
          </span>
        </span>
      </div>

      <NavSection
        links={PRIMARY_NAV}
        pathname={location.pathname}
        onNavigate={() => setDrawerOpen(false)}
      />
      <NavSection
        heading="Workspace"
        links={WORKSPACE_NAV}
        pathname={location.pathname}
        onNavigate={() => setDrawerOpen(false)}
      />

      <div className="mt-auto pt-4">
        <div className="border-border/60 border-t pt-3">
          <Link
            to="/settings"
            aria-current={settingsActive ? 'page' : undefined}
            className={cn(
              'state-layer flex items-center gap-3 rounded-xl px-3 py-2 text-sm font-medium outline-none',
              'focus-visible:ring-ring/45 transition-colors duration-150 focus-visible:ring-[3px]',
              settingsActive
                ? 'bg-primary/12 text-primary'
                : 'text-muted-foreground hover:text-foreground',
            )}
          >
            <Settings2 aria-hidden="true" className="size-4.5 shrink-0" />
            Settings
          </Link>
        </div>
      </div>
    </div>
  )

  return (
    <div className="flex min-h-screen">
      {/* Desktop rail. `sticky` rather than `fixed` so the content column does
          not need a matching magic margin. */}
      <aside className="glass sticky top-0 hidden h-screen w-[248px] shrink-0 border-r lg:block">
        {sidebar}
      </aside>

      {/* Mobile drawer. */}
      {drawerOpen ? (
        <div className="fixed inset-0 z-50 lg:hidden">
          <button
            type="button"
            aria-label="Close navigation"
            onClick={() => setDrawerOpen(false)}
            className="absolute inset-0 bg-black/40 backdrop-blur-sm"
          />
          <aside className="glass shadow-e3 absolute inset-y-0 left-0 w-[270px] border-r">
            <div className="flex justify-end px-3 pt-3">
              <Button
                variant="ghost"
                size="icon"
                onClick={() => setDrawerOpen(false)}
                aria-label="Close navigation"
              >
                <X aria-hidden="true" />
              </Button>
            </div>
            {sidebar}
          </aside>
        </div>
      ) : null}

      <div className="flex min-w-0 flex-1 flex-col">
        <header className="glass sticky top-0 z-30 flex items-center justify-between gap-3 border-b px-4 py-2.5 sm:px-6">
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
            <span className="truncate text-sm font-semibold lg:hidden">{workspaceName}</span>
          </div>

          <div className="flex items-center gap-1.5">
            <span className="text-muted-foreground hidden max-w-[22ch] truncate text-sm sm:inline">
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

        <main className="flex-1 px-4 py-6 sm:px-6 lg:px-8">
          <div className="mx-auto w-full max-w-[1500px]">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
