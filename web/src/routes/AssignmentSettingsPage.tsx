/**
 * Assignment rules and sales groups (M8).
 *
 * The most consequential settings screen in the product: a rule here decides
 * where every future lead lands. Three things follow from that.
 *
 * **Order is the first thing you see.** Rules are first-match-wins, so a rule's
 * position matters as much as its contents, and a list that did not make the
 * order obvious would hide the actual behaviour.
 *
 * **Deactivating is offered; deleting is not.** A rule that has fired is part
 * of the explanation for where existing leads went.
 *
 * **The preview names the rule, not just the person.** "Assigned to Priya" is
 * not debuggable when six rules could have produced it.
 */

import { useState } from 'react'

import { ApiError } from '@/api/client'
import type { AssignmentStrategy, MemberDetail, Page } from '@/api/types'
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
  useArchiveGroup,
  useAssignmentRules,
  useCreateGroup,
  useCreateRule,
  useDeleteRule,
  useReorderRules,
  useSalesGroups,
  useSetGroupMembers,
  useUpdateRule,
} from '@/features/routing/api'
import { useQuery } from '@tanstack/react-query'

const STRATEGIES: ReadonlyArray<{
  key: AssignmentStrategy
  label: string
  hint: string
}> = [
  { key: 'ROUND_ROBIN', label: 'Round robin', hint: 'Each named member in turn.' },
  {
    key: 'WEIGHTED',
    label: 'Weighted',
    hint: 'In turn, but a member on weight 3 takes three of every cycle.',
  },
  {
    key: 'SALES_GROUP',
    label: 'Sales group',
    hint: "Round robin within a group, honouring the group's weights.",
  },
  {
    key: 'FIELD_VALUE',
    label: 'By field value',
    hint: "Look the lead's own value up in a map of value → member.",
  },
  { key: 'FIXED', label: 'Always one person', hint: 'Useful as a catch-all last rule.' },
  {
    key: 'UNASSIGNED',
    label: 'Leave unassigned',
    hint: 'Matches and deliberately assigns nobody, stopping the rules below.',
  },
]

function message(cause: unknown): string {
  if (!(cause instanceof ApiError)) return 'That action failed.'
  if (cause.code === 'insufficient_permissions') {
    return 'Your permission template does not allow managing assignment rules.'
  }
  return cause.message
}

function strategyLabel(strategy: AssignmentStrategy): string {
  return STRATEGIES.find((entry) => entry.key === strategy)?.label ?? strategy
}

