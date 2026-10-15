/**
 * Message templates (docs/01-data-model.md §5.4).
 *
 * Personal, shared or role-scoped bodies with `{{field_key}}` substitution. The
 * placeholder helper lists the workspace's own field keys, because those are
 * what the renderer resolves against — typing `{{first_name}}` when the field
 * key is `name` is the mistake this list exists to prevent.
 *
 * Rendering itself happens server-side against a lead, through the projection
 * service, so a template naming a field the sender cannot view resolves to
 * nothing rather than leaking it. That is exercised from the lead detail
 * overlay, not here.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { TemplateChannel } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useAuth } from '@/features/auth/context'
import { useLeadFields } from '@/features/fields/api'
import {
  useArchiveMessageTemplate,
  useCreateMessageTemplate,
  useMessageTemplates,
} from '@/features/leads/api'

/** The channels the data model declares. A product concept, not a taxonomy. */
const CHANNELS: readonly TemplateChannel[] = ['WHATSAPP', 'SMS', 'EMAIL']

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'subject_required') return 'An email template needs a subject.'
  if (cause.code === 'insufficient_permissions') {
    return 'Creating a shared or role-scoped template needs template administration rights.'
  }
  if (cause.code === 'not_your_template') return 'Only the owner or an admin can archive this.'
  return cause.message
}

export function TemplatesPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const [channel, setChannel] = useState<TemplateChannel>('WHATSAPP')
  const [name, setName] = useState('')
  const [subject, setSubject] = useState('')
  const [body, setBody] = useState('')
  const [shared, setShared] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const templates = useMessageTemplates(workspaceId)
  const fields = useLeadFields(workspaceId)
  const createTemplate = useCreateMessageTemplate(workspaceId)
  const archiveTemplate = useArchiveMessageTemplate(workspaceId)

  const insert = (key: string) => setBody((current) => `${current}{{${key}}}`)

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Message templates</h1>
        <p className="text-muted-foreground text-sm">
          Canned WhatsApp, SMS and email bodies. Placeholders resolve against a lead when you
          compose, through the same projection that governs every other read.
        </p>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">New template</CardTitle>
        </CardHeader>
        <CardContent className="space-y-3">
          <div className="grid gap-3 sm:grid-cols-3">
            <div className="space-y-1.5">
              <Label htmlFor="template-channel">Channel</Label>
              <Select
                id="template-channel"
                value={channel}
                onChange={(event) => setChannel(event.target.value as TemplateChannel)}
              >
                {CHANNELS.map((entry) => (
                  <option key={entry} value={entry}>
                    {entry.charAt(0) + entry.slice(1).toLowerCase()}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5 sm:col-span-2">
              <Label htmlFor="template-name">Name</Label>
              <Input
                id="template-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
          </div>

          {channel === 'EMAIL' ? (
            <div className="space-y-1.5">
              <Label htmlFor="template-subject">Subject</Label>
              <Input
                id="template-subject"
                value={subject}
                onChange={(event) => setSubject(event.target.value)}
              />
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="template-body">Body</Label>
            <Textarea
              id="template-body"
              rows={4}
              value={body}
              onChange={(event) => setBody(event.target.value)}
            />
            <div className="flex flex-wrap gap-1 pt-1">
              <span className="text-muted-foreground text-xs">Insert a field:</span>
              {(fields.data ?? [])
                .filter((field) => !field.is_hidden)
                .map((field) => (
                  <Button
                    key={field.id}
                    type="button"
                    variant="ghost"
                    size="sm"
                    className="h-6 px-1.5 text-xs"
                    onClick={() => insert(field.key)}
                  >
                    {`{{${field.key}}}`}
                  </Button>
                ))}
            </div>
          </div>

          <label className="flex items-center gap-2 text-sm">
            <Checkbox checked={shared} onChange={(event) => setShared(event.target.checked)} />
            Share with the whole workspace
            <span className="text-muted-foreground text-xs">(otherwise it is yours alone)</span>
          </label>

          <Button
            disabled={name.trim() === '' || body.trim() === '' || createTemplate.isPending}
            onClick={() => {
              void (async () => {
                setError(null)
                try {
                  await createTemplate.mutateAsync({
                    channel,
                    name: name.trim(),
                    body,
                    subject: channel === 'EMAIL' ? subject : null,
                    shared,
                  })
                  setName('')
                  setBody('')
                  setSubject('')
                } catch (cause) {
                  setError(message(cause))
                }
              })()
            }}
          >
            Create template
          </Button>
        </CardContent>
      </Card>

      <div className="grid gap-3 md:grid-cols-2">
        {(templates.data ?? []).map((template) => (
          <Card key={template.id} data-testid="template-card">
            <CardHeader className="flex-row items-start justify-between gap-2 space-y-0">
              <div>
                <CardTitle className="text-base">{template.name}</CardTitle>
                <div className="mt-1 flex gap-2">
                  <Badge variant="outline">{template.channel}</Badge>
                  <Badge variant="secondary">{template.visibility}</Badge>
                </div>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  void (async () => {
                    setError(null)
                    try {
                      await archiveTemplate.mutateAsync({ templateId: template.id })
                    } catch (cause) {
                      setError(message(cause))
                    }
                  })()
                }}
              >
                Archive
              </Button>
            </CardHeader>
            <CardContent>
              {template.subject ? (
                <p className="mb-1 text-sm font-medium">{template.subject}</p>
              ) : null}
              <pre className="text-muted-foreground text-sm whitespace-pre-wrap">
                {template.body}
              </pre>
            </CardContent>
          </Card>
        ))}
        {(templates.data ?? []).length === 0 && !templates.isLoading ? (
          <p className="text-muted-foreground text-sm">No templates yet.</p>
        ) : null}
      </div>
    </div>
  )
}
