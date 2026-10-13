/**
 * Custom action types (docs/03-configuration-model.md §4).
 *
 * The deepest configurability in the product: an admin-defined timeline event
 * with its own form. The nested field builder reads the *action* registry —
 * eight types, a different set from the lead schema's thirteen — which is why
 * this page fetches `/settings/action-field-types` rather than reusing the lead
 * one.
 *
 * This whole screen sits behind the `custom_actions` feature flag. When it is
 * off the API answers 403, and the page says so rather than rendering an empty
 * list that looks like "you have not made any yet".
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { CustomActionType } from '@/api/types'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/features/auth/context'
import { usePermissions } from '@/features/auth/usePermissions'
import { useActionFieldTypes } from '@/features/fields/api'
import {
  useAddActionField,
  useArchiveCustomAction,
  useCreateCustomAction,
  useCustomActions,
  useUpdateCustomAction,
} from '@/features/pipeline/api'

/** Directions come from §4.2 and are a product concept, not a customer's. */
const DIRECTIONS = ['INBOUND', 'OUTBOUND', 'INFORMATION'] as const

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'feature_disabled') {
    return 'Custom actions are switched off for this workspace. Enable them in Pipeline settings.'
  }
  if (cause.code === 'score_out_of_range') return 'Score must be between -1000 and 1000.'
  return cause.message
}

function ActionFieldBuilder({
  workspaceId,
  actionType,
  canEdit,
}: {
  workspaceId: string
  actionType: CustomActionType
  canEdit: boolean
}) {
  const actionTypes = useActionFieldTypes(workspaceId)
  const addField = useAddActionField(workspaceId)

  const [label, setLabel] = useState('')
  const [typeKey, setTypeKey] = useState('')
  const [required, setRequired] = useState(false)
  const [options, setOptions] = useState('')
  const [error, setError] = useState<string | null>(null)

  const selected = actionTypes.data?.find((spec) => spec.key === typeKey)

  return (
    <div className="space-y-2 border-t pt-3">
      <Label className="text-xs uppercase">Form fields</Label>
      <ul className="space-y-1">
        {actionType.fields.map((field) => (
          <li key={field.id} className="flex items-center gap-2 text-sm">
            <span className="font-medium">{field.label}</span>
            <Badge variant="outline">{field.field_type}</Badge>
            {field.is_required ? <Badge variant="secondary">required</Badge> : null}
            <code className="text-muted-foreground text-xs">{field.key}</code>
          </li>
        ))}
      </ul>

      {canEdit ? (
        <div className="flex flex-wrap items-end gap-2">
          <Input
            className="min-w-36 flex-1"
            placeholder="Field name"
            aria-label={`New field for ${actionType.name}`}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          />
          <Select
            className="w-40"
            aria-label={`Field type for ${actionType.name}`}
            value={typeKey}
            onChange={(event) => setTypeKey(event.target.value)}
          >
            <option value="">Type…</option>
            {(actionTypes.data ?? []).map((spec) => (
              <option key={spec.key} value={spec.key}>
                {spec.label}
              </option>
            ))}
          </Select>
          {selected?.uses_options ? (
            <Input
              className="w-48"
              placeholder="Options, comma separated"
              aria-label="Options"
              value={options}
              onChange={(event) => setOptions(event.target.value)}
            />
          ) : null}
          <label className="flex items-center gap-1.5 text-sm">
            <Checkbox checked={required} onChange={(event) => setRequired(event.target.checked)} />
            Required
          </label>
          <Button
            size="sm"
            disabled={label.trim() === '' || typeKey === ''}
            onClick={() => {
              void (async () => {
                setError(null)
                try {
                  await addField.mutateAsync({
                    typeId: actionType.id,
                    label: label.trim(),
                    field_type: typeKey,
                    is_required: required,
                    options: options
                      .split(',')
                      .map((entry) => entry.trim())
                      .filter(Boolean),
                  })
                  setLabel('')
                  setOptions('')
                  setRequired(false)
                } catch (cause) {
                  setError(message(cause))
                }
              })()
            }}
          >
            Add field
          </Button>
        </div>
      ) : null}

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}
    </div>
  )
}

