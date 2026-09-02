/**
 * Settings → Voice extraction (docs/12).
 *
 * One screen, one job: map Bolna disposition names to CRM lead fields, at a
 * confidence threshold, on or off. Everything the write-back is *not* going
 * to do — refuse to overwrite with empty, skip identities, defer to the call
 * summary — is enforced server-side; the page's job is to make the mapping
 * itself unambiguous.
 */

import { useMemo, useState } from 'react'

import { ApiError } from '@/api/client'
import type { LeadField } from '@/api/types'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Dialog } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label as FieldLabel } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { useLeadFields } from '@/features/fields/api'
import {
  useCreateVoiceExtractionMapping,
  useDeleteVoiceExtractionMapping,
  useUpdateVoiceExtractionMapping,
  useVoiceExtractionMappings,
  type VoiceExtractionMapping,
} from '@/features/voice/api'

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow managing integrations.'
  }
  if (cause.code === 'duplicate_disposition') {
    return 'A mapping for that disposition already exists here.'
  }
  if (cause.code === 'summary_disposition_conflict') {
    return (
      'That disposition is the deployment’s configured call summary. ' +
      'Mapping it to a lead field would put the whole call recap into that field.'
    )
  }
  if (cause.code === 'unknown_field') {
    return 'That target field is not in this workspace.'
  }
  if (cause.code === 'hidden_field') {
    return 'That target field is hidden and cannot receive automatic writes.'
  }
  return cause.message
}

interface FormState {
  readonly disposition_name: string
  readonly target_field_key: string
  readonly min_confidence: number
  readonly is_enabled: boolean
}

const EMPTY: FormState = {
  disposition_name: '',
  target_field_key: '',
  min_confidence: 0.7,
  is_enabled: true,
}

function toFormState(mapping: VoiceExtractionMapping): FormState {
  return {
    disposition_name: mapping.disposition_name,
    target_field_key: mapping.target_field_key,
    min_confidence: mapping.min_confidence,
    is_enabled: mapping.is_enabled,
  }
}

