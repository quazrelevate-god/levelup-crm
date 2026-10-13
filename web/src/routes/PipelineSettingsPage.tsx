/**
 * Pipeline and taxonomy settings (docs/03-configuration-model.md §2, §3, §5).
 *
 * The stage screen is the three-column pipeline from §2.1 rather than a flat
 * list, because the shape *is* the rule: one initial stage, a reorderable band,
 * and a closed pair. Adding a stage only ever adds to the middle column, which
 * is why there is no kind picker — a second Won stage is not something the UI
 * should let someone try and then be refused.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { CallDisposition, LostReason, Stage } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/features/auth/context'
import { usePermissions } from '@/features/auth/usePermissions'
import {
  useArchiveDisposition,
  useArchiveLostReason,
  useArchiveStage,
  useCreateDisposition,
  useCreateLostReason,
  useCreateStage,
  useDispositions,
  useLostReasons,
  usePreferences,
  useReorderStages,
  useSetDefaultDisposition,
  useStages,
  useUpdateDisposition,
  useUpdateLostReason,
  useUpdatePreferences,
  useUpdateStage,
} from '@/features/pipeline/api'

const ACTION_MESSAGES: Record<string, string> = {
  stage_cardinality: 'A pipeline keeps exactly one initial, won and lost stage.',
  lost_reason_limit: 'A workspace can hold 25 lost reasons. Archive one first.',
  system_disposition: 'System statuses cannot be renamed. Archive it instead.',
  default_disposition: 'Make another status the default before archiving this one.',
  stage_not_reorderable: 'Only the active stages can be reordered.',
  unknown_timezone: 'That is not an IANA timezone, e.g. Europe/London.',
  unknown_feature: 'This product has no such feature flag.',
  insufficient_permissions: 'Your permission template does not allow changing settings.',
}

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  return ACTION_MESSAGES[cause.code] ?? cause.message
}

function StageChip({
  stage,
  canEdit,
  onRename,
  onRecolour,
}: {
  stage: Stage
  canEdit: boolean
  onRename: (label: string) => void
  onRecolour: (color: string) => void
}) {
  return (
    <div className="flex items-center gap-2 rounded-md border px-2 py-1.5" data-testid="stage-chip">
      <input
        type="color"
        className="size-6 shrink-0 cursor-pointer rounded border-0 bg-transparent p-0"
        aria-label={`Colour for ${stage.label}`}
        value={stage.color}
        disabled={!canEdit}
        onChange={(event) => onRecolour(event.target.value)}
      />
      <Input
        className="h-7 flex-1"
        maxLength={28}
        aria-label={`Name for ${stage.label}`}
        defaultValue={stage.label}
        disabled={!canEdit}
        onBlur={(event) => {
          if (event.target.value !== stage.label) onRename(event.target.value)
        }}
      />
    </div>
  )
}

export function PipelineSettingsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const { isAdmin } = usePermissions()

  const [error, setError] = useState<string | null>(null)
  const [newStage, setNewStage] = useState('')
  const [newReason, setNewReason] = useState('')
  const [newDisposition, setNewDisposition] = useState('')
  const [showArchived, setShowArchived] = useState(false)

  const stages = useStages(workspaceId)
  const reasons = useLostReasons(workspaceId, showArchived)
  const dispositions = useDispositions(workspaceId, showArchived)
  const preferences = usePreferences(workspaceId)

  const createStage = useCreateStage(workspaceId)
  const updateStage = useUpdateStage(workspaceId)
  const archiveStage = useArchiveStage(workspaceId)
  const reorderStages = useReorderStages(workspaceId)
  const createReason = useCreateLostReason(workspaceId)
  const updateReason = useUpdateLostReason(workspaceId)
  const archiveReason = useArchiveLostReason(workspaceId)
  const createDisposition = useCreateDisposition(workspaceId)
  const updateDisposition = useUpdateDisposition(workspaceId)
  const setDefault = useSetDefaultDisposition(workspaceId)
  const archiveDisposition = useArchiveDisposition(workspaceId)
  const updatePreferences = useUpdatePreferences(workspaceId)

  const run = async (work: Promise<unknown>) => {
    setError(null)
    try {
      await work
    } catch (cause) {
      setError(message(cause))
    }
  }

  const pipeline = stages.data
  const active = pipeline?.active ?? []

  const moveStage = (stage: Stage, delta: number) => {
    const index = active.findIndex((candidate) => candidate.id === stage.id)
    const target = index + delta
    if (target < 0 || target >= active.length) return
    const ordered = active.map((entry, position) => {
      if (position === index) return active[target] as Stage
      if (position === target) return active[index] as Stage
      return entry
    })
    void run(reorderStages.mutateAsync({ orderedIds: ordered.map((entry) => entry.id) }))
  }

  const liveReasons = (reasons.data ?? []).filter((reason) => !reason.is_archived)

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Pipeline and taxonomy</h1>
          <p className="text-muted-foreground text-sm">
            Stages, lost reasons and call outcomes — all of them this workspace&rsquo;s own words.
          </p>
        </div>
        <label className="flex items-center gap-2 text-sm">
          <Checkbox
            checked={showArchived}
            onChange={(event) => setShowArchived(event.target.checked)}
          />
          Show archived
        </label>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Lead stages</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="grid gap-4 lg:grid-cols-3">
            <section className="space-y-2">
              <h3 className="text-muted-foreground text-xs font-medium uppercase">Initial stage</h3>
              {pipeline?.initial ? (
                <StageChip
                  stage={pipeline.initial}
                  canEdit={isAdmin}
                  onRename={(label) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.initial!.id, label }))
                  }
                  onRecolour={(color) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.initial!.id, color }))
                  }
                />
              ) : null}
              <p className="text-muted-foreground text-xs">
                Exactly one. Renameable, never removed.
              </p>
            </section>

            <section className="space-y-2">
              <h3 className="text-muted-foreground text-xs font-medium uppercase">
                Active stages ({active.length})
              </h3>
              {active.map((stage) => (
                <div key={stage.id} className="flex items-center gap-1">
                  <div className="flex-1">
                    <StageChip
                      stage={stage}
                      canEdit={isAdmin}
                      onRename={(label) =>
                        void run(updateStage.mutateAsync({ stageId: stage.id, label }))
                      }
                      onRecolour={(color) =>
                        void run(updateStage.mutateAsync({ stageId: stage.id, color }))
                      }
                    />
                  </div>
                  {isAdmin ? (
                    <>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Move ${stage.label} up`}
                        onClick={() => moveStage(stage, -1)}
                      >
                        ↑
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Move ${stage.label} down`}
                        onClick={() => moveStage(stage, 1)}
                      >
                        ↓
                      </Button>
                      <Button
                        variant="ghost"
                        size="sm"
                        aria-label={`Archive ${stage.label}`}
                        onClick={() => void run(archiveStage.mutateAsync({ stageId: stage.id }))}
                      >
                        ✕
                      </Button>
                    </>
                  ) : null}
                </div>
              ))}
              {isAdmin ? (
                <div className="flex gap-2">
                  <Input
                    className="flex-1"
                    maxLength={28}
                    placeholder="New stage"
                    aria-label="New stage name"
                    value={newStage}
                    onChange={(event) => setNewStage(event.target.value)}
                  />
                  <Button
                    size="sm"
                    disabled={newStage.trim() === ''}
                    onClick={() => {
                      void (async () => {
                        await run(createStage.mutateAsync({ label: newStage.trim() }))
                        setNewStage('')
                      })()
                    }}
                  >
                    Add
                  </Button>
                </div>
              ) : null}
            </section>

            <section className="space-y-2">
              <h3 className="text-muted-foreground text-xs font-medium uppercase">Closed stages</h3>
              {pipeline?.won ? (
                <StageChip
                  stage={pipeline.won}
                  canEdit={isAdmin}
                  onRename={(label) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.won!.id, label }))
                  }
                  onRecolour={(color) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.won!.id, color }))
                  }
                />
              ) : null}
              {pipeline?.lost ? (
                <StageChip
                  stage={pipeline.lost}
                  canEdit={isAdmin}
                  onRename={(label) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.lost!.id, label }))
                  }
                  onRecolour={(color) =>
                    void run(updateStage.mutateAsync({ stageId: pipeline.lost!.id, color }))
                  }
                />
              ) : null}
              <p className="text-muted-foreground text-xs">
                One won, one lost. A pipeline with neither is not a pipeline.
              </p>
            </section>
          </div>

          {showArchived && (pipeline?.archived.length ?? 0) > 0 ? (
            <div className="mt-4 border-t pt-3">
              <h3 className="text-muted-foreground mb-2 text-xs font-medium uppercase">
                Deleted statuses ({pipeline?.archived.length})
              </h3>
              <div className="flex flex-wrap gap-2">
                {pipeline?.archived.map((stage) => (
                  <Badge key={stage.id} variant="secondary">
                    {stage.label}
                  </Badge>
                ))}
              </div>
              <p className="text-muted-foreground mt-2 text-xs">
                Leads that reached these keep them — archiving removes a stage from the picker, not
                from history.
              </p>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <div className="grid gap-6 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="text-base">
              Lost reasons{' '}
              <span className="text-muted-foreground font-normal">({liveReasons.length}/25)</span>
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {(reasons.data ?? []).map((reason: LostReason) => (
              <div key={reason.id} className="flex items-center gap-2">
                <Input
                  className="h-8 flex-1"
                  aria-label={`Lost reason ${reason.label}`}
                  defaultValue={reason.label}
                  disabled={!isAdmin || reason.is_archived}
                  onBlur={(event) => {
                    if (event.target.value !== reason.label) {
                      void run(
                        updateReason.mutateAsync({
                          reasonId: reason.id,
                          label: event.target.value,
                        }),
                      )
                    }
                  }}
                />
                {reason.is_default ? <Badge variant="outline">default</Badge> : null}
                {reason.is_archived ? <Badge variant="secondary">archived</Badge> : null}
                {isAdmin && !reason.is_archived ? (
                  <Button
                    variant="ghost"
                    size="sm"
                    aria-label={`Archive ${reason.label}`}
                    onClick={() => void run(archiveReason.mutateAsync({ reasonId: reason.id }))}
                  >
                    ✕
                  </Button>
                ) : null}
              </div>
            ))}
            {isAdmin ? (
              <div className="flex gap-2 pt-2">
                <Input
                  className="flex-1"
                  placeholder="New lost reason"
                  aria-label="New lost reason"
                  value={newReason}
                  onChange={(event) => setNewReason(event.target.value)}
                />
                <Button
                  size="sm"
                  disabled={newReason.trim() === ''}
                  onClick={() => {
                    void (async () => {
                      await run(createReason.mutateAsync({ label: newReason.trim() }))
                      setNewReason('')
                    })()
                  }}
                >
                  Add
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="text-base">Call feedback</CardTitle>
          </CardHeader>
          <CardContent className="space-y-2">
            {(dispositions.data ?? []).map((disposition: CallDisposition) => (
              <div key={disposition.id} className="flex items-center gap-2">
                <Input
                  className="h-8 flex-1"
                  aria-label={`Disposition ${disposition.label}`}
                  defaultValue={disposition.label}
                  disabled={!isAdmin || disposition.is_system || disposition.is_archived}
                  onBlur={(event) => {
                    if (event.target.value !== disposition.label) {
                      void run(
                        updateDisposition.mutateAsync({
                          dispositionId: disposition.id,
                          label: event.target.value,
                        }),
                      )
                    }
                  }}
                />
                {disposition.is_default ? <Badge>default</Badge> : null}
                {disposition.is_system ? <Badge variant="outline">system</Badge> : null}
                {disposition.is_archived ? <Badge variant="secondary">archived</Badge> : null}
                {isAdmin && !disposition.is_archived ? (
                  <>
                    {disposition.is_default ? null : (
                      <Button
                        variant="ghost"
                        size="sm"
                        onClick={() =>
                          void run(setDefault.mutateAsync({ dispositionId: disposition.id }))
                        }
                      >
                        Set default
                      </Button>
                    )}
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Archive ${disposition.label}`}
                      onClick={() =>
                        void run(archiveDisposition.mutateAsync({ dispositionId: disposition.id }))
                      }
                    >
                      ✕
                    </Button>
                  </>
                ) : null}
              </div>
            ))}
            <p className="text-muted-foreground pt-1 text-xs">
              System statuses can be archived but not renamed. Exactly one is the default,
              preselected on the log-call form once a call passes the connected threshold.
            </p>
            {isAdmin ? (
              <div className="flex gap-2 pt-2">
                <Input
                  className="flex-1"
                  placeholder="New call outcome"
                  aria-label="New call outcome"
                  value={newDisposition}
                  onChange={(event) => setNewDisposition(event.target.value)}
                />
                <Button
                  size="sm"
                  disabled={newDisposition.trim() === ''}
                  onClick={() => {
                    void (async () => {
                      await run(createDisposition.mutateAsync({ label: newDisposition.trim() }))
                      setNewDisposition('')
                    })()
                  }}
                >
                  Add
                </Button>
              </div>
            ) : null}
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Workspace preferences</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-4 sm:grid-cols-4">
            <div className="space-y-1.5">
              <Label htmlFor="country-code">Default country code</Label>
              <Input
                id="country-code"
                defaultValue={preferences.data?.default_country_code ?? ''}
                disabled={!isAdmin}
                onBlur={(event) =>
                  void run(
                    updatePreferences.mutateAsync({ default_country_code: event.target.value }),
                  )
                }
              />
              <p className="text-muted-foreground text-xs">Phone numbers normalise against this.</p>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="timezone">Timezone</Label>
              <Input
                id="timezone"
                defaultValue={preferences.data?.timezone ?? ''}
                disabled={!isAdmin}
                onBlur={(event) =>
                  void run(updatePreferences.mutateAsync({ timezone: event.target.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="currency">Currency</Label>
              <Input
                id="currency"
                maxLength={3}
                defaultValue={preferences.data?.currency ?? ''}
                disabled={!isAdmin}
                onBlur={(event) =>
                  void run(updatePreferences.mutateAsync({ currency: event.target.value }))
                }
              />
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="call-threshold">Connected call min (s)</Label>
              <Input
                id="call-threshold"
                type="number"
                min={0}
                defaultValue={preferences.data?.connected_call_min_seconds ?? 0}
                disabled={!isAdmin}
                onBlur={(event) =>
                  void run(
                    updatePreferences.mutateAsync({
                      connected_call_min_seconds: Number(event.target.value),
                    }),
                  )
                }
              />
            </div>
          </div>

          <div>
            <Label>Features</Label>
            <p className="text-muted-foreground mb-2 text-xs">
              A disabled feature&rsquo;s API refuses with 403 — this is a boundary, not a menu.
            </p>
            <div className="flex flex-wrap gap-3">
              {Object.entries(preferences.data?.features ?? {}).map(([flag, enabled]) => (
                <label key={flag} className="flex items-center gap-2 text-sm">
                  <Checkbox
                    checked={enabled === true}
                    disabled={!isAdmin}
                    onChange={(event) =>
                      void run(
                        updatePreferences.mutateAsync({
                          features: { [flag]: event.target.checked },
                        }),
                      )
                    }
                  />
                  {flag.replace(/_/g, ' ')}
                </label>
              ))}
            </div>
          </div>
        </CardContent>
      </Card>
    </div>
  )
}
