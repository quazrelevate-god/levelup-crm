/**
 * API keys, webhooks, the outbox and the intake log (M10).
 *
 * One screen because they are one job: getting data in and out of this
 * workspace, and finding out why it didn't.
 *
 * The shape of this page is set by two facts about the underlying system:
 *
 * **Two secrets are shown exactly once.** An API key and a webhook signing
 * secret both appear at creation and never again — the row holds a hash of the
 * first and nothing needs the second here. So both get a panel that says so
 * plainly rather than a value that looks re-readable.
 *
 * **The outbox and the intake log move on their own.** A webhook retries on the
 * worker's clock, so both poll; a static list would show an operator a DEAD
 * event that had already been redriven.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { IntakeOutcome, OutboxStatus, PermissionTemplateSummary } from '@/api/types'
import { api } from '@/api/client'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import {
  useApiKeys,
  useCreateApiKey,
  useCreateWebhook,
  useDeleteWebhook,
  useEventNames,
  useIntakeLog,
  useOutbox,
  useRetryOutboxEvent,
  useRevokeApiKey,
  useTestWebhook,
  useWebhooks,
} from '@/features/integrations/api'
import { useQuery } from '@tanstack/react-query'

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow managing integrations.'
  }
  return cause.message
}

function when(iso: string | null): string {
  if (!iso) return '—'
  return new Date(iso).toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  })
}

const OUTBOX_TONE: Record<OutboxStatus, string> = {
  PENDING: '',
  DELIVERING: '',
  DELIVERED: '',
  FAILED: 'border-destructive text-destructive',
  DEAD: 'border-destructive text-destructive',
}

const INTAKE_TONE: Record<IntakeOutcome, string> = {
  CREATED: '',
  UPDATED: '',
  SKIPPED: '',
  REJECTED: 'border-destructive text-destructive',
}

export function IntegrationsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const [error, setError] = useState<string | null>(null)
  const [keyOpen, setKeyOpen] = useState(false)
  const [hookOpen, setHookOpen] = useState(false)
  const [revealed, setRevealed] = useState<{ label: string; value: string } | null>(null)
  const [outboxFilter, setOutboxFilter] = useState<OutboxStatus | ''>('')
  const [rejectedOnly, setRejectedOnly] = useState(false)

  const keys = useApiKeys(workspaceId)
  const hooks = useWebhooks(workspaceId)
  const outbox = useOutbox(workspaceId, outboxFilter || undefined)
  const intake = useIntakeLog(workspaceId, rejectedOnly)
  const revoke = useRevokeApiKey(workspaceId)
  const removeHook = useDeleteWebhook(workspaceId)
  const testHook = useTestWebhook(workspaceId)
  const retry = useRetryOutboxEvent(workspaceId)

  const templates = useQuery({
    queryKey: ['permission-templates', workspaceId],
    queryFn: () =>
      api.get<PermissionTemplateSummary[]>(
        `/workspaces/${workspaceId}/settings/permission-templates`,
      ),
    enabled: Boolean(workspaceId),
  })

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Integrations</h1>
        <p className="text-muted-foreground text-sm">
          Getting data in and out &mdash; and finding out why it didn&rsquo;t.
        </p>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      {/* Both secrets in this milestone appear once. Saying so is the whole
          job of this panel — an operator who closes it rotates rather than
          hunting for a "show again" that does not exist. */}
      {revealed ? (
        <Card data-testid="revealed-secret" className="border-foreground">
          <CardHeader>
            <CardTitle className="text-base">{revealed.label}</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            <code className="bg-muted block overflow-x-auto rounded-md p-3 text-xs">
              {revealed.value}
            </code>
            <p className="text-sm">
              Copy it now. This is the only time it is shown &mdash; only a hash is stored, so it
              cannot be shown again.
            </p>
            <Button variant="outline" size="sm" onClick={() => setRevealed(null)}>
              I&rsquo;ve copied it
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">API keys</CardTitle>
          <Button size="sm" onClick={() => setKeyOpen(true)}>
            New key
          </Button>
        </CardHeader>
        <CardContent>
          {keys.isError ? (
            <p role="alert" className="text-destructive py-6 text-center text-sm">
              {message(keys.error)}
            </p>
          ) : (keys.data ?? []).length === 0 && !keys.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">
              No keys yet. An integration needs one to post leads.
            </p>
          ) : (
            <ul className="space-y-2">
              {(keys.data ?? []).map((key) => (
                <li
                  key={key.id}
                  data-testid="api-key"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-48 flex-1">
                    <span className="font-medium">{key.name}</span>
                    <span className="text-muted-foreground block font-mono text-xs">
                      {key.prefix}… · last used {when(key.last_used_at)}
                    </span>
                  </div>
                  {key.revoked_at ? (
                    <Badge variant="outline">Revoked</Badge>
                  ) : (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setError(null)
                        revoke.mutate(key.id, {
                          onError: (cause) => setError(message(cause)),
                        })
                      }}
                    >
                      Revoke
                    </Button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row items-center justify-between">
          <CardTitle className="text-base">Webhooks</CardTitle>
          <Button size="sm" onClick={() => setHookOpen(true)}>
            New webhook
          </Button>
        </CardHeader>
        <CardContent>
          {(hooks.data ?? []).length === 0 && !hooks.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">Nothing subscribed.</p>
          ) : (
            <ul className="space-y-2">
              {(hooks.data ?? []).map((hook) => (
                <li
                  key={hook.id}
                  data-testid="webhook"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-56 flex-1">
                    <span className="font-medium">{hook.name}</span>
                    <span className="text-muted-foreground block text-xs break-all">
                      {hook.url}
                    </span>
                    <span className="text-muted-foreground block text-xs">
                      {hook.events.length === 0
                        ? 'every event'
                        : `${hook.events.length} event${hook.events.length === 1 ? '' : 's'}`}
                    </span>
                  </div>
                  {!hook.is_active ? <Badge variant="outline">Inactive</Badge> : null}
                  <Button
                    variant="outline"
                    size="sm"
                    disabled={testHook.isPending}
                    onClick={() => {
                      setError(null)
                      testHook.mutate(hook.id, {
                        onError: (cause) => setError(message(cause)),
                      })
                    }}
                  >
                    Test
                  </Button>
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() => {
                      setError(null)
                      removeHook.mutate(hook.id, {
                        onError: (cause) => setError(message(cause)),
                      })
                    }}
                  >
                    Disable
                  </Button>
                </li>
              ))}
            </ul>
          )}

          {testHook.data ? (
            <p
              role="status"
              data-testid="webhook-test-result"
              className={testHook.data.delivered ? 'pt-3 text-sm' : 'text-destructive pt-3 text-sm'}
            >
              {testHook.data.delivered
                ? `Delivered — your endpoint answered ${testHook.data.status_code}.`
                : `Not delivered — ${testHook.data.error ?? `HTTP ${testHook.data.status_code}`}.`}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Outbound queue</CardTitle>
          <Select
            aria-label="Filter by delivery status"
            className="w-44"
            value={outboxFilter}
            onChange={(event) => setOutboxFilter(event.target.value as OutboxStatus | '')}
          >
            <option value="">All statuses</option>
            <option value="PENDING">Pending</option>
            <option value="DELIVERING">Delivering</option>
            <option value="DELIVERED">Delivered</option>
            <option value="FAILED">Failed</option>
            <option value="DEAD">Dead</option>
          </Select>
        </CardHeader>
        <CardContent>
          {(outbox.data?.items ?? []).length === 0 && !outbox.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">Nothing queued.</p>
          ) : (
            <ul className="space-y-2">
              {(outbox.data?.items ?? []).map((row) => (
                <li
                  key={row.id}
                  data-testid="outbox-event"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-56 flex-1">
                    <span className="font-mono text-sm">{row.event}</span>
                    <span className="text-muted-foreground block text-xs">
                      {when(row.occurred_at)} · attempt {row.attempts}
                      {row.status === 'FAILED' ? ` · next try ${when(row.next_attempt_at)}` : ''}
                    </span>
                    {row.last_error ? (
                      <span className="text-destructive block text-xs">{row.last_error}</span>
                    ) : null}
                  </div>
                  <Badge variant="outline" className={OUTBOX_TONE[row.status]}>
                    {row.status.toLowerCase()}
                  </Badge>
                  {row.status === 'DEAD' || row.status === 'FAILED' ? (
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setError(null)
                        retry.mutate(row.id, {
                          onError: (cause) => setError(message(cause)),
                        })
                      }}
                    >
                      Retry
                    </Button>
                  ) : null}
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader className="flex flex-row flex-wrap items-center justify-between gap-2">
          <CardTitle className="text-base">Intake log</CardTitle>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={rejectedOnly}
              onChange={(event) => setRejectedOnly(event.target.checked)}
            />
            Rejections only
          </label>
        </CardHeader>
        <CardContent>
          {(intake.data?.items ?? []).length === 0 && !intake.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">
              {rejectedOnly ? 'Nothing has been rejected.' : 'No intake traffic yet.'}
            </p>
          ) : (
            <ul className="space-y-2">
              {(intake.data?.items ?? []).map((entry) => (
                <li
                  key={entry.id}
                  data-testid="intake-entry"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <div className="min-w-56 flex-1">
                    <span className="font-mono text-sm">/{entry.endpoint}</span>
                    <span className="text-muted-foreground block text-xs">
                      {when(entry.created_at)} · HTTP {entry.status_code}
                    </span>
                    {entry.error ? (
                      <span className="text-destructive block text-xs">{entry.error}</span>
                    ) : null}
                    {/* Unknown fields are *accepted*, so this is the only place
                        an operator ever learns one arrived. */}
                    {entry.warnings.map((warning) => (
                      <span key={warning} className="text-muted-foreground block text-xs">
                        ⚠ {warning}
                      </span>
                    ))}
                  </div>
                  <Badge variant="outline" className={INTAKE_TONE[entry.outcome]}>
                    {entry.outcome.toLowerCase()}
                  </Badge>
                </li>
              ))}
            </ul>
          )}
        </CardContent>
      </Card>

      <NewKeyDialog
        open={keyOpen}
        templates={templates.data ?? []}
        onClose={() => setKeyOpen(false)}
        onError={setError}
        onCreated={(value) => setRevealed({ label: 'Your new API key', value })}
      />
      <NewWebhookDialog
        open={hookOpen}
        templates={templates.data ?? []}
        onClose={() => setHookOpen(false)}
        onError={setError}
        onCreated={(value) => setRevealed({ label: 'Your webhook signing secret', value })}
      />
    </div>
  )
}

function NewKeyDialog({
  open,
  templates,
  onClose,
  onError,
  onCreated,
}: {
  readonly open: boolean
  readonly templates: readonly PermissionTemplateSummary[]
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
  readonly onCreated: (key: string) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const create = useCreateApiKey(activeWorkspaceId as string)
  const [name, setName] = useState('')
  const [templateId, setTemplateId] = useState('')

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New API key"
      description="The key inherits a permission template — that is what bounds what it can read and write."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || !templateId || create.isPending}
            onClick={() => {
              onError(null)
              create.mutate(
                { name: name.trim(), permission_template_id: templateId },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: (created) => {
                    onCreated(created.key)
                    setName('')
                    setTemplateId('')
                    onClose()
                  },
                },
              )
            }}
          >
            Create key
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="key-name">Name</FieldLabel>
          <Input
            id="key-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Website form"
          />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="key-template">Permission template</FieldLabel>
          <Select
            id="key-template"
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value)}
          >
            <option value="">Choose a template</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
          <p className="text-muted-foreground text-xs">
            A field this template cannot edit is refused by name, not silently dropped. Give it the
            narrowest one that works.
          </p>
        </div>
      </div>
    </Dialog>
  )
}

function NewWebhookDialog({
  open,
  templates,
  onClose,
  onError,
  onCreated,
}: {
  readonly open: boolean
  readonly templates: readonly PermissionTemplateSummary[]
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
  readonly onCreated: (secret: string) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const create = useCreateWebhook(workspaceId)
  const events = useEventNames(workspaceId)

  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [chosen, setChosen] = useState<string[]>([])
  const [templateId, setTemplateId] = useState('')

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New webhook"
      description="Payloads are projected through the template you choose, so a webhook cannot see more than that role can."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || !url.trim() || !templateId || create.isPending}
            onClick={() => {
              onError(null)
              create.mutate(
                {
                  name: name.trim(),
                  url: url.trim(),
                  events: chosen,
                  permission_template_id: templateId,
                },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: (created) => {
                    onCreated(created.secret)
                    setName('')
                    setUrl('')
                    setChosen([])
                    setTemplateId('')
                    onClose()
                  },
                },
              )
            }}
          >
            Create webhook
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="hook-name">Name</FieldLabel>
          <Input id="hook-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="hook-url">URL</FieldLabel>
          <Input
            id="hook-url"
            value={url}
            onChange={(event) => setUrl(event.target.value)}
            placeholder="https://example.com/hooks/crm"
          />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="hook-template">Permission template</FieldLabel>
          <Select
            id="hook-template"
            value={templateId}
            onChange={(event) => setTemplateId(event.target.value)}
          >
            <option value="">Choose a template</option>
            {templates.map((template) => (
              <option key={template.id} value={template.id}>
                {template.name}
              </option>
            ))}
          </Select>
        </div>
        <fieldset className="space-y-1.5">
          <legend className="text-sm font-medium">Events</legend>
          <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border p-2">
            {(events.data ?? []).map((event) => (
              <label key={event} className="flex items-center gap-2 font-mono text-xs">
                <input
                  type="checkbox"
                  checked={chosen.includes(event)}
                  onChange={(changed) =>
                    setChosen((current) =>
                      changed.target.checked
                        ? [...current, event]
                        : current.filter((entry) => entry !== event),
                    )
                  }
                />
                {event}
              </label>
            ))}
          </div>
          <p className="text-muted-foreground text-xs">
            {chosen.length === 0
              ? 'None chosen — this endpoint will receive every event.'
              : `${chosen.length} chosen.`}
          </p>
        </fieldset>
      </div>
    </Dialog>
  )
}
