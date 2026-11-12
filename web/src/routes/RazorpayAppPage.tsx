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

import { API_BASE_URL } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import {
  useApps,
  useConnectApp,
  useCreateWorkflow,
  useRotateAppUrl,
  useUpdateApp,
} from '@/features/automation/api'
import { AppEvents, Step } from '@/features/automation/AppParts'
import { errorMessage as message } from '@/features/automation/helpers'

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

          <AppEvents workspaceId={workspaceId} provider="razorpay" title="What Razorpay has sent" />
        </>
      )}
    </div>
  )
}
