/**
 * Razorpay (docs/18): connect it in three steps, then see every payment it
 * sends and what became of it.
 *
 * The setup reads as numbered steps because the person doing it is moving
 * between two dashboards — this one and Razorpay's — and needs to know exactly
 * what to copy where. The signing secret is write-only: once saved, this
 * screen only ever says that it is set.
 */

import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { ArrowLeft, Check, Copy, CreditCard, RefreshCw } from 'lucide-react'

import { API_BASE_URL, ApiError } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import {
  type AppEvent,
  useAppEvents,
  useApps,
  useConnectApp,
  useCreateWorkflow,
  useRotateAppUrl,
  useUpdateApp,
} from '@/features/automation/api'
import { amountText } from '@/features/automation/helpers'

const OUTCOMES: Record<string, { label: string; variant: 'success' | 'outline' | 'destructive' }> =
  {
    MATCHED: { label: 'Matched a lead', variant: 'success' },
    CREATED_LEAD: { label: 'New lead created', variant: 'success' },
    NO_LEAD: { label: 'No matching lead', variant: 'outline' },
    DUPLICATE: { label: 'Already handled', variant: 'outline' },
    IGNORED: { label: 'Not a payment', variant: 'outline' },
    REJECTED: { label: 'Refused', variant: 'destructive' },
  }

function message(error: unknown): string | null {
  if (!error) return null
  return error instanceof ApiError ? error.message : 'That did not work. Please try again.'
}

