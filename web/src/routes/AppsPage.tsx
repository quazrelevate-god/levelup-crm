/**
 * Apps (docs/18): outside systems that tell the CRM something happened.
 * One card per app; the developer tools (API keys, outgoing webhooks) are a
 * link away rather than on the same screen.
 */

import { Link } from 'react-router-dom'
import { ArrowRight, CreditCard, Plug } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { PageHeader } from '@/components/ui/page-header'
import { useAuth } from '@/features/auth/context'
import { useApps } from '@/features/automation/api'

export function AppsPage() {
  const { activeWorkspaceId } = useAuth()
  const apps = useApps(activeWorkspaceId as string)

  return (
    <div className="space-y-5">
      <PageHeader
        title="Apps"
        description="Connect the tools your business already uses. What they send can start your workflows."
      />

      {apps.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : apps.isError ? (
        <p className="text-muted-foreground text-sm">Apps could not be loaded.</p>
      ) : (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-3">
          {apps.data.map((app) => (
            <Card key={app.provider}>
              <CardContent className="flex h-full flex-col gap-3 pt-5">
                <div className="flex items-center gap-3">
                  <span className="rounded-xl bg-blue-600 p-2 text-white">
                    <CreditCard aria-hidden="true" className="size-5" />
                  </span>
                  <div className="min-w-0">
                    <p className="font-semibold">{app.label}</p>
                    {app.connected && app.is_active && app.secret_set ? (
                      <Badge variant="success">Active</Badge>
                    ) : app.connected ? (
                      <Badge variant="outline">Setup not finished</Badge>
                    ) : (
                      <Badge variant="outline">Not connected</Badge>
                    )}
                  </div>
                </div>
                <p className="text-muted-foreground flex-1 text-sm">{app.description}</p>
                <Button asChild variant={app.connected ? 'outline' : 'default'} size="sm">
                  <Link to={`/apps/${app.provider}`}>
                    {app.connected ? 'Manage' : 'Connect'} <ArrowRight aria-hidden="true" />
                  </Link>
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      <Card>
        <CardContent className="flex flex-wrap items-center gap-3 pt-5">
          <Plug aria-hidden="true" className="text-muted-foreground size-5" />
          <div className="min-w-0 flex-1">
            <p className="text-sm font-medium">Building your own connection?</p>
            <p className="text-muted-foreground text-xs">
              API keys for sending leads in, and webhooks for sending events out.
            </p>
          </div>
          <Button asChild variant="outline" size="sm">
            <Link to="/settings/integrations">Developer tools</Link>
          </Button>
        </CardContent>
      </Card>
    </div>
  )
}
