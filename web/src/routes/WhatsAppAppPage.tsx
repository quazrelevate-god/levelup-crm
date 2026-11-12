/**
 * WhatsApp (docs/22): the company's own business number, through Meta's
 * WhatsApp Cloud API. Once connected, any workflow can send an approved
 * template. The access token is write-only, like every secret here.
 */

import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowLeft, Check, MessageCircle, Send } from 'lucide-react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { useAuth } from '@/features/auth/context'
import { Step } from '@/features/automation/AppParts'
import { useApps, useConnectApp, useTestWhatsApp, useUpdateApp } from '@/features/automation/api'
import { errorMessage, settingText } from '@/features/automation/helpers'

export function WhatsAppAppPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const apps = useApps(workspaceId)
  const connect = useConnectApp(workspaceId)
  const update = useUpdateApp(workspaceId)
  const test = useTestWhatsApp(workspaceId)
  const app = apps.data?.find((a) => a.provider === 'whatsapp')
  const [phoneId, setPhoneId] = useState('')
  const [token, setToken] = useState('')
  const [to, setTo] = useState('')
  const savedPhoneId = settingText(app?.settings.phone_number_id)
  const ready = Boolean(app?.connected && app.credential_set && savedPhoneId)
  const error =
    errorMessage(connect.error) ?? errorMessage(update.error) ?? errorMessage(test.error)

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center gap-3">
        <Button asChild size="icon" variant="ghost" aria-label="Back to apps">
          <Link to="/apps">
            <ArrowLeft aria-hidden="true" />
          </Link>
        </Button>
        <span className="rounded-xl bg-green-600 p-2 text-white">
          <MessageCircle aria-hidden="true" className="size-5" />
        </span>
        <div className="min-w-0 flex-1">
          <h1 className="text-2xl font-semibold tracking-tight">WhatsApp</h1>
          <p className="text-muted-foreground text-sm">
            Send approved template messages from your own WhatsApp Business number.
          </p>
        </div>
        {app?.connected ? (
          <label className="flex items-center gap-2 text-sm">
            <Switch
              aria-label="WhatsApp active"
              checked={app.is_active}
              disabled={update.isPending}
              onCheckedChange={(on) =>
                update.mutate({ provider: 'whatsapp', body: { is_active: on } })
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

      {!app?.connected ? (
        <Card>
          <CardContent className="space-y-3 pt-5">
            <p className="text-sm">
              You need a WhatsApp Business account on Meta (the official WhatsApp Cloud API). If you
              use a provider like Interakt or AiSensy instead, use a workflow’s “Call an API” block
              with their API.
            </p>
            <Button disabled={connect.isPending} onClick={() => connect.mutate('whatsapp')}>
              Connect WhatsApp
            </Button>
          </CardContent>
        </Card>
      ) : (
        <Card>
          <CardHeader>
            <CardTitle>{ready ? 'Connected' : 'Set it up'}</CardTitle>
            <CardDescription>
              From Meta’s WhatsApp Manager (business.facebook.com → WhatsApp Manager).
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-5">
            <Step n={1} title="Your phone number ID and access token">
              <p className="text-muted-foreground text-sm">
                In <b>WhatsApp Manager → API Setup</b> copy the <b>Phone number ID</b>. Create a
                permanent access token for a system user with <b>whatsapp_business_messaging</b>{' '}
                permission (Business Settings → System users).
              </p>
              <form
                className="grid gap-2 sm:grid-cols-[1fr_1fr_auto]"
                onSubmit={(event) => {
                  event.preventDefault()
                  update.mutate(
                    {
                      provider: 'whatsapp',
                      body: {
                        ...(phoneId.trim()
                          ? { settings: { phone_number_id: phoneId.trim() } }
                          : {}),
                        ...(token.trim() ? { credential: token.trim() } : {}),
                      },
                    },
                    {
                      onSuccess: () => {
                        setPhoneId('')
                        setToken('')
                      },
                    },
                  )
                }}
              >
                <div className="space-y-1">
                  <Label htmlFor="wa-phone-id">Phone number ID</Label>
                  <Input
                    id="wa-phone-id"
                    placeholder={savedPhoneId || 'e.g. 1234567890'}
                    value={phoneId}
                    onChange={(event) => setPhoneId(event.target.value)}
                  />
                </div>
                <div className="space-y-1">
                  <Label htmlFor="wa-token">Access token</Label>
                  <Input
                    id="wa-token"
                    type="password"
                    autoComplete="off"
                    placeholder={app.credential_set ? '•••••••• (saved)' : 'Paste the token'}
                    value={token}
                    onChange={(event) => setToken(event.target.value)}
                  />
                </div>
                <Button
                  type="submit"
                  className="self-end"
                  disabled={(!phoneId.trim() && !token.trim()) || update.isPending}
                >
                  Save
                </Button>
              </form>
              {ready ? (
                <p className="text-success flex items-center gap-1 text-sm">
                  <Check aria-hidden="true" className="size-4" /> Saved (number ID {savedPhoneId}).
                </p>
              ) : null}
            </Step>
            <Step n={2} title="Send yourself a test">
              <p className="text-muted-foreground text-sm">
                Sends Meta’s built-in “hello_world” template to your own WhatsApp.
              </p>
              <form
                className="flex gap-2"
                onSubmit={(event) => {
                  event.preventDefault()
                  test.mutate(to)
                }}
              >
                <Input
                  aria-label="Your WhatsApp number"
                  placeholder="Your number, e.g. 98765 43210"
                  value={to}
                  onChange={(event) => setTo(event.target.value)}
                />
                <Button
                  type="submit"
                  variant="outline"
                  disabled={!ready || !to.trim() || test.isPending}
                >
                  <Send aria-hidden="true" /> Send test
                </Button>
              </form>
              {test.data ? (
                <p
                  className={
                    test.data.status === 'SUCCESS'
                      ? 'text-success text-sm'
                      : 'text-destructive text-sm'
                  }
                >
                  {test.data.steps[0]?.detail}
                </p>
              ) : null}
            </Step>
            <Step n={3} title="Use it in a workflow">
              <p className="text-muted-foreground text-sm">
                Add a <b>Send WhatsApp</b> block — e.g. after “AI call finished” when the lead asked
                for the fee link. Templates must be approved by Meta first.
              </p>
              <Button asChild variant="ghost">
                <Link to="/automations">Open workflows</Link>
              </Button>
            </Step>
          </CardContent>
        </Card>
      )}
    </div>
  )
}
