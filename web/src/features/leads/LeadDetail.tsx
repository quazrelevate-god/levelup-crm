/**
 * The lead detail overlay.
 *
 * Two halves: the field grid, rendered from the workspace's own schema through
 * `FieldInput`, and the timeline with its compose actions.
 *
 * The grid renders **only the fields present in `lead.values`** plus the
 * workspace's field definitions the caller can see. A field the caller lacks
 * View on never arrives in the payload, so it simply is not drawn — the UI does
 * no permission arithmetic of its own, which is what keeps it from disagreeing
 * with the server.
 */

import { useEffect, useState } from 'react'

import { ApiError } from '@/api/client'
import type {
  CallDisposition,
  CustomActionType,
  FieldTypeSpec,
  Lead,
  LeadField,
  LostReason,
  MemberDetail,
  MessageTemplate,
  RenderedTemplate,
  Stage,
  TemplateChannel,
} from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { useActionFieldTypes } from '@/features/fields/api'
import { FieldInput } from '@/features/fields/FieldInput'
import { inputTypeForWidget } from '@/features/fields/renderers'
import { toDisplayString, toDisplayStringOr } from '@/lib/format'
import {
  useAddNote,
  useLeadTimeline,
  useLogCall,
  useLogCustomAction,
  useRecordMessage,
  useRenderTemplate,
  useRefreshLeadData,
  useTriggerVoiceCall,
  useUpdateLead,
} from '@/features/leads/api'
import { LeadTimeline } from '@/features/leads/LeadTimeline'
import { LeadLabels } from '@/features/work/LeadLabels'
import { LeadTasks } from '@/features/work/LeadTasks'

/** How often the timeline re-reads while an AI call's result is pending. */
const AI_CALL_POLL_MS = 5_000
/** Longest a call plus Bolna's post-processing plausibly takes. */
const AI_CALL_POLL_LIMIT_MS = 15 * 60 * 1_000

interface LeadDetailProps {
  readonly workspaceId: string
  readonly lead: Lead
  readonly fields: readonly LeadField[]
  readonly fieldTypes: readonly FieldTypeSpec[]
  readonly stages: readonly Stage[]
  readonly lostReasons: readonly LostReason[]
  readonly members: readonly MemberDetail[]
  readonly dispositions: readonly CallDisposition[]
  readonly actionTypes: readonly CustomActionType[]
  readonly templates: readonly MessageTemplate[]
  readonly connectedCallMinSeconds: number
  readonly onClose: () => void
}

const ACTION_MESSAGES: Record<string, string> = {
  field_not_editable: 'Your permission template does not allow editing one of those fields.',
  duplicate_identity: 'Another lead already uses that identifier.',
  lost_reason_required: 'Moving to the lost stage needs a reason.',
  predated_not_allowed: 'That action type cannot be logged with a past timestamp.',
  invalid_values: 'One or more values were rejected — see the field errors.',
  voice_not_configured: 'This deployment has no Bolna credentials configured.',
  voice_agent_not_configured: 'No Bolna agent id is configured for this deployment.',
  lead_has_no_phone: 'That lead has no phone number to call.',
  insufficient_permissions: 'Your permission template does not allow placing calls.',
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  const fields = cause.detail.fields
  if (fields && typeof fields === 'object' && !Array.isArray(fields)) {
    const parts = Object.entries(fields as Record<string, string>).map(
      ([key, problem]) => `${key}: ${problem}`,
    )
    if (parts.length > 0) return parts.join(' · ')
  }
  if (Array.isArray(fields)) return `Not editable: ${(fields as string[]).join(', ')}`
  return ACTION_MESSAGES[cause.code] ?? cause.message
}