export function CustomActionsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const { isAdmin } = usePermissions()

  const [name, setName] = useState('')
  const [score, setScore] = useState('0')
  const [direction, setDirection] = useState<string>('INFORMATION')
  const [allowPredated, setAllowPredated] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const actions = useCustomActions(workspaceId)
  const createAction = useCreateCustomAction(workspaceId)
  const updateAction = useUpdateCustomAction(workspaceId)
  const archiveAction = useArchiveCustomAction(workspaceId)

  const featureDisabled =
    actions.error instanceof ApiError && actions.error.code === 'feature_disabled'

  if (featureDisabled) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="text-base">Custom actions are switched off</CardTitle>
        </CardHeader>
        <CardContent className="text-muted-foreground space-y-2 text-sm">
          <p>
            This workspace has the <code>custom_actions</code> feature disabled, so the API refuses
            these endpoints with 403 — not just this screen.
          </p>
          <p>Turn it back on under Pipeline settings → Features.</p>
        </CardContent>
      </Card>
    )
  }

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-2xl font-semibold tracking-tight">Custom actions</h1>
        <p className="text-muted-foreground text-sm">
          User-defined timeline events, each with its own form. Codes are assigned per workspace
          from 1001.
        </p>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      {isAdmin ? (
        <Card>
          <CardHeader>
            <CardTitle className="text-base">Create an action</CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap items-end gap-3">
            <div className="min-w-40 flex-1 space-y-1.5">
              <Label htmlFor="action-name">Name</Label>
              <Input
                id="action-name"
                value={name}
                onChange={(event) => setName(event.target.value)}
              />
            </div>
            <div className="w-28 space-y-1.5">
              <Label htmlFor="action-score">Score</Label>
              <Input
                id="action-score"
                type="number"
                min={-1000}
                max={1000}
                value={score}
                onChange={(event) => setScore(event.target.value)}
              />
            </div>
            <div className="w-40 space-y-1.5">
              <Label htmlFor="action-direction">Direction</Label>
              <Select
                id="action-direction"
                value={direction}
                onChange={(event) => setDirection(event.target.value)}
              >
                {DIRECTIONS.map((entry) => (
                  <option key={entry} value={entry}>
                    {entry.charAt(0) + entry.slice(1).toLowerCase()}
                  </option>
                ))}
              </Select>
            </div>
            <label className="flex items-center gap-2 pb-2 text-sm">
              <Checkbox
                checked={allowPredated}
                onChange={(event) => setAllowPredated(event.target.checked)}
              />
              Allow predated
            </label>
            <Button
              disabled={name.trim() === '' || createAction.isPending}
              onClick={() => {
                void (async () => {
                  setError(null)
                  try {
                    await createAction.mutateAsync({
                      name: name.trim(),
                      score: Number(score),
                      direction,
                      allow_predated: allowPredated,
                    })
                    setName('')
                    setScore('0')
                  } catch (cause) {
                    setError(message(cause))
                  }
                })()
              }}
            >
              Create
            </Button>
          </CardContent>
        </Card>
      ) : null}

      <div className="space-y-4">
        {(actions.data ?? []).map((actionType) => (
          <Card key={actionType.id} data-testid="custom-action-card">
            <CardHeader className="flex-row items-center justify-between gap-3 space-y-0">
              <div className="flex items-center gap-2">
                <CardTitle className="text-base">{actionType.name}</CardTitle>
                <Badge variant="outline">#{actionType.code}</Badge>
                <Badge variant={actionType.score >= 0 ? 'success' : 'destructive'}>
                  {actionType.score >= 0 ? '+' : ''}
                  {actionType.score}
                </Badge>
                <Badge variant="secondary">{actionType.direction.toLowerCase()}</Badge>
                {actionType.allow_predated ? <Badge variant="outline">predatable</Badge> : null}
              </div>
              {isAdmin ? (
                <div className="flex items-center gap-2">
                  <Input
                    className="h-8 w-24"
                    type="number"
                    aria-label={`Score for ${actionType.name}`}
                    defaultValue={actionType.score}
                    onBlur={(event) => {
                      if (Number(event.target.value) !== actionType.score) {
                        void updateAction
                          .mutateAsync({
                            typeId: actionType.id,
                            score: Number(event.target.value),
                          })
                          .catch((cause: unknown) => setError(message(cause)))
                      }
                    }}
                  />
                  <Button
                    variant="ghost"
                    size="sm"
                    onClick={() =>
                      void archiveAction
                        .mutateAsync({ typeId: actionType.id })
                        .catch((cause: unknown) => setError(message(cause)))
                    }
                  >
                    Archive
                  </Button>
                </div>
              ) : null}
            </CardHeader>
            <CardContent>
              <ActionFieldBuilder
                workspaceId={workspaceId}
                actionType={actionType}
                canEdit={isAdmin}
              />
            </CardContent>
          </Card>
        ))}
        {(actions.data ?? []).length === 0 && !actions.isLoading ? (
          <p className="text-muted-foreground text-sm">
            No custom actions yet. A workspace starts with none — every one is its own invention.
          </p>
        ) : null}
      </div>
    </div>
  )
}
