/**
 * The lead timeline.
 *
 * The timeline is the audit trail, so every entry renders what actually
 * happened rather than a generic "updated". A `FIELD_CHANGE` shows old and new;
 * a `STAGE_CHANGE` resolves both stage ids to their labels — which is why this
 * component takes the taxonomy in rather than fetching it per row.
 *
 * Kind and actor filters are here because a fifty-entry timeline is unreadable
 * without them, and both are client-side: the whole list already arrived.
 */

import { useMemo, useState } from 'react'

import type {
  CallDisposition,
  CustomActionType,
  LeadAction,
  MemberDetail,
  Stage,
} from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Select } from '@/components/ui/select'
import { formatCallDuration, formatCallStatus } from '@/features/calls/format'
import { toDisplayString, toDisplayStringOr } from '@/lib/format'

interface LeadTimelineProps {
  readonly actions: readonly LeadAction[]
  readonly stages: readonly Stage[]
  readonly members: readonly MemberDetail[]
  readonly dispositions: readonly CallDisposition[]
  readonly actionTypes: readonly CustomActionType[]
}

/** Human labels for the product's own event kinds. */
const KIND_LABELS: Record<string, string> = {
  LEAD_CREATED: 'Lead created',
  FIELD_CHANGE: 'Field changed',
  STAGE_CHANGE: 'Stage changed',
  ASSIGNMENT_CHANGE: 'Reassigned',
  RATING_CHANGE: 'Rating changed',
  NOTE: 'Note',
  CALL_LOGGED: 'Call logged',
  WHATSAPP_SENT: 'WhatsApp composed',
  EMAIL_SENT: 'Email composed',
  SMS_SENT: 'SMS composed',
  TASK_CREATED: 'Task created',
  TASK_COMPLETED: 'Task completed',
  CUSTOM: 'Custom action',
}

function renderValue(value: unknown): string {
  return toDisplayStringOr(value)
}

/**
 * A call the Bolna voice agent made, logged by the post-call webhook. Same
 * `CALL_LOGGED` kind as a human's log — reports count both — told apart only by
 * `payload.source`, which the server sets and a person's log never carries.
 */
function isAiCall(action: LeadAction): boolean {
  return action.kind === 'CALL_LOGGED' && action.payload.source === 'AI_CALL'
}