export function RazorpayAppPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const navigate = useNavigate()
  const apps = useApps(workspaceId)
  const connect = useConnectApp(workspaceId)
  const update = useUpdateApp(workspaceId)
  const rotate = useRotateAppUrl(workspaceId)
  const createWorkflow = useCreateWorkflow(workspaceId)
  const [secret, setSecret] = useState('')
  const [copied, setCopied] = useState(false)

  const app = apps.data?.find((a) => a.provider === 'razorpay')
  const url = app?.webhook_path ? `${API_BASE_URL}${app.webhook_path}` : ''
  const error = message(connect.error) ?? message(update.error) ?? message(rotate.error)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="icon" variant="ghost" aria-label="Back to apps">
          <Link to="/apps">
            <ArrowLeft aria-hidden="true" />
          </Link>
        </Button>
        <span className="rounded-xl bg-blue-600 p-2 text-white">
          <CreditCard aria-hidden="true" className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight">Razorpay</h1>
          <p className="text-muted-foreground text-sm">
            Know the moment a lead pays, and let your workflows act on it.
          </p>
        </div>
        {app?.connected ? (
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label="Razorpay active"
              checked={app.is_active}
              disabled={update.isPending}
              onCheckedChange={(on) =>
                update.mutate({ provider: 'razorpay', body: { is_active: on } })
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

      {apps.isPending ? (
        <p className="text-muted-foreground text-sm">Loading…</p>
      ) : !app?.connected ? (
        <Card>
          <CardContent className="space-y-3 pt-5">
            <p className="text-sm">
              Connecting gives you a private web address. Razorpay sends every payment to it, the
              CRM finds the lead by phone or email, writes the payment on the lead’s timeline, and
              runs your payment workflows.
            </p>
            <Button disabled={connect.isPending} onClick={() => connect.mutate('razorpay')}>
              {connect.isPending ? 'Connecting…' : 'Connect Razorpay'}
            </Button>
          </CardContent>
        </Card>
      ) : (
        <>
          <Card>
            <CardHeader>
              <CardTitle>Set it up</CardTitle>
              <CardDescription>
                About two minutes. You need access to Razorpay’s dashboard.
              </CardDescription>
            </CardHeader>
            <CardContent className="space-y-5">
              <Step n={1} title="Copy this address">
                <div className="flex gap-2">
                  <Input
                    readOnly
                    aria-label="Webhook URL"
                    value={url}
                    className="font-mono text-xs"
                  />
                  <Button
                    variant="outline"
                    onClick={() => {
                      void navigator.clipboard.writeText(url).then(() => {
                        setCopied(true)
                        window.setTimeout(() => setCopied(false), 2000)
                      })
                    }}
                  >
                    {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                    {copied ? 'Copied' : 'Copy'}
                  </Button>
                </div>
                <p className="text-muted-foreground text-xs">
                  Keep it private: anyone with it could try to send fake payments (they would still
                  need your secret from step 2).
                </p>
              </Step>

              <Step n={2} title="Add it in Razorpay">
                <ol className="text-muted-foreground list-decimal space-y-1 pl-4 text-sm">
                  <li>
                    In the Razorpay Dashboard open <b>Accounts &amp; Settings → Webhooks</b> and
                    click <b>+ Add New Webhook</b>.
                  </li>
                  <li>
                    Paste the address into <b>Webhook URL</b>.
                  </li>
                  <li>
                    In <b>Secret</b>, type a password you make up (for example 20 random letters and
                    numbers). Keep it — you need it in step 3.
                  </li>
                  <li>
                    Under <b>Active Events</b> tick <b>payment.captured</b>, <b>payment.failed</b>{' '}
                    and, if you use payment links, <b>payment_link.paid</b>. Click{' '}
                    <b>Create Webhook</b>.
                  </li>
                </ol>
                <p className="text-muted-foreground text-xs">
                  Your existing webhooks (for example TeleCRM’s) keep working — Razorpay sends to
                  every webhook you add.
                </p>
              </Step>

              <Step n={3} title="Paste the same secret here">
                <form
                  className="flex gap-2"
                  onSubmit={(event) => {
                    event.preventDefault()
                    update.mutate(
                      { provider: 'razorpay', body: { secret } },
                      { onSuccess: () => setSecret('') },
                    )
                  }}
                >
                  <Input
                    type="password"
                    aria-label="Webhook secret"
                    autoComplete="off"
                    placeholder={
                      app.secret_set
                        ? '•••••••• (saved — type to replace)'
                        : 'The secret you typed in Razorpay'
                    }
                    value={secret}
                    onChange={(event) => setSecret(event.target.value)}
                  />
                  <Button type="submit" disabled={!secret.trim() || update.isPending}>
                    Save
                  </Button>
                </form>
                {app.secret_set ? (
                  <p className="text-success flex items-center gap-1 text-sm">
                    <Check aria-hidden="true" className="size-4" /> Secret saved. Payments will be
                    accepted.
                  </p>
                ) : (
                  <p className="text-sm text-amber-700 dark:text-amber-400">
                    Until the secret is saved, every payment Razorpay sends is refused.
                  </p>
                )}
              </Step>

              <Step n={4} title="Decide what happens when someone pays">
                <p className="text-muted-foreground text-sm">
                  A workflow decides it — for example “amount is 400 → stage Application Fee Paid”.
                </p>
                <div className="flex flex-wrap gap-2">
                  <Button
                    variant="outline"
                    disabled={createWorkflow.isPending}
                    onClick={() =>
                      createWorkflow.mutate(
                        {
                          name: `Razorpay payment ${new Date().toLocaleDateString()}`,
                          trigger: 'razorpay.payment_captured',
                        },
                        { onSuccess: (created) => void navigate(`/automations/${created.id}`) },
                      )
                    }
                  >
                    Create a payment workflow
                  </Button>
                  <Button asChild variant="ghost">
                    <Link to="/automations">See all workflows</Link>
                  </Button>
                </div>
              </Step>
            </CardContent>
          </Card>

          <Card>
            <CardHeader>
              <CardTitle>Options</CardTitle>
            </CardHeader>
            <CardContent className="space-y-4">
              <label className="flex items-start gap-3">
                <Switch
                  aria-label="Create a lead when nobody matches"
                  checked={Boolean(app.settings.create_lead_if_missing)}
                  disabled={update.isPending}
                  onCheckedChange={(on) =>
                    update.mutate({
                      provider: 'razorpay',
                      body: { settings: { create_lead_if_missing: on } },
                    })
                  }
                />
                <span className="text-sm">
                  <span className="block font-medium">
                    Create a new lead when a payment matches nobody
                  </span>
                  <span className="text-muted-foreground">
                    Off: the payment is only listed below as “No matching lead”.
                  </span>
                </span>
              </label>
              <div className="flex flex-wrap items-center gap-3">
                <Button
                  size="sm"
                  variant="outline"
                  disabled={rotate.isPending}
                  onClick={() => {
                    if (
                      window.confirm(
                        'Get a new address? The current one stops working at once, so you must paste the new one into Razorpay.',
                      )
                    ) {
                      rotate.mutate('razorpay')
                    }
                  }}
                >
                  <RefreshCw aria-hidden="true" /> Get a new address
                </Button>
                <span className="text-muted-foreground text-xs">
                  Only if the address may have leaked.
                </span>
              </div>
            </CardContent>
          </Card>

          <Payments workspaceId={workspaceId} />
        </>
      )}
    </div>
  )
}

function Step({
  n,
  title,
  children,
}: {
  readonly n: number
  readonly title: string
  readonly children: React.ReactNode
}) {
  return (
    <section className="flex gap-3">
      <span className="bg-primary/10 text-primary flex size-7 shrink-0 items-center justify-center rounded-full text-sm font-semibold">
        {n}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="font-medium">{title}</p>
        {children}
      </div>
    </section>
  )
}

function who(event: AppEvent): string {
  const phone = typeof event.data.phone === 'string' ? event.data.phone : ''
  const email = typeof event.data.email === 'string' ? event.data.email : ''
  return phone || email || '—'
}

function Payments({ workspaceId }: { readonly workspaceId: string }) {
  const [offset, setOffset] = useState(0)
  const events = useAppEvents(workspaceId, 'razorpay', offset)
  return (
    <Card>
      <CardHeader>
        <CardTitle>What Razorpay has sent</CardTitle>
        <CardDescription>Newest first. Refreshes on its own.</CardDescription>
      </CardHeader>
      <CardContent>
        {events.isPending ? (
          <p className="text-muted-foreground text-sm">Loading…</p>
        ) : events.isError ? (
          <p className="text-muted-foreground text-sm">The list could not be loaded.</p>
        ) : events.data.items.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            Nothing yet. After setup, the next payment appears here within seconds.
          </p>
        ) : (
          <>
            <div className="overflow-x-auto">
              <table className="w-full text-sm">
                <thead className="text-muted-foreground text-left text-xs">
                  <tr>
                    <th className="py-2 pr-3 font-medium">When</th>
                    <th className="py-2 pr-3 font-medium">Event</th>
                    <th className="py-2 pr-3 font-medium">Amount</th>
                    <th className="py-2 pr-3 font-medium">From</th>
                    <th className="py-2 pr-3 font-medium">Lead</th>
                    <th className="py-2 pr-3 font-medium">Result</th>
                    <th className="py-2 font-medium">Workflows</th>
                  </tr>
                </thead>
                <tbody>
                  {events.data.items.map((event) => {
                    const outcome = OUTCOMES[event.outcome] ?? {
                      label: event.outcome,
                      variant: 'outline' as const,
                    }
                    return (
                      <tr key={event.id} className="border-border/60 border-t align-top">
                        <td className="py-2 pr-3 whitespace-nowrap">
                          {new Date(event.created_at).toLocaleString()}
                        </td>
                        <td className="py-2 pr-3 font-mono text-xs">{event.event_type}</td>
                        <td className="py-2 pr-3">{amountText(event.data) || '—'}</td>
                        <td className="py-2 pr-3">{who(event)}</td>
                        <td className="py-2 pr-3">
                          {event.lead_id ? (
                            <Link
                              to={`/leads?lead=${event.lead_id}`}
                              className="text-primary hover:underline"
                            >
                              {event.lead_name ?? 'Lead'}
                            </Link>
                          ) : (
                            '—'
                          )}
                        </td>
                        <td className="py-2 pr-3">
                          <Badge variant={outcome.variant}>{outcome.label}</Badge>
                          {event.error ? (
                            <p className="text-muted-foreground mt-1 max-w-64 text-xs">
                              {event.error}
                            </p>
                          ) : null}
                        </td>
                        <td className="py-2">{event.workflows_run}</td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
            <div className="mt-3 flex gap-2">
              <Button
                size="sm"
                variant="outline"
                disabled={offset === 0}
                onClick={() => setOffset(Math.max(0, offset - 20))}
              >
                Newer
              </Button>
              <Button
                size="sm"
                variant="outline"
                disabled={offset + 20 >= events.data.total}
                onClick={() => setOffset(offset + 20)}
              >
                Older
              </Button>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}