export function LeadDetail({
  workspaceId,
  lead,
  fields,
  fieldTypes,
  stages,
  lostReasons,
  members,
  dispositions,
  actionTypes,
  templates,
  connectedCallMinSeconds,
  onClose,
}: LeadDetailProps) {
  const [draft, setDraft] = useState<Record<string, unknown>>({})
  const [error, setError] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [rendered, setRendered] = useState<RenderedTemplate | null>(null)
  // Kept separate from `error` so a started call reads as a success rather
  // than sharing the panel's red alert line.
  const [voiceNotice, setVoiceNotice] = useState<string | null>(null)
  // The Bolna execution this panel is waiting on. Its result arrives on a
  // webhook minutes later, so the timeline polls until that call is logged.
  const [awaitingExecution, setAwaitingExecution] = useState<{
    readonly id: string
    readonly startedAt: number
  } | null>(null)

  const timeline = useLeadTimeline(workspaceId, lead.id, {
    refetchInterval: awaitingExecution ? AI_CALL_POLL_MS : false,
  })
  const refreshLeadData = useRefreshLeadData(workspaceId)
  const actionFieldTypes = useActionFieldTypes(workspaceId)
  const updateLead = useUpdateLead(workspaceId)
  const addNote = useAddNote(workspaceId)
  const logCall = useLogCall(workspaceId)
  const triggerVoiceCall = useTriggerVoiceCall(workspaceId)
  const logCustom = useLogCustomAction(workspaceId)
  const recordMessage = useRecordMessage(workspaceId)
  const renderTemplate = useRenderTemplate(workspaceId)

  // Reset the draft whenever a different lead is opened, so edits never bleed
  // from one record into the next.
  useEffect(() => {
    setDraft({})
    setError(null)
    setRendered(null)
    setVoiceNotice(null)
    setAwaitingExecution(null)
  }, [lead.id])

  // Stop polling once this call's AI Call log is on the timeline, or give up
  // after a bound — a call Bolna never reports must not poll forever.
  useEffect(() => {
    if (!awaitingExecution) return
    const landed = (timeline.data?.items ?? []).some(
      (action) =>
        action.kind === 'CALL_LOGGED' && action.payload.execution_id === awaitingExecution.id,
    )
    if (landed) {
      setAwaitingExecution(null)
      setVoiceNotice('AI call finished · summary added to the timeline')
      void refreshLeadData()
    } else if (Date.now() - awaitingExecution.startedAt > AI_CALL_POLL_LIMIT_MS) {
      setAwaitingExecution(null)
      setVoiceNotice('AI call started · the result will appear here once Bolna reports it')
    }
  }, [awaitingExecution, timeline.data, timeline.dataUpdatedAt, refreshLeadData])

  const rendererFor = (field: LeadField) =>
    fieldTypes.find((spec) => spec.key === field.field_type)?.renderer

  // Only fields the server actually returned values for are editable: a field
  // absent from `values` is one this caller cannot view.
  const visibleFields = fields.filter((field) => !field.is_hidden && field.key in lead.values)

  const run = async (work: Promise<unknown>) => {
    setError(null)
    try {
      await work
      return true
    } catch (cause) {
      setError(message(cause))
      return false
    }
  }

  const saveFields = async () => {
    if (Object.keys(draft).length === 0) return
    const ok = await run(updateLead.mutateAsync({ leadId: lead.id, values: draft }))
    if (ok) setDraft({})
  }

  /**
   * Hand the lead to the backend and let it call Bolna.
   *
   * The body is only `lead_id` on purpose: the server resolves the lead,
   * projects what this caller may view and builds Bolna's `user_data` itself.
   *
   * A 2xx is not success. `trigger` commits the attempt row *before* dialling
   * and reports a vendor refusal in `error` with `status: FAILED`, so the
   * response has to be read rather than assumed — otherwise a refused call
   * shows a cheerful "started" message and nobody dials anybody.
   */
  const startVoiceCall = async () => {
    setError(null)
    setVoiceNotice(null)
    try {
      const result = await triggerVoiceCall.mutateAsync({ lead_id: lead.id })
      if (result.error !== null || result.execution_id === null) {
        setError(result.error ?? 'Bolna accepted the request but returned no call id.')
        return
      }
      setVoiceNotice(
        `AI call started · ${result.bolna_status ?? result.status}` +
          (result.call_count > 0 ? ` · call ${result.call_count + 1} for this lead` : '') +
          ' · waiting for the result…',
      )
      setAwaitingExecution({ id: result.execution_id, startedAt: Date.now() })
    } catch (cause) {
      setError(message(cause))
    }
  }

  const currentStage = stages.find((stage) => stage.id === lead.stage_id)
  const isLostStage = currentStage?.kind === 'LOST'

  return (
    <aside
      className="bg-background fixed inset-y-0 right-0 z-40 w-full max-w-2xl overflow-y-auto border-l p-6 shadow-xl"
      aria-label="Lead detail"
      data-testid="lead-detail"
    >
      <header className="mb-4 flex items-start justify-between gap-4">
        <div>
          <h2 className="text-lg font-semibold">
            {toDisplayStringOr(lead.primary.h1, lead.identity_value)}
          </h2>
          <p className="text-muted-foreground text-sm">
            {toDisplayString(lead.primary.h2)} · score {lead.score}
          </p>
        </div>
        <Button variant="ghost" size="sm" onClick={onClose}>
          Close
        </Button>
      </header>

      {error ? (
        <p role="alert" className="text-destructive mb-3 text-sm">
          {error}
        </p>
      ) : null}

      <section className="mb-6 space-y-3">
        <div className="grid gap-3 sm:grid-cols-2">
          <div className="space-y-1.5">
            <Label htmlFor="lead-stage">Stage</Label>
            <Select
              id="lead-stage"
              value={lead.stage_id ?? ''}
              onChange={(event) =>
                void run(updateLead.mutateAsync({ leadId: lead.id, stage_id: event.target.value }))
              }
            >
              {stages.map((stage) => (
                <option key={stage.id} value={stage.id}>
                  {stage.label}
                </option>
              ))}
            </Select>
          </div>

          {isLostStage ? (
            <div className="space-y-1.5">
              <Label htmlFor="lead-lost-reason">Lost reason</Label>
              <Select
                id="lead-lost-reason"
                value={lead.lost_reason_id ?? ''}
                onChange={(event) =>
                  void run(
                    updateLead.mutateAsync({
                      leadId: lead.id,
                      stage_id: lead.stage_id,
                      lost_reason_id: event.target.value,
                    }),
                  )
                }
              >
                {lostReasons.map((reason) => (
                  <option key={reason.id} value={reason.id}>
                    {reason.label}
                  </option>
                ))}
              </Select>
            </div>
          ) : null}

          <div className="space-y-1.5">
            <Label htmlFor="lead-assignee">Assignee</Label>
            <Select
              id="lead-assignee"
              value={lead.assignee_id ?? ''}
              onChange={(event) =>
                void run(
                  updateLead.mutateAsync({ leadId: lead.id, assignee_id: event.target.value }),
                )
              }
            >
              <option value="">Unassigned</option>
              {members.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.user.full_name}
                </option>
              ))}
            </Select>
          </div>

          <div className="space-y-1.5">
            <Label htmlFor="lead-rating">Rating</Label>
            <Select
              id="lead-rating"
              value={lead.rating === null ? '' : String(lead.rating)}
              onChange={(event) =>
                void run(
                  updateLead.mutateAsync({
                    leadId: lead.id,
                    rating: event.target.value === '' ? null : Number(event.target.value),
                  }),
                )
              }
            >
              <option value="">None</option>
              {[1, 2, 3, 4, 5].map((value) => (
                <option key={value} value={value}>
                  {value}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </section>

      <section className="mb-6 space-y-3 border-t pt-4">
        <div className="flex items-center justify-between">
          <h3 className="text-sm font-medium">Fields</h3>
          <Button
            size="sm"
            disabled={Object.keys(draft).length === 0 || updateLead.isPending}
            onClick={() => void saveFields()}
          >
            Save {Object.keys(draft).length > 0 ? `(${Object.keys(draft).length})` : ''}
          </Button>
        </div>

        {visibleFields.map((field) => {
          const renderer = rendererFor(field)
          if (!renderer) return null
          const value = field.key in draft ? draft[field.key] : lead.values[field.key]
          return (
            <div key={field.id} className="space-y-1.5" data-testid="lead-field">
              <Label htmlFor={`field-${field.key}`}>
                {field.label}
                {field.is_required ? <span className="text-destructive"> *</span> : null}
                {field.lock_after_create ? (
                  <Badge variant="outline" className="ml-2">
                    locked
                  </Badge>
                ) : null}
              </Label>
              <FieldInput
                field={field}
                renderer={renderer}
                inputId={`field-${field.key}`}
                value={value}
                disabled={field.lock_after_create}
                onChange={(next) => setDraft((current) => ({ ...current, [field.key]: next }))}
              />
            </div>
          )
        })}
        {visibleFields.length === 0 ? (
          <p className="text-muted-foreground text-sm">
            {/* Two very different situations look identical here, and saying
                the wrong one is worse than saying nothing: before the schema
                query resolves `fields` is empty too, and blaming a permission
                template for a request still in flight sends somebody to the
                permissions screen to fix a problem that does not exist. */}
            {fields.length === 0
              ? 'Loading fields…'
              : 'No fields are visible to your permission template.'}
          </p>
        ) : null}
      </section>

      <section className="mb-6 space-y-3 border-t pt-4">
        <h3 className="text-sm font-medium">Log activity</h3>

        <div className="space-y-2">
          <Button
            size="sm"
            variant="outline"
            disabled={triggerVoiceCall.isPending}
            onClick={() => void startVoiceCall()}
          >
            {triggerVoiceCall.isPending ? 'Starting AI call…' : '🤖 AI Call'}
          </Button>
          {voiceNotice ? (
            <p role="status" className="text-muted-foreground text-sm">
              {voiceNotice}
            </p>
          ) : null}
          <p className="text-muted-foreground text-xs">
            Calls this lead with the configured voice agent. The outcome and any corrected details
            arrive on the timeline once the call ends.
          </p>
        </div>

        <div className="space-y-2">
          <Textarea
            rows={2}
            placeholder="Add a note…"
            aria-label="Note body"
            value={note}
            onChange={(event) => setNote(event.target.value)}
          />
          <Button
            size="sm"
            variant="outline"
            disabled={note.trim() === ''}
            onClick={() => {
              void (async () => {
                const ok = await run(addNote.mutateAsync({ leadId: lead.id, body: note.trim() }))
                if (ok) setNote('')
              })()
            }}
          >
            Add note
          </Button>
        </div>

        <CallLogger
          dispositions={dispositions}
          connectedCallMinSeconds={connectedCallMinSeconds}
          onSubmit={(payload) => run(logCall.mutateAsync({ leadId: lead.id, ...payload }))}
        />

        {actionTypes.length > 0 ? (
          <CustomActionLogger
            actionTypes={actionTypes}
            actionFieldTypes={actionFieldTypes.data ?? []}
            onSubmit={(payload) => run(logCustom.mutateAsync({ leadId: lead.id, ...payload }))}
          />
        ) : null}

        <TemplateComposer
          templates={templates}
          rendered={rendered}
          onRender={async (templateId) => {
            setError(null)
            try {
              setRendered(await renderTemplate.mutateAsync({ templateId, leadId: lead.id }))
            } catch (cause) {
              setError(message(cause))
            }
          }}
          onRecord={(channel, body, templateId) =>
            run(
              recordMessage.mutateAsync({
                leadId: lead.id,
                channel,
                body,
                template_id: templateId,
              }),
            )
          }
        />
      </section>

      <LeadLabels workspaceId={workspaceId} leadId={lead.id} />

      <LeadTasks workspaceId={workspaceId} leadId={lead.id} />

      <section className="border-t pt-4">
        <h3 className="mb-3 text-sm font-medium">Timeline</h3>
        <LeadTimeline
          actions={timeline.data?.items ?? []}
          stages={stages}
          members={members}
          dispositions={dispositions}
          actionTypes={actionTypes}
        />
      </section>
    </aside>
  )
}

/**
 * Manual call logging. There is no telephony in v1 — this records what a human
 * says happened, with the workspace's default disposition preselected once the
 * duration passes its connected threshold.
 */
function CallLogger({
  dispositions,
  connectedCallMinSeconds,
  onSubmit,
}: {
  dispositions: readonly CallDisposition[]
  connectedCallMinSeconds: number
  onSubmit: (payload: {
    direction: string
    disposition_id: string
    duration_seconds: number
    notes: string | null
  }) => Promise<boolean>
}) {
  const [open, setOpen] = useState(false)
  const [direction, setDirection] = useState('OUTGOING')
  const [duration, setDuration] = useState('0')
  const [dispositionId, setDispositionId] = useState('')

  const defaultDisposition = dispositions.find((entry) => entry.is_default)
  const seconds = Number(duration) || 0
  // The rule from §3: past the threshold, the default is what happened.
  const suggested = seconds > connectedCallMinSeconds ? defaultDisposition?.id : undefined
  const effective = dispositionId || suggested || dispositions[0]?.id || ''

  if (!open) {
    return (
      <Button size="sm" variant="outline" onClick={() => setOpen(true)}>
        Log a call
      </Button>
    )
  }

  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="call-logger">
      <div className="grid gap-2 sm:grid-cols-3">
        <Select
          aria-label="Call direction"
          value={direction}
          onChange={(event) => setDirection(event.target.value)}
        >
          <option value="OUTGOING">Outgoing</option>
          <option value="INCOMING">Incoming</option>
          <option value="MISSED">Missed</option>
        </Select>
        <Input
          type="number"
          min={0}
          aria-label="Duration in seconds"
          placeholder="Seconds"
          value={duration}
          onChange={(event) => setDuration(event.target.value)}
        />
        <Select
          aria-label="Call outcome"
          value={effective}
          onChange={(event) => setDispositionId(event.target.value)}
        >
          {dispositions.map((entry) => (
            <option key={entry.id} value={entry.id}>
              {entry.label}
              {entry.is_default ? ' (default)' : ''}
            </option>
          ))}
        </Select>
      </div>
      {suggested && !dispositionId ? (
        <p className="text-muted-foreground text-xs">
          Past {connectedCallMinSeconds}s, so the default outcome is preselected.
        </p>
      ) : null}
      <div className="flex gap-2">
        <Button
          size="sm"
          disabled={effective === ''}
          onClick={() => {
            void (async () => {
              const ok = await onSubmit({
                direction,
                disposition_id: effective,
                duration_seconds: seconds,
                notes: null,
              })
              if (ok) {
                setOpen(false)
                setDuration('0')
                setDispositionId('')
              }
            })()
          }}
        >
          Save call
        </Button>
        <Button size="sm" variant="ghost" onClick={() => setOpen(false)}>
          Cancel
        </Button>
      </div>
    </div>
  )
}