export function LeadTimeline({
  actions,
  stages,
  members,
  dispositions,
  actionTypes,
}: LeadTimelineProps) {
  const [kindFilter, setKindFilter] = useState('')
  const [actorFilter, setActorFilter] = useState('')

  const stageLabel = (id: unknown) =>
    stages.find((stage) => stage.id === id)?.label ?? (id ? 'a removed stage' : '—')
  const memberName = (id: string | null) =>
    members.find((member) => member.id === id)?.user.full_name ?? 'System'

  const visible = useMemo(
    () =>
      actions.filter((action) => {
        if (kindFilter && action.kind !== kindFilter) return false
        if (actorFilter && action.actor_id !== actorFilter) return false
        return true
      }),
    [actions, kindFilter, actorFilter],
  )

  const kinds = useMemo(() => Array.from(new Set(actions.map((action) => action.kind))), [actions])

  const describe = (action: LeadAction) => {
    const payload = action.payload

    if (action.kind === 'FIELD_CHANGE') {
      return (
        <>
          <span className="font-medium">{toDisplayString(payload.label ?? payload.field_key)}</span>{' '}
          <span className="text-muted-foreground line-through">{renderValue(payload.old)}</span> →{' '}
          <span className="font-medium">{renderValue(payload.new)}</span>
        </>
      )
    }
    if (action.kind === 'STAGE_CHANGE') {
      return (
        <>
          <span className="text-muted-foreground">{stageLabel(payload.old_stage_id)}</span> →{' '}
          <span className="font-medium">{stageLabel(payload.new_stage_id)}</span>
        </>
      )
    }
    if (action.kind === 'ASSIGNMENT_CHANGE') {
      return (
        <>
          <span className="text-muted-foreground">
            {payload.old_assignee_id
              ? memberName(toDisplayString(payload.old_assignee_id))
              : 'Unassigned'}
          </span>{' '}
          →{' '}
          <span className="font-medium">
            {payload.new_assignee_id
              ? memberName(toDisplayString(payload.new_assignee_id))
              : 'Unassigned'}
          </span>
        </>
      )
    }
    if (isAiCall(action)) {
      const disposition = dispositions.find((entry) => entry.id === payload.disposition_id)
      return (
        <div className="space-y-1" data-testid="ai-call-entry">
          <p className="text-muted-foreground text-xs">
            Made by AI Call · Call data received
            {payload.summary_source === 'FALLBACK' ? ' · no automatic summary' : ''}
          </p>
          {action.body ? <p className="whitespace-pre-wrap">{action.body}</p> : null}
          <p className="text-muted-foreground text-xs">
            Duration: {formatCallDuration(payload.duration_seconds)} · Status:{' '}
            {formatCallStatus(payload.call_status)}
            {disposition ? ` · ${disposition.label}` : ''}
          </p>
        </div>
      )
    }
    if (action.kind === 'CALL_LOGGED') {
      const disposition = dispositions.find((entry) => entry.id === payload.disposition_id)
      return (
        <>
          {toDisplayString(payload.direction).toLowerCase()} ·{' '}
          <span className="font-medium">{disposition?.label ?? 'unknown outcome'}</span> ·{' '}
          {toDisplayStringOr(payload.duration_seconds, '0')}s
        </>
      )
    }
    if (action.kind === 'CUSTOM') {
      const type = actionTypes.find((entry) => entry.id === action.action_type_id)
      const entries = Object.entries(payload).filter(([, value]) => value !== null)
      return (
        <>
          <span className="font-medium">{type?.name ?? 'Custom action'}</span>
          {entries.length > 0 ? (
            <span className="text-muted-foreground">
              {' '}
              — {entries.map(([key, value]) => `${key}: ${renderValue(value)}`).join(', ')}
            </span>
          ) : null}
        </>
      )
    }
    return <span className="text-muted-foreground">{action.body ?? '—'}</span>
  }

  return (
    <div className="space-y-3" data-testid="lead-timeline">
      <div className="flex flex-wrap gap-2">
        <Select
          className="max-w-44"
          aria-label="Filter timeline by kind"
          value={kindFilter}
          onChange={(event) => setKindFilter(event.target.value)}
        >
          <option value="">All events</option>
          {kinds.map((kind) => (
            <option key={kind} value={kind}>
              {KIND_LABELS[kind] ?? kind}
            </option>
          ))}
        </Select>
        <Select
          className="max-w-44"
          aria-label="Filter timeline by actor"
          value={actorFilter}
          onChange={(event) => setActorFilter(event.target.value)}
        >
          <option value="">Anyone</option>
          {members.map((member) => (
            <option key={member.id} value={member.id}>
              {member.user.full_name}
            </option>
          ))}
        </Select>
      </div>

      {visible.length === 0 ? (
        <p className="text-muted-foreground text-sm">Nothing on the timeline yet.</p>
      ) : (
        <ol className="space-y-2">
          {visible.map((action) => (
            <li
              key={action.id}
              className="rounded-md border p-3 text-sm"
              data-testid="timeline-entry"
            >
              <div className="mb-1 flex flex-wrap items-center gap-2">
                <Badge variant="outline">
                  {isAiCall(action) ? '🤖 AI Call' : (KIND_LABELS[action.kind] ?? action.kind)}
                </Badge>
                <span className="text-muted-foreground text-xs">
                  {new Date(action.performed_at).toLocaleString()} ·{' '}
                  {isAiCall(action) ? 'AI Call' : memberName(action.actor_id)}
                </span>
                {action.score_applied !== 0 ? (
                  <Badge variant={action.score_applied > 0 ? 'success' : 'destructive'}>
                    {action.score_applied > 0 ? '+' : ''}
                    {action.score_applied}
                  </Badge>
                ) : null}
                {action.changeset_id ? (
                  <span
                    className="text-muted-foreground font-mono text-[10px]"
                    title="Every action carries the changeset it belongs to, which is what makes undo possible"
                  >
                    cs:{action.changeset_id.slice(0, 8)}
                  </span>
                ) : null}
              </div>
              <div>{describe(action)}</div>
              {action.body && action.kind !== 'CALL_LOGGED' ? (
                <p className="text-muted-foreground mt-1 whitespace-pre-wrap">{action.body}</p>
              ) : null}
            </li>
          ))}
        </ol>
      )}
    </div>
  )
}
