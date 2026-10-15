import { useNavigate } from 'react-router-dom'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/features/auth/context'

/**
 * Choose which workspace to work in.
 *
 * Memberships without a licence, or that have been deactivated, are listed but
 * not selectable — showing them with a reason is more useful than hiding them,
 * because "where did my workspace go?" is the support ticket that follows.
 *
 * This route always renders, even when there is only one usable workspace.
 * Login already skips the picker in that case; short-circuiting *here* as well
 * would mean the one person who most needs to see "no licensed seat" — someone
 * with exactly one working workspace and one broken one — could never reach
 * the page that says so.
 */
export function WorkspacePickerPage() {
  const { memberships, selectWorkspace, logout } = useAuth()
  const navigate = useNavigate()

  function choose(workspaceId: string) {
    selectWorkspace(workspaceId)
    // Navigate to the shell root and let its index route decide the landing
    // page. Naming a screen here would mean two places to change whenever the
    // first screen does — which is exactly how this ended up pointing at the
    // members list after Leads became the landing page.
    void navigate('/', { replace: true })
  }

  return (
    <main className="bg-muted/30 flex min-h-screen items-center justify-center p-6">
      <div className="flex w-full max-w-2xl flex-col gap-4">
        <div className="flex items-baseline justify-between">
          <h1 className="text-2xl font-semibold tracking-tight">Choose a workspace</h1>
          <Button variant="ghost" size="sm" onClick={() => void logout()}>
            Sign out
          </Button>
        </div>

        {memberships.length === 0 ? (
          <Card>
            <CardHeader>
              <CardTitle>No workspaces yet</CardTitle>
              <CardDescription>
                You are signed in but not a member of any workspace. Create one, or ask an
                administrator to invite you.
              </CardDescription>
            </CardHeader>
          </Card>
        ) : null}

        {memberships.map((membership) => {
          const selectable = membership.is_active && membership.has_license
          return (
            <Card key={membership.id}>
              <CardHeader>
                <div className="flex items-center justify-between gap-4">
                  <div className="flex flex-col gap-1">
                    <CardTitle>{membership.workspace.name}</CardTitle>
                    <CardDescription>
                      {membership.template_name} · {membership.workspace.timezone} ·{' '}
                      {membership.workspace.currency}
                    </CardDescription>
                  </div>
                  <Button
                    disabled={!selectable}
                    onClick={() => choose(membership.workspace.id)}
                    aria-label={`Open ${membership.workspace.name}`}
                  >
                    Open
                  </Button>
                </div>
              </CardHeader>
              {selectable ? null : (
                <CardContent>
                  <Badge variant="destructive">
                    {membership.is_active ? 'No licensed seat' : 'Deactivated'}
                  </Badge>
                </CardContent>
              )}
            </Card>
          )
        })}
      </div>
    </main>
  )
}
