import { Link, Navigate, Outlet, useLocation } from 'react-router-dom'

import { Button } from '@/components/ui/button'
import { useAuth } from '@/features/auth/context'

/**
 * Every tenant screen, in the order an admin meets them: work the leads and
 * the follow-ups they generate, move data in and out, review what was changed,
 * then configure the schema that shapes it all.
 */
const NAV_LINKS = [
  { to: '/dashboard', label: 'Dashboard' },
  { to: '/leads', label: 'Leads' },
  { to: '/calls', label: 'Call details' },
  { to: '/tasks', label: 'Tasks' },
  { to: '/import', label: 'Import' },
  { to: '/edit-report', label: 'Edit report' },
  { to: '/templates', label: 'Templates' },
  { to: '/members', label: 'Team' },
  { to: '/settings/fields', label: 'Fields' },
  { to: '/settings/pipeline', label: 'Pipeline' },
  { to: '/settings/custom-actions', label: 'Actions' },
  { to: '/settings/permissions', label: 'Permissions' },
  { to: '/settings/assignment', label: 'Assignment' },
  { to: '/settings/scheduled-reports', label: 'Schedules' },
  { to: '/settings/integrations', label: 'Integrations' },
  { to: '/settings/voice-extraction', label: 'Voice extraction' },
  { to: '/settings/dashboards', label: 'Dashboards' },
  { to: '/status', label: 'System' },
] as const

/**
 * The authenticated shell.
 *
 * Guards two things in one place: you must be signed in, and you must have
 * chosen a workspace. Every tenant route hangs off this, so no page has to
 * check either for itself.
 */
export function AppLayout() {
  const { status, user, activeMembership, activeWorkspaceId, memberships, logout } = useAuth()
  const location = useLocation()

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

  return (
    <div className="flex min-h-screen flex-col">
      <header className="flex items-center justify-between gap-4 border-b px-6 py-3">
        <div className="flex items-center gap-6">
          <span className="font-semibold">{activeMembership.workspace.name}</span>
          <nav className="flex flex-wrap items-center gap-1">
            {NAV_LINKS.map((link) => (
              <Button
                key={link.to}
                variant={location.pathname.startsWith(link.to) ? 'secondary' : 'ghost'}
                size="sm"
                asChild
              >
                <Link to={link.to}>{link.label}</Link>
              </Button>
            ))}
          </nav>
        </div>

        <div className="flex items-center gap-3">
          <span className="text-muted-foreground text-sm">{user?.email}</span>
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

      <main className="flex-1 p-6">
        <Outlet />
      </main>
    </div>
  )
}