export function VoiceExtractionSettingsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const mappings = useVoiceExtractionMappings(workspaceId)
  const leadFields = useLeadFields(workspaceId)
  const create = useCreateVoiceExtractionMapping(workspaceId)
  const update = useUpdateVoiceExtractionMapping(workspaceId)
  const remove = useDeleteVoiceExtractionMapping(workspaceId)

  const [editing, setEditing] = useState<VoiceExtractionMapping | null>(null)
  const [form, setForm] = useState<FormState>(EMPTY)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const editableFields = useMemo<LeadField[]>(
    () => (leadFields.data ?? []).filter((f) => !f.is_hidden),
    [leadFields.data],
  )

  const rows = mappings.data ?? []

  function beginCreate(): void {
    setEditing(null)
    setForm(EMPTY)
    setError(null)
    setDialogOpen(true)
  }

  function beginEdit(mapping: VoiceExtractionMapping): void {
    setEditing(mapping)
    setForm(toFormState(mapping))
    setError(null)
    setDialogOpen(true)
  }

  async function submit(): Promise<void> {
    setError(null)
    if (!form.disposition_name.trim()) {
      setError('A disposition name is required.')
      return
    }
    if (!form.target_field_key) {
      setError('Pick a lead field.')
      return
    }
    try {
      if (editing) {
        await update.mutateAsync({ id: editing.id, body: form })
      } else {
        await create.mutateAsync(form)
      }
      setDialogOpen(false)
    } catch (cause) {
      setError(message(cause))
    }
  }

  async function toggle(mapping: VoiceExtractionMapping): Promise<void> {
    try {
      await update.mutateAsync({
        id: mapping.id,
        body: { is_enabled: !mapping.is_enabled },
      })
    } catch (cause) {
      setError(message(cause))
    }
  }

  async function del(mapping: VoiceExtractionMapping): Promise<void> {
    if (
      !window.confirm(
        `Delete the mapping for "${mapping.disposition_name}"? Disable it instead if you want to keep the configuration.`,
      )
    ) {
      return
    }
    try {
      await remove.mutateAsync(mapping.id)
    } catch (cause) {
      setError(message(cause))
    }
  }

  return (
    <div className="mx-auto max-w-4xl space-y-6 p-6">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold">Voice extraction</h1>
          <p className="text-muted-foreground mt-1 text-sm">
            Map a Bolna disposition name to a CRM lead field. After a completed
            call, an extraction that clears its confidence threshold updates the
            field. Below the threshold, the extracted value is recorded on the
            lead&rsquo;s timeline but the field is left alone.
          </p>
        </div>
        <Button onClick={beginCreate}>Add mapping</Button>
      </div>

      {error ? (
        <div className="border-destructive/40 bg-destructive/10 text-destructive rounded-md border p-3 text-sm">
          {error}
        </div>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Mappings</CardTitle>
        </CardHeader>
        <CardContent>
          {mappings.isLoading ? (
            <p className="text-muted-foreground text-sm">Loading&hellip;</p>
          ) : rows.length === 0 ? (
            <p className="text-muted-foreground text-sm">
              No mappings yet. Every completed Bolna call will still be logged
              and its summary recorded &mdash; only field write-back is off.
            </p>
          ) : (
            <table className="w-full text-sm">
              <thead>
                <tr className="text-muted-foreground text-left">
                  <th className="pb-2 font-medium">Disposition</th>
                  <th className="pb-2 font-medium">Target field</th>
                  <th className="pb-2 font-medium">Min confidence</th>
                  <th className="pb-2 font-medium">Enabled</th>
                  <th className="pb-2 font-medium" />
                </tr>
              </thead>
              <tbody>
                {rows.map((mapping) => (
                  <tr key={mapping.id} className="border-t">
                    <td className="py-2 font-medium">{mapping.disposition_name}</td>
                    <td className="py-2">{mapping.target_field_key}</td>
                    <td className="py-2">{mapping.min_confidence.toFixed(2)}</td>
                    <td className="py-2">
                      <label className="inline-flex cursor-pointer items-center gap-2">
                        <input
                          type="checkbox"
                          checked={mapping.is_enabled}
                          onChange={() => void toggle(mapping)}
                        />
                        <span>{mapping.is_enabled ? 'on' : 'off'}</span>
                      </label>
                    </td>
                    <td className="py-2 text-right">
                      <Button variant="ghost" size="sm" onClick={() => beginEdit(mapping)}>
                        Edit
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() => void del(mapping)}
                      >
                        Delete
                      </Button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={dialogOpen}
        onClose={() => setDialogOpen(false)}
        title={editing ? 'Edit mapping' : 'Add mapping'}
        description={
          editing
            ? 'Rename, retarget, or change the threshold. The mapping id is preserved.'
            : 'Any Bolna disposition that arrives on a completed call will be checked against this mapping.'
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setDialogOpen(false)}>
              Cancel
            </Button>
            <Button
              onClick={() => void submit()}
              disabled={create.isPending || update.isPending}
            >
              {editing ? 'Save' : 'Add mapping'}
            </Button>
          </>
        }
      >
        <div className="space-y-4">
          <div className="space-y-2">
            <FieldLabel htmlFor="voice-mapping-disposition">
              Bolna disposition name
            </FieldLabel>
            <Input
              id="voice-mapping-disposition"
              value={form.disposition_name}
              onChange={(event) =>
                setForm({ ...form, disposition_name: event.target.value })
              }
              placeholder="e.g. Customer Name"
            />
            <p className="text-muted-foreground text-xs">
              Exactly as it appears in Bolna. Case-sensitive.
            </p>
          </div>

          <div className="space-y-2">
            <FieldLabel htmlFor="voice-mapping-field">Target lead field</FieldLabel>
            <Select
              id="voice-mapping-field"
              value={form.target_field_key}
              onChange={(event) =>
                setForm({ ...form, target_field_key: event.target.value })
              }
            >
              <option value="">Pick a field&hellip;</option>
              {editableFields.map((field) => (
                <option key={field.id} value={field.key}>
                  {field.label} ({field.key})
                </option>
              ))}
            </Select>
          </div>

          <div className="space-y-2">
            <FieldLabel htmlFor="voice-mapping-confidence">
              Minimum confidence: {form.min_confidence.toFixed(2)}
            </FieldLabel>
            <input
              id="voice-mapping-confidence"
              type="range"
              min={0}
              max={1}
              step={0.05}
              value={form.min_confidence}
              onChange={(event) =>
                setForm({
                  ...form,
                  min_confidence: Number.parseFloat(event.target.value),
                })
              }
              className="w-full"
            />
            <p className="text-muted-foreground text-xs">
              Below this, the extracted value is not written &mdash; it appears
              on the timeline only. 0.70 is the recommended default.
            </p>
          </div>

          <div className="flex items-center gap-2">
            <input
              id="voice-mapping-enabled"
              type="checkbox"
              checked={form.is_enabled}
              onChange={(event) =>
                setForm({ ...form, is_enabled: event.target.checked })
              }
            />
            <FieldLabel htmlFor="voice-mapping-enabled">Enabled</FieldLabel>
          </div>
        </div>
      </Dialog>
    </div>
  )
}