export function AssignmentSettingsPage() {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string

  const rules = useAssignmentRules(workspaceId)
  const groups = useSalesGroups(workspaceId)
  const reorder = useReorderRules(workspaceId)
  const update = useUpdateRule(workspaceId)
  const remove = useDeleteRule(workspaceId)

  const [ruleOpen, setRuleOpen] = useState(false)
  const [groupOpen, setGroupOpen] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const members = useQuery({
    queryKey: ['members', workspaceId],
    queryFn: () =>
      api.get<Page<MemberDetail>>(`/workspaces/${workspaceId}/members`, {
        query: { limit: 100 },
      }),
  })
  const memberName = (id: string) =>
    (members.data?.items ?? []).find((m) => m.id === id)?.user.full_name ?? 'Unknown'

  const ordered = [...(rules.data ?? [])]

  function move(index: number, direction: -1 | 1) {
    const next = [...ordered]
    const target = index + direction
    const moving = next[index]
    const displaced = next[target]
    if (!moving || !displaced) return
    next[index] = displaced
    next[target] = moving
    setError(null)
    reorder.mutate(
      next.map((rule) => rule.id),
      { onError: (cause) => setError(message(cause)) },
    )
  }

  return (
    <div className="space-y-6">
      <header className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Assignment</h1>
          <p className="text-muted-foreground text-sm">
            Rules run in order on every new lead and the first match wins.
          </p>
        </div>
        <div className="flex gap-2">
          <Button variant="outline" onClick={() => setGroupOpen(true)}>
            New sales group
          </Button>
          <Button onClick={() => setRuleOpen(true)}>New rule</Button>
        </div>
      </header>

      {error ? (
        <p role="alert" className="text-destructive text-sm">
          {error}
        </p>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Rules, in order</CardTitle>
        </CardHeader>
        <CardContent>
          {rules.isError ? (
            <p role="alert" className="text-destructive py-8 text-center text-sm">
              {message(rules.error)}
            </p>
          ) : ordered.length === 0 && !rules.isLoading ? (
            <p className="text-muted-foreground py-8 text-center text-sm">
              No rules yet. Without one, every new lead arrives unassigned.
            </p>
          ) : (
            <ol className="space-y-2">
              {ordered.map((rule, index) => (
                <li
                  key={rule.id}
                  data-testid="assignment-rule"
                  className="flex flex-wrap items-center gap-3 rounded-md border p-3"
                >
                  <span className="text-muted-foreground w-6 text-sm tabular-nums">
                    {index + 1}
                  </span>
                  <div className="min-w-48 flex-1">
                    <span className="font-medium">{rule.name}</span>
                    <span className="text-muted-foreground block text-xs">
                      {strategyLabel(rule.strategy)}
                      {Object.keys(rule.conditions ?? {}).length === 0
                        ? ' · matches every lead'
                        : ' · conditional'}
                      {rule.skip_unavailable ? ' · skips members on leave' : ''}
                    </span>
                  </div>
                  {!rule.is_active ? <Badge variant="outline">Inactive</Badge> : null}
                  <div className="flex gap-1">
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Move ${rule.name} up`}
                      disabled={index === 0 || reorder.isPending}
                      onClick={() => move(index, -1)}
                    >
                      ↑
                    </Button>
                    <Button
                      variant="ghost"
                      size="sm"
                      aria-label={`Move ${rule.name} down`}
                      disabled={index === ordered.length - 1 || reorder.isPending}
                      onClick={() => move(index, 1)}
                    >
                      ↓
                    </Button>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => {
                        setError(null)
                        if (rule.is_active) {
                          remove.mutate(rule.id, {
                            onError: (cause) => setError(message(cause)),
                          })
                        } else {
                          update.mutate(
                            { ruleId: rule.id, is_active: true },
                            { onError: (cause) => setError(message(cause)) },
                          )
                        }
                      }}
                    >
                      {rule.is_active ? 'Deactivate' : 'Reactivate'}
                    </Button>
                  </div>
                </li>
              ))}
            </ol>
          )}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Sales groups</CardTitle>
        </CardHeader>
        <CardContent className="space-y-2">
          {(groups.data ?? []).length === 0 && !groups.isLoading ? (
            <p className="text-muted-foreground py-6 text-center text-sm">No groups yet.</p>
          ) : null}
          {(groups.data ?? []).map((group) => (
            <GroupRow
              key={group.id}
              groupId={group.id}
              name={group.name}
              description={group.description}
              memberName={memberName}
              members={members.data?.items ?? []}
              onError={setError}
            />
          ))}
        </CardContent>
      </Card>

      <NewRuleDialog
        open={ruleOpen}
        onClose={() => setRuleOpen(false)}
        members={members.data?.items ?? []}
        groups={(groups.data ?? []).map((group) => ({ id: group.id, name: group.name }))}
        onError={setError}
      />
      <NewGroupDialog open={groupOpen} onClose={() => setGroupOpen(false)} onError={setError} />
    </div>
  )
}

function GroupRow({
  groupId,
  name,
  description,
  members,
  memberName,
  onError,
}: {
  readonly groupId: string
  readonly name: string
  readonly description: string | null
  readonly members: readonly MemberDetail[]
  readonly memberName: (id: string) => string
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const workspaceId = activeWorkspaceId as string
  const archive = useArchiveGroup(workspaceId)
  const setMembers = useSetGroupMembers(workspaceId)
  const [adding, setAdding] = useState(false)
  const [chosen, setChosen] = useState('')
  const [weight, setWeight] = useState('1')

  return (
    <div data-testid="sales-group" className="rounded-md border p-3">
      <div className="flex flex-wrap items-center gap-3">
        <div className="min-w-48 flex-1">
          <span className="font-medium">{name}</span>
          {description ? (
            <span className="text-muted-foreground block text-xs">{description}</span>
          ) : null}
        </div>
        <Button variant="ghost" size="sm" onClick={() => setAdding((open) => !open)}>
          Members
        </Button>
        <Button
          variant="outline"
          size="sm"
          onClick={() => {
            onError(null)
            archive.mutate(groupId, { onError: (cause) => onError(message(cause)) })
          }}
        >
          Archive
        </Button>
      </div>

      {adding ? (
        <div className="mt-2 flex flex-wrap items-end gap-2 border-t pt-2">
          <div className="space-y-1">
            <FieldLabel htmlFor={`group-member-${groupId}`}>Member</FieldLabel>
            <Select
              id={`group-member-${groupId}`}
              value={chosen}
              onChange={(event) => setChosen(event.target.value)}
            >
              <option value="">Choose a member</option>
              {members.map((member) => (
                <option key={member.id} value={member.id}>
                  {member.user.full_name}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1">
            <FieldLabel htmlFor={`group-weight-${groupId}`}>Weight</FieldLabel>
            <Input
              id={`group-weight-${groupId}`}
              className="w-20"
              type="number"
              min={1}
              max={100}
              value={weight}
              onChange={(event) => setWeight(event.target.value)}
            />
          </div>
          <Button
            size="sm"
            disabled={!chosen || setMembers.isPending}
            onClick={() => {
              onError(null)
              setMembers.mutate(
                {
                  groupId,
                  members: [{ membership_id: chosen, weight: Number(weight) || 1 }],
                },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: () => {
                    setChosen('')
                    setWeight('1')
                  },
                },
              )
            }}
          >
            Set members
          </Button>
          <span className="text-muted-foreground text-xs">
            Replaces the whole membership —{' '}
            {memberName(chosen || '') !== 'Unknown'
              ? `currently setting just ${memberName(chosen)}`
              : 'choose everyone who should be in it'}
            .
          </span>
        </div>
      ) : null}
    </div>
  )
}

function NewGroupDialog({
  open,
  onClose,
  onError,
}: {
  readonly open: boolean
  readonly onClose: () => void
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const create = useCreateGroup(activeWorkspaceId as string)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New sales group"
      description="A distribution target, and a report segment later."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!name.trim() || create.isPending}
            onClick={() => {
              onError(null)
              create.mutate(
                {
                  name: name.trim(),
                  ...(description.trim() ? { description: description.trim() } : {}),
                },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: () => {
                    setName('')
                    setDescription('')
                    onClose()
                  },
                },
              )
            }}
          >
            Create group
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="group-name">Name</FieldLabel>
          <Input id="group-name" value={name} onChange={(event) => setName(event.target.value)} />
        </div>
        <div className="space-y-1.5">
          <FieldLabel htmlFor="group-description">Description</FieldLabel>
          <Input
            id="group-description"
            value={description}
            onChange={(event) => setDescription(event.target.value)}
          />
        </div>
      </div>
    </Dialog>
  )
}

function NewRuleDialog({
  open,
  onClose,
  members,
  groups,
  onError,
}: {
  readonly open: boolean
  readonly onClose: () => void
  readonly members: readonly MemberDetail[]
  readonly groups: ReadonlyArray<{ id: string; name: string }>
  readonly onError: (message: string | null) => void
}) {
  const { activeWorkspaceId } = useAuth()
  const create = useCreateRule(activeWorkspaceId as string)

  const [name, setName] = useState('')
  const [strategy, setStrategy] = useState<AssignmentStrategy>('ROUND_ROBIN')
  const [chosen, setChosen] = useState<string[]>([])
  const [groupId, setGroupId] = useState('')
  const [skipUnavailable, setSkipUnavailable] = useState(true)

  const [lastOpen, setLastOpen] = useState(open)
  if (open !== lastOpen) {
    setLastOpen(open)
    if (open) {
      setName('')
      setStrategy('ROUND_ROBIN')
      setChosen([])
      setGroupId('')
      setSkipUnavailable(true)
    }
  }

  const spec = STRATEGIES.find((entry) => entry.key === strategy)
  const needsMembers = strategy === 'ROUND_ROBIN' || strategy === 'WEIGHTED'
  const needsOne = strategy === 'FIXED'
  const needsGroup = strategy === 'SALES_GROUP'

  function config(): Record<string, unknown> {
    if (strategy === 'ROUND_ROBIN') return { membership_ids: chosen }
    if (strategy === 'WEIGHTED') {
      return { members: chosen.map((id) => ({ membership_id: id, weight: 1 })) }
    }
    if (strategy === 'FIXED') return { membership_id: chosen[0] }
    if (strategy === 'SALES_GROUP') return { group_id: groupId }
    return {}
  }

  const ready =
    name.trim().length > 0 &&
    (needsMembers ? chosen.length > 0 : true) &&
    (needsOne ? chosen.length === 1 : true) &&
    (needsGroup ? groupId !== '' : true)

  return (
    <Dialog
      open={open}
      onClose={onClose}
      title="New assignment rule"
      description="Runs on every new lead — from the UI, an import, or the intake API."
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            Cancel
          </Button>
          <Button
            disabled={!ready || create.isPending}
            onClick={() => {
              onError(null)
              create.mutate(
                {
                  name: name.trim(),
                  strategy,
                  config: config(),
                  skip_unavailable: skipUnavailable,
                },
                {
                  onError: (cause) => onError(message(cause)),
                  onSuccess: onClose,
                },
              )
            }}
          >
            Create rule
          </Button>
        </>
      }
    >
      <div className="space-y-3">
        <div className="space-y-1.5">
          <FieldLabel htmlFor="rule-name">Name</FieldLabel>
          <Input
            id="rule-name"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Website enquiries"
          />
        </div>

        <div className="space-y-1.5">
          <FieldLabel htmlFor="rule-strategy">Strategy</FieldLabel>
          <Select
            id="rule-strategy"
            value={strategy}
            onChange={(event) => {
              setStrategy(event.target.value as AssignmentStrategy)
              setChosen([])
            }}
          >
            {STRATEGIES.map((entry) => (
              <option key={entry.key} value={entry.key}>
                {entry.label}
              </option>
            ))}
          </Select>
          {spec ? <p className="text-muted-foreground text-xs">{spec.hint}</p> : null}
        </div>

        {needsGroup ? (
          <div className="space-y-1.5">
            <FieldLabel htmlFor="rule-group">Group</FieldLabel>
            <Select
              id="rule-group"
              value={groupId}
              onChange={(event) => setGroupId(event.target.value)}
            >
              <option value="">Choose a group</option>
              {groups.map((group) => (
                <option key={group.id} value={group.id}>
                  {group.name}
                </option>
              ))}
            </Select>
          </div>
        ) : null}

        {needsMembers || needsOne ? (
          <fieldset className="space-y-1.5">
            <legend className="text-sm font-medium">
              {needsOne ? 'Who gets these leads' : 'Members, in order'}
            </legend>
            <div className="max-h-48 space-y-1 overflow-y-auto rounded-md border p-2">
              {members.map((member) => (
                <label key={member.id} className="flex items-center gap-2 text-sm">
                  <input
                    type={needsOne ? 'radio' : 'checkbox'}
                    name="rule-members"
                    checked={chosen.includes(member.id)}
                    onChange={(event) => {
                      if (needsOne) {
                        setChosen([member.id])
                      } else {
                        setChosen((current) =>
                          event.target.checked
                            ? [...current, member.id]
                            : current.filter((id) => id !== member.id),
                        )
                      }
                    }}
                  />
                  {member.user.full_name}
                </label>
              ))}
            </div>
          </fieldset>
        ) : null}

        <label className="flex items-start gap-2 rounded-md border p-2 text-sm">
          <input
            type="checkbox"
            className="mt-0.5"
            checked={skipUnavailable}
            onChange={(event) => setSkipUnavailable(event.target.checked)}
          />
          <span>
            Skip members who are on leave
            <span className="text-muted-foreground block text-xs">
              Members without a licence are always skipped — they cannot log in to work the lead.
            </span>
          </span>
        </label>
      </div>
    </Dialog>
  )
}