/**
 * A custom action's form, generated from its own `action_fields`.
 *
 * The inputs are deliberately simple text/number/date controls rather than the
 * lead `FieldInput`: action fields use a different, smaller registry, and
 * reusing the lead renderer would imply the two type sets are the same.
 */
function CustomActionLogger({
  actionTypes,
  actionFieldTypes,
  onSubmit,
}: {
  actionTypes: readonly CustomActionType[]
  /** The 8-type action registry, so this form draws what the server declares. */
  actionFieldTypes: readonly FieldTypeSpec[]
  onSubmit: (payload: {
    action_type_id: string
    values: Record<string, unknown>
  }) => Promise<boolean>
}) {
  const [typeId, setTypeId] = useState('')
  const [values, setValues] = useState<Record<string, unknown>>({})

  const selected = actionTypes.find((entry) => entry.id === typeId)

  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="custom-action-logger">
      <Select
        aria-label="Custom action type"
        value={typeId}
        onChange={(event) => {
          setTypeId(event.target.value)
          setValues({})
        }}
      >
        <option value="">Log a custom action…</option>
        {actionTypes.map((entry) => (
          <option key={entry.id} value={entry.id}>
            {entry.name} ({entry.score >= 0 ? '+' : ''}
            {entry.score})
          </option>
        ))}
      </Select>

      {selected
        ? selected.fields
            .filter((field) => !field.is_hidden)
            .map((field) => (
              <div key={field.id} className="space-y-1">
                <Label htmlFor={`action-${field.key}`}>
                  {field.label}
                  {field.is_required ? <span className="text-destructive"> *</span> : null}
                </Label>
                {field.options.length > 0 ? (
                  <Select
                    id={`action-${field.key}`}
                    value={toDisplayString(values[field.key])}
                    onChange={(event) =>
                      setValues((current) => ({ ...current, [field.key]: event.target.value }))
                    }
                  >
                    <option value="">Select…</option>
                    {field.options.map((option) => (
                      <option key={option.id} value={option.code}>
                        {option.label}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input
                    id={`action-${field.key}`}
                    // The widget comes from the action registry the server
                    // serves; this file names no action field type.
                    type={inputTypeForWidget(
                      actionFieldTypes.find((spec) => spec.key === field.field_type)?.renderer
                        .widget ?? 'text',
                    )}
                    value={toDisplayString(values[field.key])}
                    onChange={(event) =>
                      setValues((current) => ({ ...current, [field.key]: event.target.value }))
                    }
                  />
                )}
              </div>
            ))
        : null}

      {selected ? (
        <Button
          size="sm"
          onClick={() => {
            void (async () => {
              const ok = await onSubmit({ action_type_id: selected.id, values })
              if (ok) {
                setTypeId('')
                setValues({})
              }
            })()
          }}
        >
          Log {selected.name}
        </Button>
      ) : null}
    </div>
  )
}

/**
 * The compose flow.
 *
 * Rendering happens server-side so a template cannot read a field the sender
 * lacks View on. Unresolved placeholders are surfaced rather than hidden — a
 * rep about to send "Hi , your renewal is due" needs to know before they do.
 *
 * Recording a send records that a message was *composed*. Nothing here implies
 * delivery, because nothing in v1 delivers anything.
 */
function TemplateComposer({
  templates,
  rendered,
  onRender,
  onRecord,
}: {
  templates: readonly MessageTemplate[]
  rendered: RenderedTemplate | null
  onRender: (templateId: string) => Promise<void>
  onRecord: (channel: TemplateChannel, body: string, templateId: string | null) => Promise<boolean>
}) {
  const [templateId, setTemplateId] = useState('')

  if (templates.length === 0) {
    return (
      <p className="text-muted-foreground text-sm">
        No message templates yet — create one under Templates.
      </p>
    )
  }

  return (
    <div className="space-y-2 rounded-md border p-3" data-testid="template-composer">
      <div className="flex gap-2">
        <Select
          aria-label="Message template"
          value={templateId}
          onChange={(event) => setTemplateId(event.target.value)}
        >
          <option value="">Compose from a template…</option>
          {templates.map((template) => (
            <option key={template.id} value={template.id}>
              {template.channel} · {template.name}
            </option>
          ))}
        </Select>
        <Button
          size="sm"
          variant="outline"
          disabled={templateId === ''}
          onClick={() => void onRender(templateId)}
        >
          Preview
        </Button>
      </div>

      {rendered ? (
        <div className="space-y-2" data-testid="template-preview">
          {rendered.subject ? <p className="text-sm font-medium">{rendered.subject}</p> : null}
          <pre className="bg-muted rounded-md p-2 text-sm whitespace-pre-wrap">{rendered.body}</pre>
          {rendered.unresolved.length > 0 ? (
            <p className="text-destructive text-xs" data-testid="unresolved-placeholders">
              Unresolved: {rendered.unresolved.join(', ')} — either the lead has no value, or your
              template cannot view that field.
            </p>
          ) : null}
          <Button
            size="sm"
            onClick={() => void onRecord(rendered.channel, rendered.body, rendered.id)}
          >
            Record as sent
          </Button>
        </div>
      ) : null}
    </div>
  )
}
