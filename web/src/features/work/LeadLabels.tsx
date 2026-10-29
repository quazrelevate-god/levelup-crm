/**
 * Labels on one lead (M7).
 *
 * Deliberately not a `TAGS` field. A field is part of the schema an admin
 * designed and is subject to the field matrix; a label is something a caller
 * sticks on in the moment. Both exist in the source product, and conflating
 * them would mean either putting ad-hoc tags through the permission matrix or
 * letting anyone add options to a designed field.
 *
 * So this sits beside the fields rather than among them, and reads as
 * chips-you-toggle rather than a form control.
 */

import { useState } from 'react'
import { Tag } from 'lucide-react'

import type { Label } from '@/api/types'
import { DetailSection } from '@/components/ui/detail-section'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Input } from '@/components/ui/input'
import {
  useAttachLabel,
  useCreateLabel,
  useDetachLabel,
  useLabels,
  useLeadLabels,
} from '@/features/work/api'

export interface LeadLabelsProps {
  readonly workspaceId: string
  readonly leadId: string
  /** Band colour in the lead panel's alternating sections. */
  readonly tone?: 'a' | 'b'
}

export function LeadLabels({ workspaceId, leadId, tone = 'a' }: LeadLabelsProps) {
  const all = useLabels(workspaceId)
  const applied = useLeadLabels(workspaceId, leadId)
  const attach = useAttachLabel(workspaceId)
  const detach = useDetachLabel(workspaceId)
  const create = useCreateLabel(workspaceId)

  const [adding, setAdding] = useState(false)
  const [name, setName] = useState('')

  const appliedIds = new Set((applied.data ?? []).map((label) => label.id))
  const available = (all.data ?? []).filter((label) => !appliedIds.has(label.id))

  function toggle(label: Label, on: boolean) {
    void (
      on
        ? attach.mutateAsync({ leadId, labelId: label.id })
        : detach.mutateAsync({ leadId, labelId: label.id })
    ).then(() => applied.refetch())
  }

  return (
    <DetailSection title="Labels" icon={Tag} tone={tone}>
      <div className="flex flex-wrap items-center gap-2">
        {(applied.data ?? []).map((label) => (
          <Badge
            key={label.id}
            variant="outline"
            style={label.color ? { borderColor: label.color, color: label.color } : undefined}
          >
            {label.name}
            <button
              type="button"
              className="ml-1 opacity-60 hover:opacity-100"
              aria-label={`Remove ${label.name}`}
              onClick={() => toggle(label, false)}
            >
              ×
            </button>
          </Badge>
        ))}

        {(applied.data ?? []).length === 0 ? (
          <span className="text-muted-foreground text-sm">None yet.</span>
        ) : null}
      </div>

      {available.length > 0 ? (
        <div className="flex flex-wrap gap-1.5 pt-1">
          {available.map((label) => (
            <button
              key={label.id}
              type="button"
              className="hover:bg-muted rounded-full border px-2 py-0.5 text-xs"
              onClick={() => toggle(label, true)}
            >
              + {label.name}
            </button>
          ))}
        </div>
      ) : null}

      {adding ? (
        <div className="flex gap-2 pt-1">
          <Input
            className="max-w-48"
            aria-label="New label name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Label name"
          />
          <Button
            size="sm"
            disabled={!name.trim() || create.isPending}
            onClick={() => {
              void (async () => {
                const created = await create.mutateAsync({ name: name.trim() })
                await attach.mutateAsync({ leadId, labelId: created.id })
                await applied.refetch()
                setName('')
                setAdding(false)
              })()
            }}
          >
            Add
          </Button>
          <Button size="sm" variant="ghost" onClick={() => setAdding(false)}>
            Cancel
          </Button>
        </div>
      ) : (
        <Button size="sm" variant="ghost" onClick={() => setAdding(true)}>
          New label
        </Button>
      )}
    </DetailSection>
  )
}
