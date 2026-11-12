/**
 * Calendly (docs/22): connect with a personal access token, and the CRM sets
 * up Calendly's webhook itself — Calendly has no screen for that. Then every
 * booking and cancellation shows here and starts workflows.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, CalendarCheck, Check } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import { AppEvents, Step } from '@/features/automation/AppParts'
import {
  useApps,
  useCreateWorkflow,
  useSetupCalendly,
  useUpdateApp,
} from '@/features/automation/api'
import { errorMessage, settingText } from '@/features/automation/helpers'

export function CalendlyAppPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const navigate = useNavigate()
  const apps = useApps(workspaceId)
  const setup = useSetupCalendly(workspaceId)
  const update = useUpdateApp(workspaceId)
  const createWorkflow = useCreateWorkflow(workspaceId)
  const [token, setToken] = useState('')
  const app = apps.data?.find((a) => a.provider === 'calendly')
  const ready = Boolean(app?.connected && app.secret_set)
  const error = errorMessage(setup.error) ?? errorMessage(update.error)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="icon" variant="ghost" aria-label="Back to apps">
          <Link to="/apps">
            <ArrowLeft aria-hidden="true" />
          </Link>
        </Button>
        <span className="rounded-xl bg-sky-600 p-2 text-white">
          <CalendarCheck aria-hidden="true" className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight">Calendly</h1>
          <p className="text-muted-foreground text-sm">
            Know the moment a lead books — or cancels — a meeting, like the interview.
          </p>
        </div>
        {ready && app ? (
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label="Calendly active"
              checked={app.is_active}
              disabled={update.isPending}
              onCheckedChange={(on) =>
                update.mutate({ provider: 'calendly', body: { is_active: on } })
              }
            />
            {app.is_active ? (
              <Badge variant="success">Active</Badge>
            ) : (
              <Badge variant="outline">Paused</Badge>
            )}
          </label>
        ) : null}
      </div>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>{ready ? 'Connected' : 'Set it up'}</CardTitle>
          <CardDescription>
            {ready
              ? `Watching ${settingText(app?.settings.account, 'your Calendly')} (${settingText(app?.settings.scope)} bookings).`
              : 'About two minutes. Calendly webhooks need a paid Calendly plan (Standard or above).'}
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-5">
          <Step n={1} title="Get a token from Calendly">
            <ol className="text-muted-foreground list-decimal space-y-1 pl-4 text-sm">
              <li>
                In Calendly open <b>Integrations &amp; apps → API and webhooks</b>.
              </li>
              <li>
                Under <b>Personal access tokens</b> click <b>Generate new token</b>, name it “CRM”,
                and copy it.
              </li>
            </ol>
          </Step>
          <Step n={2} title={ready ? 'Paste a new token to reconnect' : 'Paste it here'}>
            <form
              className="flex gap-2"
              onSubmit={(event) => {
                event.preventDefault()
                setup.mutate(token, { onSuccess: () => setToken('') })
              }}
            >
              <Input
                type="password"
                aria-label="Calendly token"
                autoComplete="off"
                placeholder={ready ? '•••••••• (saved)' : 'Your personal access token'}
                value={token}
                onChange={(event) => setToken(event.target.value)}
              />
              <Button type="submit" disabled={token.trim().length < 10 || setup.isPending}>
                {setup.isPending ? 'Connecting…' : 'Connect'}
              </Button>
            </form>
            {ready ? (
              <p className="text-success flex items-center gap-1 text-sm">
                <Check aria-hidden="true" className="size-4" /> The CRM created Calendly’s webhook.
                Bookings now arrive here.
              </p>
            ) : (
              <p className="text-muted-foreground text-xs">
                The CRM uses it to create the webhook, and keeps it hidden from everyone.
              </p>
            )}
          </Step>
          <Step n={3} title="Decide what happens when someone books">
            <p className="text-muted-foreground text-sm">
              For example “Meeting booked → stage Interview Scheduled”.
            </p>
            <Button
              variant="outline"
              disabled={createWorkflow.isPending}
              onClick={() =>
                createWorkflow.mutate(
                  {
                    name: `Calendly booking ${new Date().toLocaleDateString()}`,
                    trigger: 'calendly.invitee_created',
                  },
                  { onSuccess: (created) => void navigate(`/automations/${created.id}`) },
                )
              }
            >
              Create a booking workflow
            </Button>
          </Step>
        </CardContent>
      </Card>

      {ready && app ? (
        <Card>
          <CardHeader>
            <CardTitle>Options</CardTitle>
          </CardHeader>
          <CardContent>
            <label className="flex items-start gap-3">
              <Switch
                aria-label="Create a lead when nobody matches"
                checked={Boolean(app.settings.create_lead_if_missing)}
                disabled={update.isPending}
                onCheckedChange={(on) =>
                  update.mutate({
                    provider: 'calendly',
                    body: { settings: { create_lead_if_missing: on } },
                  })
                }
              />
              <span className="text-sm">
                <span className="block font-medium">
                  Create a new lead when a booking matches nobody
                </span>
                <span className="text-muted-foreground">
                  Needs the booking form to ask for a phone number.
                </span>
              </span>
            </label>
          </CardContent>
        </Card>
      ) : null}

      {app?.connected ? (
        <AppEvents workspaceId={workspaceId} provider="calendly" title="What Calendly has sent" />
      ) : null}
    </div>
  )
}
